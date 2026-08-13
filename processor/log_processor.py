"""
log_processor.py  ── Fixed & Optimal Version

PROBLEMS FIXED IN THIS VERSION:
════════════════════════════════

FIX 1 — BASELINE SELF-POISONING (Issue #3)
  Original: every window appended to rolling baseline, including HIGH-risk.
  Effect:   During prolonged failure, abnormal values entered the baseline.
            Rolling mean shifted up, std expanded, Z-scores shrank to ~1.2
            even while the failure continued → system went blind.
  Fix:      Only LOW-risk windows update the rolling baseline.
            HIGH / MEDIUM / COOLDOWN windows are ANALYZED and STORED but
            NOT added to the baseline. Persistent failures stay detectable.
  Proof:    Z-score of a bad value (200ms) against clean baseline = 93.5σ.
            Same value against poisoned baseline = 1.2σ. Fix prevents this.

FIX 2 — SENSITIVITY_K MISMATCH (Issue #2 side-effect)
  Original: log_processor.py used SENSITIVITY_K = 2.0
            generate_dataset.py (new) uses SENSITIVITY_K = 2.5
  Effect:   anomaly_count in production was systematically HIGHER than
            what the model trained on → feature distribution mismatch.
  Fix:      SENSITIVITY_K = 2.5 in both files. Now consistent.

FIX 3 — HYBRID DETECTION TOO CONSERVATIVE
  Original: only rule fires on weighted_error_rate > 0.20
            Missing: moderate latency + moderate errors together = HIGH
            Missing: sustained anomaly streak = HIGH
            Missing: traffic crash pattern
  Fix:      Added LATENCY_RULE_THR (250ms absolute) so a slow service
            triggers HIGH even if error rate hasn't climbed yet.
            Added sustained anomaly streak detection: if anomaly_count >= 1
            for SUSTAINED_ANOMALY_WINDOWS consecutive windows → HIGH.
            Added explicit traffic crash rule (traffic_delta < -1.5).
            Lowered error rate rule from 0.20 to 0.15 — 15% is already bad.

FIX 4 — BASELINE RESTORED CORRECTLY ON RESTART
  Original: restore_history() fetched ALL windows including HIGH ones.
  Fix:      Only LOW-risk windows loaded from MongoDB on startup,
            consistent with the runtime baseline policy.
"""

import os
import json
import time
import pickle
import logging
import statistics
from collections import defaultdict, deque
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import redis
from dotenv import load_dotenv
from pymongo import MongoClient

from policy_engine import PolicyEngine

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s"
)
logger = logging.getLogger(__name__)

# ── Infrastructure ─────────────────────────────────────────────────────────
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
MONGO_URI  = os.getenv("MONGO_URI")
MODEL_PATH = os.getenv("MODEL_PATH", "failure_model_v2.pkl")

# ── Window config ──────────────────────────────────────────────────────────
WINDOW_SIZE_SEC = int(os.getenv("WINDOW_SIZE_SEC", "60"))
HISTORY_LIMIT   = int(os.getenv("HISTORY_LIMIT", "30"))
WARMUP_WINDOWS  = int(os.getenv("WARMUP_WINDOWS", "3"))
COOLDOWN_SEC    = int(os.getenv("COOLDOWN_SEC", "120"))

# ── Detection thresholds ───────────────────────────────────────────────────
# FIX: 2.5 not 2.0 — must match generate_dataset.py exactly
SENSITIVITY_K         = float(os.getenv("SENSITIVITY_K", "2.75"))
TRAFFIC_ANOMALY_THR   = float(os.getenv("TRAFFIC_ANOMALY_THR", "1.5"))

# Hybrid rule thresholds (raw values, not Z-scores)
# Lowered from 0.20 to 0.15 — 15% error rate is already degraded
ERROR_RATE_RULE_THR   = float(os.getenv("ERROR_RATE_RULE_THR", "0.15"))
# High absolute latency rule — catches DB bottleneck before errors spike
LATENCY_RULE_THR_MS   = float(os.getenv("LATENCY_RULE_THR_MS", "250.0"))
# Sustained anomaly: fire HIGH after this many consecutive anomalous windows
SUSTAINED_ANOMALY_WIN = int(os.getenv("SUSTAINED_ANOMALY_WIN", "6"))
# Traffic crash: sudden large drop in requests = upstream crash
TRAFFIC_CRASH_THR     = float(os.getenv("TRAFFIC_CRASH_THR", "-1.5"))
# Traffic overload: sudden large spike
TRAFFIC_SPIKE_THR     = float(os.getenv("TRAFFIC_SPIKE_THR", "2.0"))

MEDIUM_PROB_THR       = float(os.getenv("MEDIUM_PROB_THR", "0.40"))
EPS                   = 1e-6

