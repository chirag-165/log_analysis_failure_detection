import numpy as np
import pandas as pd
import random

np.random.seed(42)

rows = []

for _ in range(15000):

    service = random.choice(["auth", "order", "payment"])

    is_auth = 1 if service == "auth" else 0
    is_order = 1 if service == "order" else 0
    is_payment = 1 if service == "payment" else 0

    scenario = np.random.choice(
        ["healthy", "mild", "partial", "failure"],
        p=[0.55, 0.20, 0.15, 0.10]
    )

    # ---------------- HEALTHY ----------------
    if scenario == "healthy":
        z_latency = np.random.normal(0.3, 0.7)
        z_errors = np.random.normal(0.1, 0.5)
        z_warns = np.random.normal(0.2, 0.5)
        traffic_delta = np.random.normal(0, 0.3)
        anomaly_count = np.random.choice([0,1], p=[0.8,0.2])
        label = 0

    # ---------------- MILD ANOMALY ----------------
    elif scenario == "mild":
        z_latency = np.random.normal(1.5, 0.8)
        z_errors = np.random.normal(1.0, 0.7)
        z_warns = np.random.normal(2.0, 1.0)
        traffic_delta = np.random.normal(0.5, 0.6)
        anomaly_count = np.random.choice([1,2], p=[0.7,0.3])
        label = np.random.choice([0,1], p=[0.6,0.4])  # overlap

    # ---------------- PARTIAL DEGRADATION ----------------
    elif scenario == "partial":
        z_latency = np.random.normal(2.5, 1.0)
        z_errors = np.random.normal(2.5, 1.2)
        z_warns = np.random.normal(3.0, 1.5)
        traffic_delta = np.random.normal(1.0, 0.8)
        anomaly_count = np.random.choice([1,2,3], p=[0.3,0.4,0.3])
        label = np.random.choice([0,1], p=[0.3,0.7])  # overlap

    # ---------------- SEVERE FAILURE ----------------
    else:
        z_latency = np.random.normal(4.0, 1.2)
        z_errors = np.random.normal(4.5, 1.5)
        z_warns = np.random.normal(5.0, 1.5)
        traffic_delta = np.random.normal(-1.0, 0.7)
        anomaly_count = np.random.choice([2,3,4], p=[0.3,0.4,0.3])
        label = 1

    rows.append([
        is_auth,
        is_order,
        is_payment,
        z_latency,
        z_errors,
        z_warns,
        traffic_delta,
        anomaly_count,
        label
    ])

df = pd.DataFrame(rows, columns=[
    "is_auth",
    "is_order",
    "is_payment",
    "z_latency",
    "z_errors",
    "z_warns",
    "traffic_delta",
    "anomaly_count",
    "label"
])

df.to_csv("dataset_v2.csv", index=False)

print("Realistic dataset generated successfully.")