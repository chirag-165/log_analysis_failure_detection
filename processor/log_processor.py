import redis
from dotenv import load_dotenv
import os
load_dotenv()  # Load environment variables from .env file
import json
import time
import statistics
import pickle
import numpy as np
import pandas as pd
import logging
from collections import defaultdict, deque
from pymongo import MongoClient
from datetime import datetime, timezone
from policy_engine import PolicyEngine

# ---------------- CONFIG ----------------
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - [%(levelname)s] - %(message)s')

r = redis.Redis(host="localhost", port=6379, decode_responses=True)

mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client["log_analysis_dashboard"]
window_collection = db["window_history"]

# Keep 24h history
window_collection.create_index("timestamp", expireAfterSeconds=86400)

MODEL_PATH = "failure_model_v2.pkl"
WINDOW_SIZE_SEC = 60
HISTORY_LIMIT = 30
SENSITIVITY_K = 2.0
EPS = 1e-6
WARMUP_WINDOWS = 3
COOLDOWN_SEC = 120

SERVICE_MAP = {
    "auth-service": [1, 0, 0],
    "order-service": [0, 1, 0],
    "payment-service": [0, 0, 1]
}

# ---------------- LOAD MODEL ----------------
with open(MODEL_PATH, "rb") as f:
    model_data = pickle.load(f)
    ml_model = model_data["model"]
    feature_names = model_data["features"]
    threshold = model_data.get("threshold", 0.4)

logging.info("✅ ML Model Loaded")