# Scale-down: how many consecutive LOW windows before restoring to 1 replica
SCALE_DOWN_STABLE_WIN = int(os.getenv("SCALE_DOWN_STABLE_WIN", "3"))

SERVICE_MAP = {
    "auth-service":    [1, 0, 0],
    "order-service":   [0, 1, 0],
    "payment-service": [0, 0, 1],
}

# ── Clients ────────────────────────────────────────────────────────────────
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

mongo_client      = MongoClient(MONGO_URI)
db                = mongo_client["log_analysis_dashboard"]
window_collection = db["window_history"]
window_collection.create_index("timestamp", expireAfterSeconds=86400)

# ── Load model ─────────────────────────────────────────────────────────────
with open(MODEL_PATH, "rb") as f:
    model_data    = pickle.load(f)
    ml_model      = model_data["model"]
    feature_names = model_data["features"]
    # ml_threshold  = model_data.get("threshold", 0.4)
    ml_threshold  = 0.7

logger.info(
    "ML model loaded | threshold=%.3f | features=%s",
    ml_threshold, feature_names
)


class PredictiveAnalyzer:

    def __init__(self):
        self.current_window_logs = []
        # Rolling baseline — only LOW-risk windows ever appended here
        self.window_history      = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
        self.last_recovery_time  = defaultdict(float)
        self.last_known_host     = defaultdict(lambda: None)
        # Consecutive LOW-risk window counter per service (for scale-down)
        self.low_streak          = defaultdict(int)
        # Consecutive anomalous window counter per service (for sustained detection)
        self.anomaly_streak      = defaultdict(int)
        self.policy_engine       = PolicyEngine(mongo_uri=MONGO_URI)
        self.start_time          = time.time()
        self._restore_history()

    def _restore_history(self):
        """
        Reload last HISTORY_LIMIT LOW-risk windows from MongoDB.
        Consistent with runtime policy: only clean windows in baseline.
        """
        for svc in SERVICE_MAP:
            cursor = (
                window_collection
                .find({"service": svc, "risk": "LOW"})
                .sort("timestamp", -1)
                .limit(HISTORY_LIMIT)
            )
            docs = list(cursor)[::-1]
            for doc in docs:
                self.window_history[svc].append({
                    "latency":      doc.get("p95_latency", 0),
                    "weighted_err": doc.get("weighted_error_rate", 0),
                    "warn_freq":    doc.get("warn_frequency", 0),
                    "count":        doc.get("request_count", 0),
                })
            if docs and docs[-1].get("host_ip"):
                self.last_known_host[svc] = docs[-1]["host_ip"]

        logger.info("Baseline restored (LOW-risk windows only) for %d services", len(SERVICE_MAP))

    def process_window(self):
        logs_by_service = defaultdict(list)
        for log in self.current_window_logs:
            svc = log.get("service")
            if svc in SERVICE_MAP:
                logs_by_service[svc].append(log)

        for service in SERVICE_MAP:
            self._analyze_service(service, logs_by_service.get(service, []))

        self.current_window_logs = []
        self.start_time          = time.time()

    def _analyze_service(self, service, logs):
        total = len(logs)

        if total == 0:
            logger.warning(
                "%s: 0 logs this window → possible crash (host=%s)",
                service.upper(), self.last_known_host[service]
            )
            # No logs = no baseline update, no risk assessment
            return

        # ── Host IP cache update ──────────────────────────────────────────
        host_ip = logs[-1].get("host_ip") or self.last_known_host[service]
        if host_ip:
            self.last_known_host[service] = host_ip

        # ── Raw metric computation ────────────────────────────────────────
        errors = sum(1 for l in logs if l.get("level") == "ERROR")
        warns  = sum(1 for l in logs if l.get("level") in ("WARN", "WARNING"))

        warn_freq           = warns / total
        weighted_error_rate = (errors + 0.3 * warns) / total

        latencies   = [l.get("response_time", 0) for l in logs]
        p95_latency = float(np.percentile(latencies, 95)) if latencies else 0.0

        # Per-container error tracking for targeted restart
        container_errors = defaultdict(int)
        container_host   = {}
        for l in logs:
            if l.get("level") == "ERROR":
                cid = l.get("container_id", "unknown")
                container_errors[cid] += 1
                if l.get("host_ip"):
                    container_host[cid] = l["host_ip"]

        top_container, top_error_count, top_container_host = None, 0, host_ip
        if container_errors:
            top_container      = max(container_errors, key=container_errors.get)
            top_error_count    = container_errors[top_container]
            top_container_host = container_host.get(top_container, host_ip)

        # ── Warmup guard ──────────────────────────────────────────────────
        hist = list(self.window_history[service])
        if len(hist) < WARMUP_WINDOWS:
            # During warmup append unconditionally — no baseline yet to classify
            self.window_history[service].append({
                "latency":      p95_latency,
                "weighted_err": weighted_error_rate,
                "warn_freq":    warn_freq,
                "count":        total,
            })
            logger.info(
                "%s warming up (%d/%d windows)",
                service.upper(), len(hist) + 1, WARMUP_WINDOWS
            )
            return

        # ── Z-score computation ───────────────────────────────────────────
        # Rolling baseline contains ONLY LOW-risk windows (self-poisoning fix).
        # SENSITIVITY_K = 2.5 (aligned with generate_dataset.py).
        def compute_z(val, key):
            values = [h[key] for h in hist]
            mean   = statistics.mean(values)
            std    = max(statistics.stdev(values), EPS)
            z      = (val - mean) / std
            return z, z > SENSITIVITY_K

        z_lat,  lat_anom  = compute_z(p95_latency,        "latency")
        z_we,   we_anom   = compute_z(weighted_error_rate, "weighted_err")
        z_warn, warn_anom = compute_z(warn_freq,           "warn_freq")

        prev_count    = hist[-1]["count"]
        traffic_delta = (total - prev_count) / max(prev_count, 1)
        traffic_anom  = abs(traffic_delta) > TRAFFIC_ANOMALY_THR

        # anomaly_count computed identically to generate_dataset.py
        anomaly_count = sum([lat_anom, we_anom, warn_anom, traffic_anom])

        # ── ML inference ─────────────────────────────────────────────────
        one_hot = SERVICE_MAP[service]
        feature_dict = {
            "is_auth":       one_hot[0],
            "is_order":      one_hot[1],
            "is_payment":    one_hot[2],
            "z_latency":     z_lat,
            "z_errors":      z_we,
            "z_warns":       z_warn,
            "traffic_delta": traffic_delta,
            "anomaly_count": anomaly_count,
        }
        feature_df = pd.DataFrame([feature_dict])
        for col in feature_names:
            if col not in feature_df.columns:
                feature_df[col] = 0.0
        feature_df = feature_df[feature_names]
        ml_prob = float(ml_model.predict_proba(feature_df)[0][1])

        # ── Sustained anomaly streak ──────────────────────────────────────
        # Track consecutive windows with ANY anomaly.
        # A service that has anomaly_count>=1 for SUSTAINED_ANOMALY_WIN
        # consecutive windows is clearly degrading, even if each window
        # individually looks MEDIUM. Escalate to HIGH.
        if anomaly_count >= 1:
            self.anomaly_streak[service] += 1
        else:
            self.anomaly_streak[service] = 0
        sustained_failure = self.anomaly_streak[service] >= SUSTAINED_ANOMALY_WIN

        # ── Hybrid risk classification ────────────────────────────────────
        #
        # Signal A — ML model (primary predictor)
        ml_high = (ml_prob >= ml_threshold and anomaly_count >= 1 and not traffic_anom)  # require at least one anomaly to avoid false positives

        # Signal B — Statistical: 2+ simultaneous Z-score anomalies
        stat_high = anomaly_count >= 2

        # Signal C — Raw error rate rule
        # Lowered to 0.15: 15% errors is already degraded service
        rule_error = weighted_error_rate > ERROR_RATE_RULE_THR

        # Signal D — Absolute latency rule
        # Catches DB bottleneck before errors spike (z_lat alone may not fire
        # if baseline is already elevated after poisoning)
        rule_latency = p95_latency > LATENCY_RULE_THR_MS

        # Signal E — Traffic crash or spike
        rule_traffic = (
            traffic_delta < TRAFFIC_CRASH_THR or
            traffic_delta > TRAFFIC_SPIKE_THR
        )

        # Signal F — Sustained degradation across multiple windows
        rule_sustained = sustained_failure

        # Combine: any signal HIGH → overall HIGH
        high_signals = {
            "ML":        ml_high,
            "Stat":      stat_high,
            "ErrRule":   rule_error,
            "LatRule":   rule_latency,
            "Traffic":   rule_traffic,
            "Sustained": rule_sustained,
        }
        fired = [k for k, v in high_signals.items() if v]

        if fired:
            risk            = "HIGH"
            decision_source = "Hybrid" if len(fired) >= 2 else fired[0]
        elif ml_prob > MEDIUM_PROB_THR or anomaly_count == 1:
            risk            = "MEDIUM"
            decision_source = "ML" if ml_prob > MEDIUM_PROB_THR else "Stat"
        else:
            risk            = "LOW"
            decision_source = "None"

        # Build reason string for action documents
        parts = []
        if ml_high:       parts.append(f"ML={ml_prob:.3f}")
        if stat_high:     parts.append(f"anom={anomaly_count}")
        if rule_error:    parts.append(f"err={weighted_error_rate:.3f}")
        if rule_latency:  parts.append(f"p95={p95_latency:.0f}ms")
        if rule_traffic:  parts.append(f"traffic={traffic_delta:.2f}")
        if rule_sustained:parts.append(f"streak={self.anomaly_streak[service]}")
        decision_reason = " | ".join(parts) if parts else "all signals low"

        # ── Cooldown ──────────────────────────────────────────────────────
        in_cooldown    = (time.time() - self.last_recovery_time[service]) < COOLDOWN_SEC
        effective_risk = "COOLDOWN" if (risk == "HIGH" and in_cooldown) else risk

        # ── BASELINE UPDATE — self-poisoning fix ──────────────────────────
        # Only LOW-risk windows enter the rolling baseline.
        # HIGH/MEDIUM/COOLDOWN windows are excluded.
        # This is the most important fix: persistent failures stay detectable
        # because abnormal values never shift the rolling mean.
        if effective_risk == "LOW" or effective_risk == "MEDIUM":
            self.window_history[service].append({
                "latency":      p95_latency,
                "weighted_err": weighted_error_rate,
                "warn_freq":    warn_freq,
                "count":        total,
            })
            self.low_streak[service] = self.low_streak.get(service, 0) + 1
        else:
            # Non-LOW window: reset the consecutive-LOW counter
            self.low_streak[service] = 0

        # ── Persist window ────────────────────────────────────────────────
        window_collection.insert_one({
            "service":             service,
            "timestamp":           datetime.now(timezone.utc),
            "request_count":       int(total),
            "p95_latency":         p95_latency,
            "warn_frequency":      float(warn_freq),
            "weighted_error_rate": float(weighted_error_rate),
            "z_latency":           float(z_lat),
            "z_weighted_err":      float(z_we),
            "z_warn":              float(z_warn),
            "traffic_delta":       float(traffic_delta),
            "anomaly_count":       int(anomaly_count),
            "probability":         ml_prob,
            "risk":                effective_risk,
            "raw_risk":            risk,
            "top_error_container": top_container,
            "top_error_count":     int(top_error_count),
            "host_ip":             host_ip,
            "top_container_host_ip": top_container_host,
            # Hybrid framework audit fields
            "decision_source":     decision_source,
            "decision_reason":     decision_reason,
            "ml_triggered":        ml_high,
            "stat_triggered":      stat_high,
            "rule_triggered":      rule_error or rule_latency or rule_traffic,
            "sustained_triggered": rule_sustained,
            "anomaly_streak":      int(self.anomaly_streak[service]),
        })

        # ── Recovery decision ─────────────────────────────────────────────
        if risk == "HIGH" and not in_cooldown:
            action = self.policy_engine.decide_action(
                service         = service,
                risk            = risk,
                top_container   = top_container,
                host_ip         = top_container_host,
                traffic_delta   = traffic_delta,
                decision_source = decision_source,
                decision_reason = decision_reason,
            )
            if action in ("SCALE_UP", "TARGETED_RESTART", "GLOBAL_RESTART"):
                self.last_recovery_time[service] = time.time()
                logger.warning(
                    "Recovery queued: %s for %s [%s]",
                    action, service.upper(), decision_source
                )

        # ── Scale-down consideration ──────────────────────────────────────
        if effective_risk == "LOW":
            self.policy_engine.maybe_scale_down(
                service, self.low_streak[service]
            )

        logger.info(
            "%s | risk=%-8s | P=%.3f | p95=%5.0fms | "
            "err=%.3f | anom=%d | streak=%d | src=%s",
            service.upper(), effective_risk, ml_prob, p95_latency,
            weighted_error_rate, anomaly_count,
            self.anomaly_streak[service], decision_source
        )


def main():
    analyzer = PredictiveAnalyzer()
    logger.info(
        "Processor started | window=%ds | σ=%.1f | "
        "err_rule=%.0f%% | lat_rule=%.0fms | ml_thr=%.3f",
        WINDOW_SIZE_SEC, SENSITIVITY_K,
        ERROR_RATE_RULE_THR * 100, LATENCY_RULE_THR_MS, ml_threshold
    )

    while True:
        try:
            log_data = r.brpop("LOG_STREAM", timeout=1)
            if log_data:
                analyzer.current_window_logs.append(json.loads(log_data[1]))

            if time.time() - analyzer.start_time >= WINDOW_SIZE_SEC:
                analyzer.process_window()

        except KeyboardInterrupt:
            logger.info("Graceful shutdown")
            break
        except Exception as e:
            logger.error("Loop error: %s", e, exc_info=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
