"""
log_processor.py

Reads logs from Redis, aggregates into 60s windows per service, computes
statistical anomaly features + ML failure probability, stores results in
MongoDB, and asks the PolicyEngine to decide (NOT execute) a recovery action
when risk is HIGH.

HOST_IP CACHING:
Every log entry is expected to carry a "host_ip" field (set by the log
generator / stamped by the Collector based on the real TCP source - see
note below). For each service we track the most recently seen host_ip in
self.last_known_host. If a container crashes hard enough to stop emitting
logs entirely in a window, there's nothing fresh to read an IP from, so we
fall back to the last known value rather than having no target at all.

TRUST NOTE: this design trusts host_ip as reported in the log payload. For
this project's scope (controlled environment) that's acceptable. In a
hardened version, the Collector should stamp host_ip itself from the actual
socket peer address rather than trusting a self-reported field, so a
compromised service can't lie about where it's running.
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

# ---------------- CONFIG ----------------
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s"
)
logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
MONGO_URI = os.getenv("MONGO_URI")
MODEL_PATH = os.getenv("MODEL_PATH", "failure_model_v2.pkl")

WINDOW_SIZE_SEC = 60
HISTORY_LIMIT = 30
SENSITIVITY_K = 2.0
EPS = 1e-6
WARMUP_WINDOWS = 3
COOLDOWN_SEC = 120
MEDIUM_RISK_PROB_THRESHOLD = 0.2

SERVICE_MAP = {
    "auth-service": [1, 0, 0],
    "order-service": [0, 1, 0],
    "payment-service": [0, 0, 1],
}

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["predictive_system"]
window_collection = db["window_history"]
window_collection.create_index("timestamp", expireAfterSeconds=86400)

# ---------------- LOAD MODEL ----------------
with open(MODEL_PATH, "rb") as f:
    model_data = pickle.load(f)
    ml_model = model_data["model"]
    feature_names = model_data["features"]
    ml_threshold = model_data.get("threshold", 0.4)

logger.info("ML model loaded from %s (threshold=%.2f)", MODEL_PATH, ml_threshold)


class PredictiveAnalyzer:
    def __init__(self):
        self.current_window_logs = []
        self.window_history = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
        self.last_recovery_time = defaultdict(float)
        # service -> most recently seen host_ip, used as fallback when a
        # window has zero logs (container crashed) so we still know where
        # to send a restart/scale request.
        self.last_known_host = defaultdict(lambda: None)
        self.policy_engine = PolicyEngine(mongo_uri=MONGO_URI)
        self.start_time = time.time()
        self.restore_history()

    def restore_history(self):
        for svc in SERVICE_MAP.keys():
            cursor = (
                window_collection.find({"service": svc})
                .sort("timestamp", -1)
                .limit(HISTORY_LIMIT)
            )
            docs = list(cursor)[::-1]
            for doc in docs:
                self.window_history[svc].append(
                    {
                        "latency": doc.get("p95_latency", 0),
                        "weighted_err": doc.get("weighted_error_rate", 0),
                        "warn_freq": doc.get("warn_frequency", 0),
                        "count": doc.get("request_count", 0),
                    }
                )
            if docs and docs[-1].get("host_ip"):
                self.last_known_host[svc] = docs[-1]["host_ip"]

    def process_window(self):
        logs_by_service = defaultdict(list)
        for log in self.current_window_logs:
            svc = log.get("service")
            if svc in SERVICE_MAP:
                logs_by_service[svc].append(log)

        for service in SERVICE_MAP.keys():
            self.analyze_service(service, logs_by_service.get(service, []))

        self.current_window_logs = []
        self.start_time = time.time()

    def analyze_service(self, service, logs):
        total = len(logs)
        if total == 0:
            # Container produced no logs this window - likely crashed.
            # We still have a last_known_host to target, so don't lose it.
            logger.warning(
                "%s sent 0 logs this window -> possible crash (last known host: %s)",
                service.upper(), self.last_known_host[service],
            )
            return

        # Update last known host from this window's freshest data.
        # Last log entry in the window is the most recent observation.
        host_ip = logs[-1].get("host_ip") or self.last_known_host[service]
        if host_ip:
            self.last_known_host[service] = host_ip

        errors = sum(1 for l in logs if l.get("level") == "ERROR")
        warns = sum(1 for l in logs if l.get("level") in ("WARN", "WARNING"))

        warn_freq = warns / total
        weighted_error_rate = (errors + 0.3 * warns) / total

        latencies = [l.get("response_time", 0) for l in logs]
        p95_latency = float(np.percentile(latencies, 95)) if latencies else 0.0

        # Track per-container error counts AND the host_ip each container
        # was reported on, so we know exactly where to send a targeted
        # restart for the worst-offending container.
        container_errors = defaultdict(int)
        container_host = {}
        for l in logs:
            if l.get("level") == "ERROR":
                cid = l.get("container_id", "unknown")
                container_errors[cid] += 1
                if l.get("host_ip"):
                    container_host[cid] = l["host_ip"]

        top_container, top_error_count, top_container_host = None, 0, host_ip
        if container_errors:
            top_container = max(container_errors, key=container_errors.get)
            top_error_count = container_errors[top_container]
            top_container_host = container_host.get(top_container, host_ip)

        hist = list(self.window_history[service])
        if len(hist) < WARMUP_WINDOWS:
            self.window_history[service].append(
                {
                    "latency": p95_latency,
                    "weighted_err": weighted_error_rate,
                    "warn_freq": warn_freq,
                    "count": total,
                }
            )
            logger.info("%s warming up (%d/%d windows)", service.upper(), len(hist) + 1, WARMUP_WINDOWS)
            return

        def compute_z(val, key):
            values = [h[key] for h in hist]
            mean = statistics.mean(values)
            std = max(statistics.stdev(values), EPS)
            z = (val - mean) / std
            return z, z > SENSITIVITY_K

        z_lat, lat_anom = compute_z(p95_latency, "latency")
        z_we, we_anom = compute_z(weighted_error_rate, "weighted_err")
        z_warn, warn_anom = compute_z(warn_freq, "warn_freq")

        prev_count = hist[-1]["count"]
        traffic_delta = (total - prev_count) / max(prev_count, 1)
        traffic_anom = abs(traffic_delta) > 1.5

        anomaly_count = sum([lat_anom, we_anom, warn_anom, traffic_anom])

        one_hot = SERVICE_MAP[service]
        feature_dict = {
            "is_auth-service": one_hot[0],
            "is_order-service": one_hot[1],
            "is_payment-service": one_hot[2],
            "z_latency": z_lat,
            "z_errors": z_we,
            "z_warns": z_warn,
            "traffic_delta": traffic_delta,
            "anomaly_count": anomaly_count,
        }

        feature_df = pd.DataFrame([feature_dict])
        for col in feature_names:
            if col not in feature_df.columns:
                feature_df[col] = 0.0
        feature_df = feature_df[feature_names]

        prob = float(ml_model.predict_proba(feature_df)[0][1])

        # ---------------- HYBRID RISK (rule override + ML) ----------------
        if weighted_error_rate > 0.20 or prob >= ml_threshold or anomaly_count >= 2:
            risk = "HIGH"
        elif prob > MEDIUM_RISK_PROB_THRESHOLD or anomaly_count == 1:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        in_cooldown = time.time() - self.last_recovery_time[service] < COOLDOWN_SEC
        effective_risk = "COOLDOWN" if (risk == "HIGH" and in_cooldown) else risk

        window_collection.insert_one(
            {
                "service": service,
                "timestamp": datetime.now(timezone.utc),
                "request_count": int(total),
                "p95_latency": p95_latency,
                "warn_frequency": float(warn_freq),
                "weighted_error_rate": float(weighted_error_rate),
                "z_latency": float(z_lat),
                "z_weighted_err": float(z_we),
                "z_warn": float(z_warn),
                "traffic_delta": float(traffic_delta),
                "anomaly_count": int(anomaly_count),
                "probability": prob,
                "risk": effective_risk,
                "raw_risk": risk,
                "top_error_container": top_container,
                "top_error_count": int(top_error_count),
                "host_ip": host_ip,
                "top_container_host_ip": top_container_host,
            }
        )

        self.window_history[service].append(
            {
                "latency": p95_latency,
                "weighted_err": weighted_error_rate,
                "warn_freq": warn_freq,
                "count": total,
            }
        )

        # ---------------- DECIDE (not execute) ----------------
        if risk == "HIGH" and not in_cooldown:
            action = self.policy_engine.decide_action(
                service, risk, top_container, top_container_host, traffic_delta
            )
            if action in ("SCALE_UP", "TARGETED_RESTART", "GLOBAL_RESTART"):
                self.last_recovery_time[service] = time.time()
                logger.warning(
                    "Recovery action queued: %s for %s @ %s",
                    action, service.upper(), top_container_host,
                )

        logger.info(
            "%s | risk=%s | P=%.2f | p95=%.1fms | err=%.2f | anomalies=%d | top_container=%s @ %s",
            service.upper(), effective_risk, prob, p95_latency, weighted_error_rate,
            anomaly_count, top_container, top_container_host,
        )


def main():
    analyzer = PredictiveAnalyzer()
    logger.info("Predictive system running")

    while True:
        try:
            log_data = r.brpop("LOG_STREAM", timeout=1)
            if log_data:
                analyzer.current_window_logs.append(json.loads(log_data[1]))

            if time.time() - analyzer.start_time >= WINDOW_SIZE_SEC:
                analyzer.process_window()

        except KeyboardInterrupt:
            logger.info("Shutting down")
            break
        except Exception as e:
            logger.error("Loop error: %s", e, exc_info=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
