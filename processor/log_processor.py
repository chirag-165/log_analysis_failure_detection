"""
log_processor.py

Reads logs from Redis, aggregates them into windows per service, computes
statistical anomaly features + ML failure probability, stores results in
MongoDB, and asks the PolicyEngine to decide (NOT execute) a recovery action.

CHANGE 1 ONLY:
-------------
Traffic delta is calculated using requests/logs PER ACTIVE CONTAINER rather
than total request/log count.

This prevents an automatic scale-up from being interpreted as a traffic spike.

Example:

Before scale-up:
    1 container -> 100 logs
    normalized traffic = 100 / 1 = 100

After scale-up:
    3 containers -> 300 logs
    normalized traffic = 300 / 3 = 100

Therefore:

    traffic_delta = (100 - 100) / 100 = 0

instead of:

    traffic_delta = (300 - 100) / 100 = 2.0

All existing detection thresholds, ML threshold, anomaly logic,
cooldown logic, recovery policy and policy_engine behavior are preserved.
"""

from collections import defaultdict, deque
from datetime import datetime, timezone
import json
import logging
import os
import pickle
import statistics
import time

from dotenv import load_dotenv
import numpy as np
import pandas as pd
from pymongo import MongoClient
import redis

from policy_engine import PolicyEngine

load_dotenv()


# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s"
)
logger = logging.getLogger(__name__)


# ============================================================================
# INFRASTRUCTURE CONFIGURATION
# ============================================================================

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
MONGO_URI = os.getenv("MONGO_URI")
MODEL_PATH = os.getenv("MODEL_PATH", "failure_model_v2.pkl")


# ============================================================================
# WINDOW CONFIGURATION
# ============================================================================

WINDOW_SIZE_SEC = int(os.getenv("WINDOW_SIZE_SEC", "60"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "30"))
WARMUP_WINDOWS = int(os.getenv("WARMUP_WINDOWS", "3"))
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "120"))


# ============================================================================
# DETECTION THRESHOLDS
# ============================================================================
#
# IMPORTANT:
# These thresholds are intentionally kept unchanged.
#

SENSITIVITY_K = float(os.getenv("SENSITIVITY_K", "2.75"))
TRAFFIC_ANOMALY_THR = float(os.getenv("TRAFFIC_ANOMALY_THR", "1.5"))
ERROR_RATE_RULE_THR = float(os.getenv("ERROR_RATE_RULE_THR", "0.15"))
LATENCY_RULE_THR_MS = float(os.getenv("LATENCY_RULE_THR_MS", "250.0"))
SUSTAINED_ANOMALY_WIN = int(os.getenv("SUSTAINED_ANOMALY_WIN", "6"))
SUSTAINED_TRAFFIC_WIN = int(os.getenv("SUSTAINED_TRAFFIC_WIN", "3"))
TRAFFIC_CRASH_THR = float(os.getenv("TRAFFIC_CRASH_THR", "-1.5"))
TRAFFIC_SPIKE_THR = float(os.getenv("TRAFFIC_SPIKE_THR", "2.0"))
MEDIUM_PROB_THR = float(os.getenv("MEDIUM_PROB_THR", "0.40"))
EPS = 1e-6

# Scale-down: consecutive LOW windows before restoring to 1 replica.
SCALE_DOWN_STABLE_WIN = int(os.getenv("SCALE_DOWN_STABLE_WIN", "3"))


# ============================================================================
# SERVICE MAP
# ============================================================================

SERVICE_MAP = {
    "auth-service": [1, 0, 0],
    "order-service": [0, 1, 0],
    "payment-service": [0, 0, 1],
}


# ============================================================================
# REDIS & MONGODB
# ============================================================================

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["log_analysis_dashboard"]
window_collection = db["window_history"]
window_collection.create_index("timestamp", expireAfterSeconds=86400)


# ============================================================================
# LOAD MACHINE LEARNING MODEL
# ============================================================================

with open(MODEL_PATH, "rb") as f:
    model_data = pickle.load(f)
    ml_model = model_data["model"]
    feature_names = model_data["features"]
    # Existing threshold intentionally preserved.
    ml_threshold = 0.7

