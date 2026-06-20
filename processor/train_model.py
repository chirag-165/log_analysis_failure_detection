"""
train_model.py

Trains the Random Forest failure-prediction model with:
  - Stratified K-Fold cross-validation (not a single lucky train/test split)
  - Threshold selection via the precision-recall curve, optimizing for
    recall (a missed failure costs more than a false-alarm restart)
  - Honest reporting of feature importances and known dataset limitations

KNOWN DATASET LIMITATION (read before retraining on a new dataset):
This dataset (dataset_v2.csv) contains only z-scored features, no raw
percentages (e.g. no raw weighted_error_rate column). The live system's
hybrid risk logic in log_processor.py applies a rule override directly on
RAW weighted_error_rate (>0.20 -> HIGH) BEFORE consulting this model -
that override exists specifically because this dataset can't teach the
model to reason about raw rates directly, only about deviation from a
rolling baseline. If you regenerate the dataset from real fault-injection
runs, include the raw weighted_error_rate, raw p95_latency, and raw
warn_frequency as additional columns alongside the z-scores - that will
let a future model subsume the rule override instead of needing it as a
separate safety net.
"""

import pickle
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.metrics import (
    classification_report,
    confusion_matrix,
    precision_recall_curve,
    f1_score,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)

DATASET_PATH = "dataset_v2.csv"
MODEL_OUTPUT_PATH = "failure_model_v2.pkl"
MIN_RECALL_TARGET = 0.90  # we'd rather over-trigger restarts than miss a real failure
RANDOM_STATE = 42


def load_data(path):
    df = pd.read_csv(path)
    if "label" not in df.columns:
        raise ValueError("Dataset must contain a 'label' column")
    X = df.drop("label", axis=1)
    y = df["label"]
    logger.info("Loaded %d rows, %d features", len(df), X.shape[1])
    logger.info("Label balance: %s", y.value_counts(normalize=True).to_dict())
    return X, y


def cross_validate(model, X, y, n_splits=5):
    """
    Stratified K-Fold instead of a single train/test split - confirms the
    model's performance is stable and not an artifact of one lucky split.
    """
    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=RANDOM_STATE)
    f1_scores = cross_val_score(model, X, y, cv=skf, scoring="f1")
    recall_scores = cross_val_score(model, X, y, cv=skf, scoring="recall")

    logger.info("Cross-validation (%d folds):", n_splits)
    logger.info("  F1:     mean=%.3f  std=%.3f  folds=%s", f1_scores.mean(), f1_scores.std(), np.round(f1_scores, 3))
    logger.info("  Recall: mean=%.3f  std=%.3f  folds=%s", recall_scores.mean(), recall_scores.std(), np.round(recall_scores, 3))
    return f1_scores, recall_scores


def select_threshold(model, X_val, y_val, min_recall=MIN_RECALL_TARGET):
    """
    Picks the highest-precision threshold that still achieves at least
    min_recall on the validation set. For a failure-prediction system,
    a missed failure (false negative) is far costlier than an unnecessary
    restart (false positive), so we deliberately favor recall over raw F1.
    """
    probs = model.predict_proba(X_val)[:, 1]
    precision, recall, thresholds = precision_recall_curve(y_val, probs)

    # precision_recall_curve returns thresholds of length len(precision)-1
    candidates = [
        (t, p, r)
        for p, r, t in zip(precision[:-1], recall[:-1], thresholds)
        if r >= min_recall
    ]

    if not candidates:
        logger.warning(
            "No threshold achieves recall >= %.2f, falling back to 0.4", min_recall
        )
        return 0.4

    # Among thresholds meeting the recall floor, pick the one with best precision
    best_threshold, best_precision, best_recall = max(candidates, key=lambda c: c[1])
    logger.info(
        "Selected threshold=%.3f -> precision=%.3f, recall=%.3f (target recall >= %.2f)",
        best_threshold, best_precision, best_recall, min_recall,
    )
    return float(best_threshold)


def evaluate(model, X_test, y_test, threshold):
    probs = model.predict_proba(X_test)[:, 1]
    preds = (probs >= threshold).astype(int)

    logger.info("=== Held-out test set evaluation (threshold=%.3f) ===", threshold)
    print(classification_report(y_test, preds))
    print("Confusion matrix:")
    print(confusion_matrix(y_test, preds))
    print()
    print("Probability distribution (percentiles 0/10/25/50/75/90/100):")
    print(np.percentile(probs, [0, 10, 25, 50, 75, 90, 100]))


def report_feature_importance(model, feature_names):
    logger.info("=== Feature importances ===")
    ranked = sorted(zip(feature_names, model.feature_importances_), key=lambda x: -x[1])
    for name, imp in ranked:
        logger.info("  %-20s %.3f", name, imp)


def main():
    X, y = load_data(DATASET_PATH)

    model = RandomForestClassifier(
        n_estimators=150,
        max_depth=10,
        class_weight="balanced",
        random_state=RANDOM_STATE,
    )

    # 1. Cross-validate on the FULL dataset first to confirm stability
    cross_validate(model, X, y, n_splits=5)

    # 2. Split off a true held-out test set the model never sees during
    #    training or threshold selection, for an honest final report.
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=RANDOM_STATE, stratify=y
    )
    # Further split training data to get a validation set for threshold
    # tuning, so the test set stays untouched until the very last step.
    X_fit, X_val, y_fit, y_val = train_test_split(
        X_train, y_train, test_size=0.2, random_state=RANDOM_STATE, stratify=y_train
    )

    model.fit(X_fit, y_fit)

    # 3. Tune threshold on validation set (NOT test set, to avoid leakage)
    threshold = select_threshold(model, X_val, y_val)

    # 4. Refit on all training data (fit + val) before final evaluation,
    #    now that the threshold is locked in.
    model.fit(X_train, y_train)
    evaluate(model, X_test, y_test, threshold)
    report_feature_importance(model, list(X.columns))

    # 5. Save model + metadata
    model_data = {
        "model": model,
        "features": list(X.columns),
        "threshold": threshold,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "dataset": DATASET_PATH,
        "notes": (
            "Trained on z-scored features only; no raw rate columns available "
            "in this dataset version. Live system applies a raw-rate rule "
            "override alongside this model's probability - see log_processor.py."
        ),
    }

    with open(MODEL_OUTPUT_PATH, "wb") as f:
        pickle.dump(model_data, f)

    logger.info("Model saved to %s (threshold=%.3f)", MODEL_OUTPUT_PATH, threshold)


if __name__ == "__main__":
    main()