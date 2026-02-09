import redis
import json
import time
import statistics
from collections import defaultdict

# ---------------- CONFIG ----------------
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_KEY = "LOG_STREAM"

WINDOW_SIZE_SEC = 60      # 1-minute window
MIN_HISTORY_WINDOWS = 3  # needed for anomaly baseline

# ---------------- REDIS CONNECTION ----------------
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

print("✅ Python Log Processor started")

# ---------------- IN-MEMORY STORAGE ----------------
current_window_logs = []
window_history = defaultdict(list)  # service -> list of window metrics

current_window_start = int(time.time())

# ---------------- METRIC CALCULATION ----------------
def compute_metrics(logs):
    total = len(logs)
    errors = sum(1 for l in logs if l["level"] == "ERROR")

    avg_rt = sum(l["response_time"] for l in logs) / total if total > 0 else 0
    std_rt = statistics.stdev([l["response_time"] for l in logs]) if total > 1 else 0

    return {
        "request_count": total,
        "error_count": errors,
        "error_rate": errors / total if total > 0 else 0,
        "avg_response_time": round(avg_rt, 2),
        "std_response_time": round(std_rt, 2)
    }

# ---------------- ANOMALY DETECTION ----------------
def detect_anomaly(service, current_metrics):
    history = window_history[service]

    if len(history) < MIN_HISTORY_WINDOWS:
        return []

    anomalies = []

    error_rates = [h["error_rate"] for h in history]
    avg_rts = [h["avg_response_time"] for h in history]
    req_counts = [h["request_count"] for h in history]

    # Error rate spike
    if current_metrics["error_rate"] > statistics.mean(error_rates) + 2 * statistics.stdev(error_rates):
        anomalies.append("ERROR_RATE_SPIKE")

    # Latency spike
    if current_metrics["avg_response_time"] > statistics.mean(avg_rts) + 2 * statistics.stdev(avg_rts):
        anomalies.append("LATENCY_SPIKE")

    # Traffic drop
    if current_metrics["request_count"] < 0.5 * statistics.mean(req_counts):
        anomalies.append("TRAFFIC_DROP")

    return anomalies

# ---------------- MAIN LOOP ----------------
while True:
    # Pop logs from Redis
    log_data = r.rpop(REDIS_KEY)

    if log_data:
        log = json.loads(log_data)
        current_window_logs.append(log)

    now = int(time.time())

    # Check if window ended
    if now - current_window_start >= WINDOW_SIZE_SEC:
        print("\n🪟 New Window Processed")

        # Group logs by service
        logs_by_service = defaultdict(list)
        for log in current_window_logs:
            logs_by_service[log["service"]].append(log)

        for service, logs in logs_by_service.items():
            metrics = compute_metrics(logs)
            anomalies = detect_anomaly(service, metrics)

            # Save history
            window_history[service].append(metrics)

            print(f"\nService: {service}")
            print("Metrics:", metrics)

            if anomalies:
                print("🚨 Anomalies:", anomalies)
            else:
                print("✅ No anomalies")

        # Reset window
        current_window_logs.clear()
        current_window_start = now

    time.sleep(0.1)