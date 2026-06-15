import pandas as pd
import pickle
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.metrics import classification_report, confusion_matrix

# 1. Load dataset
# Ensure column names match: [is_auth, is_order, is_payment, z_latency, z_errors, z_warn, traffic_delta, anomaly_count]
df = pd.read_csv("dataset_v2.csv")

X = df.drop("label", axis=1)
y = df["label"]

# 2. Train-test split (using stratify to handle imbalanced failure classes)
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42, stratify=y
)

# 3. Enhanced Random Forest
model = RandomForestClassifier(
    n_estimators=150,      # Slightly more trees for stability
    max_depth=10,          # Prevent overfitting to noise
    class_weight="balanced", 
    random_state=42
)

model.fit(X_train, y_train)

# 4. Detailed Evaluation
y_pred_proba = model.predict_proba(X_test)[:, 1]
y_pred_custom = (y_pred_proba >= 0.4).astype(int) # Testing your 0.4 threshold

print("--- Classification Report (at 0.4 threshold) ---")
print(classification_report(y_test, y_pred_custom))
probs = model.predict_proba(X_test)[:,1]

print("Min probability:", probs.min())
print("Max probability:", probs.max())

print("Percentiles:")
print(np.percentile(probs, [0, 10, 25, 50, 75, 90, 100]))
print("\nConfusion Matrix:")
print(confusion_matrix(y_test, y_pred_custom))
# 5. Save model with metadata
model_data = {
    "model": model,
    "features": list(X.columns),
    "threshold": 0.4,
    "trained_at": pd.Timestamp.now().isoformat()
}

with open("failure_model_v2_test.pkl", "wb") as f:
    pickle.dump(model_data, f)

print("✅ Model trained and saved with proactive threshold logic.")