logger.info(
    "ML model loaded | threshold=%.3f | features=%s",
    ml_threshold,
    feature_names
)


# ============================================================================
# PREDICTIVE ANALYZER
# ============================================================================

class PredictiveAnalyzer:

    def __init__(self):
        # Logs collected during the current window.
        self.current_window_logs = []

        # Rolling baseline history.
        self.window_history = defaultdict(
            lambda: deque(maxlen=HISTORY_LIMIT)
        )

        # Last recovery timestamp per service.
        self.last_recovery_time = defaultdict(float)

        # Last known host per service.
        self.last_known_host = defaultdict(lambda: None)

        # Consecutive LOW-risk windows.
        self.low_streak = defaultdict(int)

        # Existing traffic streak structure preserved.
        self.traffic_delta_streak = defaultdict(int)

        # Consecutive anomalous windows.
        self.anomaly_streak = defaultdict(int)

        # Policy engine.
        self.policy_engine = PolicyEngine(mongo_uri=MONGO_URI)
        self.fault_experiments_col = db["fault_experiments"]
        self.start_time = time.time()

        self._restore_history()

    def _get_active_experiment(self, service: str):
        """Helper to get active experiment metadata if current window overlaps an experiment."""
        now = datetime.now(timezone.utc)
        doc = self.fault_experiments_col.find_one({
            "service": service,
            "status": "RUNNING",
            "start_time": {"$lte": now},
            "planned_end_time": {"$gte": now},
        })
        if doc:
            return doc.get("experiment_id"), doc.get("fault_type")
        return None, None

    # ========================================================================
    # RESTORE HISTORY
    # ========================================================================

    def _restore_history(self):
        """
        Reload historical LOW-risk windows from MongoDB.

        The new traffic calculation is restored using:

            requests_per_container

        For old records that don't have this field, it is reconstructed from:

            request_count / container_count

        with container_count defaulting to 1.
        """
        for svc in SERVICE_MAP:
            cursor = (
                window_collection
                .find({
                    "service": svc,
                    "risk": "LOW"
                })
                .sort("timestamp", -1)
                .limit(HISTORY_LIMIT)
            )

            docs = list(cursor)[::-1]

            for doc in docs:
                request_count = doc.get("request_count", 0)
                container_count = max(doc.get("container_count", 1), 1)

                # New field introduced by Change 1.
                requests_per_container = doc.get("requests_per_container")

                # Backward compatibility with old MongoDB records.
                if requests_per_container is None:
                    requests_per_container = request_count / container_count

                self.window_history[svc].append({
                    "latency": doc.get("p95_latency", 0),
                    "weighted_err": doc.get("weighted_error_rate", 0),
                    "warn_freq": doc.get("warn_frequency", 0),
                    "count": request_count,
                    "container_count": container_count,
                    "requests_per_container": requests_per_container,
                })

            if docs and docs[-1].get("host_ip"):
                self.last_known_host[svc] = docs[-1]["host_ip"]

        logger.info(
            "Baseline restored (LOW-risk windows only) for %d services",
            len(SERVICE_MAP)
        )

    # ========================================================================
    # PROCESS WINDOW
    # ========================================================================

    def process_window(self):
        logs_by_service = defaultdict(list)

        for log in self.current_window_logs:
            svc = log.get("service")
            if svc in SERVICE_MAP:
                logs_by_service[svc].append(log)

        for service in SERVICE_MAP:
            self._analyze_service(service, logs_by_service.get(service, []))

        self.current_window_logs = []
        self.start_time = time.time()

    # ========================================================================
    # ANALYZE SERVICE
    # ========================================================================

    def _analyze_service(self, service, logs):
        total = len(logs)

        # ====================================================================
        # CHANGE 1: DETERMINE ACTIVE CONTAINERS
        # ====================================================================
        # A scale-out increases the number of log producers.
        # Therefore total logs alone cannot represent traffic.
        # We normalize traffic by the number of active containers.

        active_containers = {
            l.get("container_id")
            for l in logs
            if l.get("container_id")
        }

        # Remove unknown container identifiers.
        active_containers.discard("unknown")

        # If container IDs are missing, use 1 so the system continues operating normally.
        container_count = max(len(active_containers), 1)
        requests_per_container = total / container_count

        # ====================================================================
        # NO LOGS
        # ====================================================================

        if total == 0:
            logger.warning(
                "%s: 0 logs this window → possible crash (host=%s)",
                service.upper(),
                self.last_known_host[service]
            )
            # No baseline update.
            return

        # ====================================================================
        # HOST IP CACHE
        # ====================================================================

        host_ip = logs[-1].get("host_ip") or self.last_known_host[service]
        if host_ip:
            self.last_known_host[service] = host_ip

        # ====================================================================
        # RAW METRICS
        # ====================================================================

        errors = sum(1 for l in logs if l.get("level") == "ERROR")
        warns = sum(1 for l in logs if l.get("level") in ("WARN", "WARNING"))

        warn_freq = warns / total
        weighted_error_rate = (errors + 0.3 * warns) / total

        # ====================================================================
        # LATENCY
        # ====================================================================

        latencies = [l.get("response_time", 0) for l in logs]
        p95_latency = float(np.percentile(latencies, 95)) if latencies else 0.0

        # ====================================================================
        # PER-CONTAINER ERROR TRACKING
        # ====================================================================

        container_errors = defaultdict(int)
        container_host = {}

        for l in logs:
            if l.get("level") == "ERROR":
                cid = l.get("container_id", "unknown")
                container_errors[cid] += 1
                if l.get("host_ip"):
                    container_host[cid] = l["host_ip"]

        top_container = None
        top_error_count = 0
        top_container_host = host_ip

        if container_errors:
            top_container = max(container_errors, key=container_errors.get)
            top_error_count = container_errors[top_container]
            top_container_host = container_host.get(top_container, host_ip)

        # ====================================================================
        # WARMUP
        # ====================================================================

        hist = list(self.window_history[service])

        if len(hist) < WARMUP_WINDOWS:
            self.window_history[service].append({
                "latency": p95_latency,
                "weighted_err": weighted_error_rate,
                "warn_freq": warn_freq,
                "count": total,
                # CHANGE 1:
                "container_count": container_count,
                "requests_per_container": requests_per_container,
            })

            logger.info(
                "%s warming up (%d/%d windows)",
                service.upper(),
                len(hist) + 1,
                WARMUP_WINDOWS
            )
            return

        # ====================================================================
        # Z-SCORE COMPUTATION
        # ====================================================================

        def compute_z(val, key):
            values = [h[key] for h in hist]
            mean = statistics.mean(values)
            std = max(statistics.stdev(values), EPS)
            z = (val - mean) / std
            return z, z > SENSITIVITY_K

        z_lat, lat_anom = compute_z(p95_latency, "latency")
        z_we, we_anom = compute_z(weighted_error_rate, "weighted_err")
        z_warn, warn_anom = compute_z(warn_freq, "warn_freq")

        # ====================================================================
        # CHANGE 1: CONTAINER-NORMALIZED TRAFFIC DELTA
        # ====================================================================
        # OLD:
        #   (current_total - previous_total) / previous_total
        # PROBLEM:
        #   Scaling from 1 → 3 containers causes total logs to increase,
        #   even when actual workload per container has not increased.
        # NEW:
        #   Compare requests_per_container.
        # This prevents scale-out itself from being classified as traffic anomaly.

        previous_requests_per_container = hist[-1].get("requests_per_container")

        # Backward compatibility for old in-memory/history records.
        if previous_requests_per_container is None:
            previous_container_count = max(hist[-1].get("container_count", 1), 1)
            previous_requests_per_container = (
                hist[-1]["count"] / previous_container_count
            )

        traffic_delta = (
            requests_per_container - previous_requests_per_container
        ) / max(previous_requests_per_container, 1)

        traffic_anom = abs(traffic_delta) > TRAFFIC_ANOMALY_THR

        # ====================================================================
        # ANOMALY COUNT
        # ====================================================================

        anomaly_count = sum([lat_anom, we_anom, warn_anom, traffic_anom])

        # ====================================================================
        # ML FEATURE PREPARATION
        # ====================================================================

        one_hot = SERVICE_MAP[service]

        feature_dict = {
            "is_auth": one_hot[0],
            "is_order": one_hot[1],
            "is_payment": one_hot[2],
            "z_latency": z_lat,
            "z_errors": z_we,
            "z_warns": z_warn,
            "traffic_delta": traffic_delta,
            "anomaly_count": anomaly_count,
        }

        feature_df = pd.DataFrame([feature_dict])

        # Make sure all model features exist.
        for col in feature_names:
            if col not in feature_df.columns:
                feature_df[col] = 0.0

        feature_df = feature_df[feature_names]

        # ====================================================================
        # ML INFERENCE
        # ====================================================================

        ml_prob = float(ml_model.predict_proba(feature_df)[0][1])

        # ====================================================================
        # ANOMALY STREAK
        # ====================================================================

        if anomaly_count >= 1:
            self.anomaly_streak[service] += 1
        else:
            self.anomaly_streak[service] = 0

        sustained_failure = (
            self.anomaly_streak[service] >= SUSTAINED_ANOMALY_WIN
        )

        # ====================================================================
        # HYBRID RISK CLASSIFICATION
        # ====================================================================

        # Signal A — ML
        ml_high = ml_prob >= ml_threshold and anomaly_count >= 1

        # Signal B — Statistical
        stat_high = anomaly_count >= 2

        # Signal C — Error rule
        rule_error = weighted_error_rate > ERROR_RATE_RULE_THR

        # Signal D — Latency rule
        rule_latency = p95_latency > LATENCY_RULE_THR_MS

        # Signal E — Traffic rule
        rule_traffic = (
            traffic_delta < TRAFFIC_CRASH_THR or traffic_delta > TRAFFIC_SPIKE_THR
        )

        # Signal F — Sustained degradation
        rule_sustained = sustained_failure

        high_signals = {
            "ML": ml_high,
            "Stat": stat_high,
            "ErrRule": rule_error,
            "LatRule": rule_latency,
            "Traffic": rule_traffic,
            "Sustained": rule_sustained,
        }

        fired = [k for k, v in high_signals.items() if v]

        if fired:
            risk = "HIGH"
            decision_source = "Hybrid" if len(fired) >= 2 else fired[0]
        elif ml_prob > MEDIUM_PROB_THR or anomaly_count == 1:
            risk = "MEDIUM"
            decision_source = "ML" if ml_prob > MEDIUM_PROB_THR else "Stat"
        else:
            risk = "LOW"
            decision_source = "None"

        # ====================================================================
        # DECISION REASON
        # ====================================================================

        parts = []

        if ml_high:
            parts.append(f"ML={ml_prob:.3f}")
        if stat_high:
            parts.append(f"anom={anomaly_count}")
        if rule_error:
            parts.append(f"err={weighted_error_rate:.3f}")
        if rule_latency:
            parts.append(f"p95={p95_latency:.0f}ms")
        if rule_traffic:
            parts.append(f"traffic={traffic_delta:.2f}")
        if rule_sustained:
            parts.append(f"streak={self.anomaly_streak[service]}")

        decision_reason = " | ".join(parts) if parts else "all signals low"

        # ====================================================================
        # COOLDOWN
        # ====================================================================

        in_cooldown = (
            time.time() - self.last_recovery_time[service] < COOLDOWN_SEC
        )

        effective_risk = (
            "COOLDOWN" if (risk == "HIGH" and in_cooldown) else risk
        )

        # ====================================================================
        # BASELINE UPDATE
        # ====================================================================
        # Existing behavior preserved.

        if effective_risk in ("LOW", "MEDIUM"):
            self.window_history[service].append({
                "latency": p95_latency,
                "weighted_err": weighted_error_rate,
                "warn_freq": warn_freq,
                "count": total,
                # CHANGE 1:
                "container_count": container_count,
                "requests_per_container": requests_per_container,
            })
            self.low_streak[service] = self.low_streak.get(service, 0) + 1
        else:
            self.low_streak[service] = 0

        exp_id, exp_fault_type = self._get_active_experiment(service)

        # ====================================================================
        # STORE WINDOW IN MONGODB
        # ====================================================================

        window_collection.insert_one({
            "service": service,
            "timestamp": datetime.now(timezone.utc),
            "experiment_id": exp_id,
            "experiment_fault_type": exp_fault_type,
            "request_count": int(total),
            # CHANGE 1:
            "container_count": int(container_count),
            "requests_per_container": float(requests_per_container),
            "p95_latency": p95_latency,
            "warn_frequency": float(warn_freq),
            "weighted_error_rate": float(weighted_error_rate),
            "z_latency": float(z_lat),
            "z_weighted_err": float(z_we),
            "z_warn": float(z_warn),
            "traffic_delta": float(traffic_delta),
            "anomaly_count": int(anomaly_count),
            "probability": ml_prob,
            "risk": effective_risk,
            "raw_risk": risk,
            "top_error_container": top_container,
            "top_error_count": int(top_error_count),
            "host_ip": top_container_host,
            "top_container_host_ip": top_container_host,
            # Hybrid framework audit fields
            "decision_source": decision_source,
            "decision_reason": decision_reason,
            "ml_triggered": ml_high,
            "stat_triggered": stat_high,
            "rule_triggered": (rule_error or rule_latency or rule_traffic),
            "sustained_triggered": rule_sustained,
            "anomaly_streak": int(self.anomaly_streak[service]),
        })

        # ====================================================================
        # RECOVERY DECISION
        # ====================================================================

        if risk == "HIGH" and not in_cooldown:
            action = self.policy_engine.decide_action(
                service=service,
                risk=risk,
                top_container=top_container,
                host_ip=top_container_host,
                traffic_delta=traffic_delta,
                decision_source=decision_source,
                decision_reason=decision_reason,
                experiment_id=exp_id,
            )

            if action in ("SCALE_UP", "TARGETED_RESTART", "GLOBAL_RESTART","ESCALATE_ON_CALL"):
                self.last_recovery_time[service] = time.time()
                logger.warning(
                    "Recovery queued: %s for %s [%s]",
                    action,
                    service.upper(),
                    decision_source
                )

        # ====================================================================
        # SCALE DOWN
        # ====================================================================

        if effective_risk == "LOW":
            self.policy_engine.maybe_scale_down(
                service,
                self.low_streak[service]
            )

        # ====================================================================
        # LOG RESULT
        # ====================================================================

        logger.info(
            "%s | risk=%-8s | P=%.3f | p95=%5.0fms | err=%.3f | "
            "anom=%d | streak=%d | src=%s | containers=%d | "
            "req/container=%.2f | traffic_delta=%.3f",
            service.upper(),
            effective_risk,
            ml_prob,
            p95_latency,
            weighted_error_rate,
            anomaly_count,
            self.anomaly_streak[service],
            decision_source,
            container_count,
            requests_per_container,
            traffic_delta
        )


# ============================================================================
# MAIN
# ============================================================================

def main():
    analyzer = PredictiveAnalyzer()

    logger.info(
        "Processor started | window=%ds | σ=%.1f | err_rule=%.0f%% | "
        "lat_rule=%.0fms | ml_thr=%.3f",
        WINDOW_SIZE_SEC,
        SENSITIVITY_K,
        ERROR_RATE_RULE_THR * 100,
        LATENCY_RULE_THR_MS,
        ml_threshold
    )

    while True:
        try:
            log_data = r.brpop("LOG_STREAM", timeout=1)

            if log_data:
                analyzer.current_window_logs.append(
                    json.loads(log_data[1])
                )

            if time.time() - analyzer.start_time >= WINDOW_SIZE_SEC:
                analyzer.process_window()

        except KeyboardInterrupt:
            logger.info("Graceful shutdown")
            break

        except Exception as e:
            logger.error("Loop error: %s", e, exc_info=True)
            time.sleep(1)


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    main()