# ---------------- ANALYZER ----------------
class PredictiveAnalyzer:

    def __init__(self):
        self.current_window_logs = []
        self.window_history = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
        self.last_recovery_time = defaultdict(float)
        
        # FIX 1: Standardized variable name to match usage below
        self.policy_engine = PolicyEngine() 
        
        self.start_time = time.time()
        self.restore_history()

    # ---------------- RESTORE HISTORY ----------------
    def restore_history(self):
        for svc in SERVICE_MAP.keys():
            cursor = window_collection.find(
                {"service": svc}
            ).sort("timestamp", -1).limit(HISTORY_LIMIT)

            for doc in list(cursor)[::-1]:
                self.window_history[svc].append({
                    'latency': doc.get('p95_latency', 0),
                    'weighted_err': doc.get('weighted_error_rate', 0),
                    'warn_freq': doc.get('warn_frequency', 0),
                    'count': doc.get('request_count', 0)
                })

    # ---------------- PROCESS WINDOW ----------------
    def process_window(self):
        logs_by_service = defaultdict(list)
        for log in self.current_window_logs:
            svc = log.get("service")
            if svc in SERVICE_MAP:
                logs_by_service[svc].append(log)

        for service in SERVICE_MAP.keys():
            logs = logs_by_service.get(service, [])
            self.analyze_service(service, logs)

        self.current_window_logs = []
        self.start_time = time.time()


    # ---------------- ANALYZE SERVICE ----------------
    def analyze_service(self, service, logs):
        total = len(logs)

        if total == 0:
            logging.warning(f"⚠ {service.upper()} sent 0 logs → possible crash")
            return

        errors = sum(1 for l in logs if l.get("level") == "ERROR")
        warns = sum(1 for l in logs if l.get("level") in ["WARN", "WARNING"])

        warn_freq = warns / total
        weighted_error_rate = (errors + 0.3 * warns) / total

        latencies = [l.get("response_time", 0) for l in logs]
        p95_latency = np.percentile(latencies, 95) if latencies else 0.0

        container_errors = defaultdict(int)
        for l in logs:
            if l.get("level") == "ERROR":
                cid = l.get("container_id", "unknown")
                container_errors[cid] += 1

        top_container = None
        top_error_count = 0
        if container_errors:
            top_container = max(container_errors, key=container_errors.get)
            top_error_count = container_errors[top_container]

        hist = list(self.window_history[service])

        if len(hist) < WARMUP_WINDOWS:
            self.window_history[service].append({
                'latency': p95_latency,
                'weighted_err': weighted_error_rate,
                'warn_freq': warn_freq,
                'count': total
            })
            logging.info(f"{service.upper()} warming up...")
            return

        # ---------------- Z-SCORE ----------------
        def compute_z(val, key):
            values = [h[key] for h in hist]
            mean = statistics.mean(values)
            std = max(statistics.stdev(values), EPS)
            z = (val - mean) / std
            anomaly = z > SENSITIVITY_K
            return z, anomaly

        z_lat, lat_anom = compute_z(p95_latency, 'latency')
        z_we, we_anom = compute_z(weighted_error_rate, 'weighted_err')
        z_warn, warn_anom = compute_z(warn_freq, 'warn_freq')

        prev_count = hist[-1]['count']
        traffic_delta = (total - prev_count) / max(prev_count, 1)
        traffic_anom = abs(traffic_delta) > 1.5

        anomaly_count = sum([lat_anom, we_anom, warn_anom, traffic_anom])

        # ---------------- FEATURE VECTOR ----------------
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

        # FIX 2: Used pandas DataFrame to resolve Scikit-Learn feature names warning
        feature_df = pd.DataFrame([feature_dict])
        
        # Ensure the columns match the exact order the model was trained on
        # (Missing columns will be padded with NaN, which RF can't handle, so we ensure exact match)
        for col in feature_names:
            if col not in feature_df.columns:
                feature_df[col] = 0.0
        feature_df = feature_df[feature_names]

        prob = ml_model.predict_proba(feature_df)[0][1]

        # Hybrid risk
        if prob >= threshold or anomaly_count >= 2:
            risk = "HIGH"
        elif prob > 0.2:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        # Cooldown
        if time.time() - self.last_recovery_time[service] < COOLDOWN_SEC:
            risk = "COOLDOWN"

        # ---------------- STORE ----------------
        # FIX 3 & 4: Cast numpy variables to native python types (int, float) 
        # to prevent MongoDB "Loop Error", and use timezone-aware datetime.
        window_collection.insert_one({
            "service": service,
            "timestamp": datetime.now(timezone.utc),
            "request_count": int(total),
            "p95_latency": float(p95_latency),
            "warn_frequency": float(warn_freq),
            "weighted_error_rate": float(weighted_error_rate),
            "z_latency": float(z_lat),
            "z_weighted_err": float(z_we),
            "z_warn": float(z_warn),
            "traffic_delta": float(traffic_delta),
            "anomaly_count": int(anomaly_count),
            "probability": float(prob),
            "risk": risk,
            "top_error_container": top_container,
            "top_error_count": int(top_error_count) if top_error_count else 0
        })

        self.window_history[service].append({
            'latency': float(p95_latency),
            'weighted_err': float(weighted_error_rate),
            'warn_freq': float(warn_freq),
            'count': int(total)
        })

        # ---------------- RECOVERY ENGINE ----------------
        if risk == "HIGH":
            if time.time() - self.last_recovery_time[service] > COOLDOWN_SEC:
                # Call to the cleanly separated policy engine
                action = self.policy_engine.decide_action(
                    service, risk, top_container, float(traffic_delta)
                )
                if action in ["SCALE_UP", "TARGETED_RESTART", "GLOBAL_RESTART"]:
                    self.last_recovery_time[service] = time.time()

        logging.info(
            f"{service.upper()} | Risk={risk} | P={prob:.2f} | "
            f"p95={p95_latency:.1f}ms | Anom={anomaly_count} | "
            f"TopContainer={top_container}"
        )

# ---------------- MAIN ----------------
if __name__ == "__main__":
    analyzer = PredictiveAnalyzer()
    logging.info("🚀 Predictive System Running")

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
            break
        except Exception as e:
            logging.error(f"Loop Error: {e}")
            time.sleep(1)