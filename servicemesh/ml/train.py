"""Train and evaluate the provider-risk model.

Run:  python -m servicemesh.ml.train

Metric choice, and why
----------------------
The headline metric is **ROC-AUC**, with **recall on the failure class** as the
operational one. Accuracy is reported but deliberately not optimised for:
failures are the minority class, so a model that predicted "never fails" would
score roughly 75% accuracy while being useless for the one job it has.

The asymmetry matters. A false negative (we predict a provider is fine, it then
fails) costs a retry cycle, a fallback and possibly an SLA breach. A false
positive (we deprioritise a provider that would have been fine) costs slightly
worse routing, and nothing else - because the prediction only reorders
providers that already passed every hard constraint. Recall is therefore worth
more than precision here, and the operating threshold reflects that.

Baseline comparison is included so the model has to justify itself: if it does
not beat "always predict the majority class" and a single-feature rule on
historical success rate, it should not be in the system.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from servicemesh.ml.dataset import FEATURES, generate_rows

MODEL_VERSION = "provider-risk-v1"


def _load(rows_or_csv, rows: int, seed: int):
    import pandas as pd

    if rows_or_csv and Path(rows_or_csv).exists():
        return pd.read_csv(rows_or_csv)
    return pd.DataFrame(generate_rows(rows, seed))


def train(
    csv_path: str | None = None,
    rows: int = 6000,
    seed: int = 7,
    out_path: str = "artifacts/provider_risk_model.joblib",
    report_path: str = "artifacts/ml_report.json",
) -> dict:
    import joblib
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (
        accuracy_score,
        classification_report,
        confusion_matrix,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )
    from sklearn.model_selection import cross_val_score, train_test_split
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    df = _load(csv_path, rows, seed)
    X = df[FEATURES].to_numpy(dtype=float)
    y = df["failed"].to_numpy(dtype=int)

    # Stratified split preserves the failure rate in both halves; without it a
    # minority class this size can end up badly represented in the test set.
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=seed, stratify=y
    )

    candidates = {
        "logistic_regression": Pipeline([
            ("scale", StandardScaler()),
            ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
        ]),
        "random_forest": Pipeline([
            ("clf", RandomForestClassifier(
                n_estimators=300, max_depth=12, min_samples_leaf=5,
                class_weight="balanced", random_state=seed, n_jobs=-1,
            )),
        ]),
    }

    results: dict[str, dict] = {}
    fitted: dict[str, Pipeline] = {}

    for name, pipe in candidates.items():
        cv = cross_val_score(pipe, X_train, y_train, cv=5, scoring="roc_auc")
        pipe.fit(X_train, y_train)
        proba = pipe.predict_proba(X_test)[:, 1]
        pred = (proba >= 0.5).astype(int)

        results[name] = {
            "cv_roc_auc_mean": round(float(cv.mean()), 4),
            "cv_roc_auc_std": round(float(cv.std()), 4),
            "test_roc_auc": round(float(roc_auc_score(y_test, proba)), 4),
            "test_accuracy": round(float(accuracy_score(y_test, pred)), 4),
            "test_precision": round(float(precision_score(y_test, pred, zero_division=0)), 4),
            "test_recall": round(float(recall_score(y_test, pred, zero_division=0)), 4),
            "test_f1": round(float(f1_score(y_test, pred, zero_division=0)), 4),
            "confusion_matrix": confusion_matrix(y_test, pred).tolist(),
        }
        fitted[name] = pipe

    # --- baselines the model must beat to be worth including ------------
    majority = int(np.bincount(y_train).argmax())
    majority_pred = np.full_like(y_test, majority)
    # Single-feature rule: flag anything below median historical success rate.
    success_idx = FEATURES.index("historical_success_rate")
    threshold = float(np.median(X_train[:, success_idx]))
    rule_pred = (X_test[:, success_idx] < threshold).astype(int)

    baselines = {
        "majority_class": {
            "test_accuracy": round(float(accuracy_score(y_test, majority_pred)), 4),
            "test_recall": round(float(recall_score(y_test, majority_pred, zero_division=0)), 4),
            "test_roc_auc": 0.5,
        },
        "success_rate_threshold_rule": {
            "test_accuracy": round(float(accuracy_score(y_test, rule_pred)), 4),
            "test_recall": round(float(recall_score(y_test, rule_pred, zero_division=0)), 4),
            "test_roc_auc": round(float(roc_auc_score(y_test, rule_pred)), 4),
        },
    }

    best_name = max(results, key=lambda k: results[k]["test_roc_auc"])
    best = fitted[best_name]

    importances = None
    if best_name == "random_forest":
        imp = best.named_steps["clf"].feature_importances_
        importances = dict(
            sorted(
                {f: round(float(v), 4) for f, v in zip(FEATURES, imp, strict=True)}.items(),
                key=lambda kv: -kv[1],
            )
        )

    report = {
        "model_version": MODEL_VERSION,
        "data": {
            "source": "SYNTHETIC - generated by servicemesh.ml.dataset",
            "rows": int(len(df)),
            "failure_rate": round(float(y.mean()), 4),
            "features": FEATURES,
            "seed": seed,
        },
        "primary_metric": "roc_auc",
        "metric_rationale": (
            "Failures are the minority class, so accuracy rewards predicting "
            "'never fails'. ROC-AUC measures ranking quality across thresholds, "
            "which is what provider scoring actually needs, and recall on the "
            "failure class is tracked because a missed failure costs a retry "
            "cycle while a false alarm only costs slightly worse routing."
        ),
        "candidates": results,
        "baselines": baselines,
        "selected_model": best_name,
        "feature_importances": importances,
        "classification_report": classification_report(
            y_test, (best.predict_proba(X_test)[:, 1] >= 0.5).astype(int),
            target_names=["succeeded", "failed"], output_dict=True, zero_division=0,
        ),
        "beats_baselines": (
            results[best_name]["test_roc_auc"]
            > max(b["test_roc_auc"] for b in baselines.values())
        ),
    }

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": best, "features": FEATURES, "version": MODEL_VERSION}, out_path)
    Path(report_path).write_text(json.dumps(report, indent=2))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the provider-risk model")
    parser.add_argument("--csv", default=None)
    parser.add_argument("--rows", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="artifacts/provider_risk_model.joblib")
    parser.add_argument("--report", default="artifacts/ml_report.json")
    args = parser.parse_args(argv)

    report = train(args.csv, args.rows, args.seed, args.out, args.report)

    print("=" * 66)
    print("Provider-risk model  (SYNTHETIC DATA - not industry measurements)")
    print("=" * 66)
    print(f"rows={report['data']['rows']}  failure_rate={report['data']['failure_rate']:.1%}")
    print()
    for name, m in report["candidates"].items():
        mark = "*" if name == report["selected_model"] else " "
        print(f"{mark} {name:22} AUC={m['test_roc_auc']:.4f}  acc={m['test_accuracy']:.4f}  "
              f"recall={m['test_recall']:.4f}  f1={m['test_f1']:.4f}  "
              f"cv={m['cv_roc_auc_mean']:.4f}±{m['cv_roc_auc_std']:.4f}")
    print()
    for name, m in report["baselines"].items():
        print(f"  baseline {name:28} AUC={m['test_roc_auc']:.4f}  "
              f"acc={m['test_accuracy']:.4f}  recall={m['test_recall']:.4f}")
    print()
    print(f"selected  : {report['selected_model']}")
    print(f"beats base: {report['beats_baselines']}")
    if report["feature_importances"]:
        top = list(report["feature_importances"].items())[:5]
        print("top features:", ", ".join(f"{k}={v}" for k, v in top))
    print(f"\nmodel  -> {args.out}\nreport -> {args.report}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
