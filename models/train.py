"""
models/train.py — Train a baseline z-score rule and a Logistic Regression
classifier on the feature table, log both to MLflow, and save a PR-curve
evaluation report.

Usage:
    python models/train.py
    python models/train.py --features data/processed/features.parquet --experiment volatility-m3
"""

import argparse
import os
import sys
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import mlflow
import mlflow.sklearn
import numpy as np
import pandas as pd
import scipy.stats
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    ConfusionMatrixDisplay,
    PrecisionRecallDisplay,
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

load_dotenv()
warnings.filterwarnings("ignore")

FEATURE_COLS = [
    "spread_pct",
    "return_1s",
    "rolling_vol_30s",
    "rolling_vol_60s",
    "trade_intensity",
    "bid_ask_imbalance",
]

REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)


# ── Data loading & splitting ──────────────────────────────────────────────────

def load_and_split(parquet_path: str):
    df = pd.read_parquet(parquet_path).sort_values("time").reset_index(drop=True)

    if "label" not in df.columns:
        print("[train] ERROR: 'label' column missing — run replay.py first", file=sys.stderr)
        sys.exit(1)

    # Drop rows with missing features or label
    df = df.dropna(subset=FEATURE_COLS + ["label"]).copy()

    # Time-based split — never shuffle
    n = len(df)
    train = df.iloc[: int(n * 0.70)].copy()
    val   = df.iloc[int(n * 0.70) : int(n * 0.85)].copy()
    test  = df.iloc[int(n * 0.85) :].copy()

    print(f"[train] rows  train={len(train):,}  val={len(val):,}  test={len(test):,}")
    print(f"[train] positive rate  train={train['label'].mean()*100:.1f}%  "
          f"val={val['label'].mean()*100:.1f}%  test={test['label'].mean()*100:.1f}%")
    return train, val, test


# ── Baseline: z-score rule ────────────────────────────────────────────────────

def compute_zscore_scores(df: pd.DataFrame, roll_window: int = 300) -> pd.Series:
    """Soft probability score from z-score of rolling_vol_60s."""
    rolling_mean = df["rolling_vol_60s"].rolling(roll_window, min_periods=10).mean()
    rolling_std  = df["rolling_vol_60s"].rolling(roll_window, min_periods=10).std().replace(0, np.nan)
    z = (df["rolling_vol_60s"] - rolling_mean) / rolling_std
    z = z.fillna(0.0)
    return pd.Series(scipy.stats.norm.cdf(z.values), index=df.index)


def run_baseline(train, val, test, experiment_name: str) -> str:
    """Train z-score baseline, log to MLflow, return run_id."""

    # Tune threshold_z on validation set
    val_scores = compute_zscore_scores(
        pd.concat([train, val]).reset_index(drop=True)
    ).iloc[len(train):]
    val_scores.index = val.index

    best_z, best_prauc = 2.0, 0.0
    for z in np.arange(0.5, 4.0, 0.25):
        preds = (val_scores >= scipy.stats.norm.cdf(z)).astype(int)
        pa = average_precision_score(val["label"], val_scores)
        if pa > best_prauc:
            best_prauc, best_z = pa, z

    # Evaluate on test
    all_scores = compute_zscore_scores(
        pd.concat([train, val, test]).reset_index(drop=True)
    )
    test_scores = all_scores.iloc[len(train) + len(val):]
    test_scores.index = test.index
    val_scores_final = all_scores.iloc[len(train): len(train) + len(val)]
    val_scores_final.index = val.index

    pr_auc_val  = average_precision_score(val["label"],  val_scores_final)
    pr_auc_test = average_precision_score(test["label"], test_scores)

    print(f"[baseline] threshold_z={best_z:.2f}  PR-AUC val={pr_auc_val:.4f}  test={pr_auc_test:.4f}")

    mlflow.set_experiment(experiment_name)
    with mlflow.start_run(run_name="baseline_zscore") as run:
        mlflow.set_tag("milestone", "3")
        mlflow.set_tag("model_type", "baseline_zscore")
        mlflow.log_param("threshold_z", best_z)
        mlflow.log_param("roll_window", 300)
        mlflow.log_metric("pr_auc_val",  pr_auc_val)
        mlflow.log_metric("pr_auc_test", pr_auc_test)

        # Save PR curve plot
        fig, ax = plt.subplots(figsize=(7, 5))
        PrecisionRecallDisplay.from_predictions(
            test["label"], test_scores, ax=ax, name=f"Baseline z-score (PR-AUC={pr_auc_test:.3f})"
        )
        ax.set_title("PR Curve — Baseline (z-score)")
        path = REPORTS_DIR / "pr_curve_baseline.png"
        fig.savefig(path, bbox_inches="tight")
        plt.close(fig)
        mlflow.log_artifact(str(path))

        run_id = run.info.run_id

    return run_id, pr_auc_val, pr_auc_test, test_scores


# ── ML model: Logistic Regression ────────────────────────────────────────────

def run_logistic(train, val, test, experiment_name: str):
    """Train logistic regression pipeline, log to MLflow, return run_id."""

    X_train = train[FEATURE_COLS].fillna(0)
    y_train = train["label"]
    X_val   = val[FEATURE_COLS].fillna(0)
    y_val   = val["label"]
    X_test  = test[FEATURE_COLS].fillna(0)
    y_test  = test["label"]

    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("clf",    LogisticRegression(class_weight="balanced", max_iter=1000, random_state=42)),
    ])
    pipe.fit(X_train, y_train)

    prob_val  = pipe.predict_proba(X_val)[:, 1]
    prob_test = pipe.predict_proba(X_test)[:, 1]

    pr_auc_val  = average_precision_score(y_val,  prob_val)
    pr_auc_test = average_precision_score(y_test, prob_test)

    # Optimal F1 threshold on validation
    prec, rec, thresholds = precision_recall_curve(y_val, prob_val)
    f1s = 2 * prec * rec / (prec + rec + 1e-9)
    best_thresh = float(thresholds[np.argmax(f1s[:-1])])

    print(f"[logreg]   best_thresh={best_thresh:.4f}  PR-AUC val={pr_auc_val:.4f}  test={pr_auc_test:.4f}")

    mlflow.set_experiment(experiment_name)
    with mlflow.start_run(run_name="logistic_regression_v1") as run:
        mlflow.set_tag("milestone", "3")
        mlflow.set_tag("model_type", "logistic_regression")
        mlflow.log_params(pipe.named_steps["clf"].get_params())
        mlflow.log_param("features", FEATURE_COLS)
        mlflow.log_param("best_threshold", best_thresh)
        mlflow.log_metric("pr_auc_val",  pr_auc_val)
        mlflow.log_metric("pr_auc_test", pr_auc_test)
        mlflow.sklearn.log_model(pipe, "model")

        # PR curve
        fig, ax = plt.subplots(figsize=(7, 5))
        PrecisionRecallDisplay.from_predictions(
            y_test, prob_test, ax=ax,
            name=f"Logistic Regression (PR-AUC={pr_auc_test:.3f})"
        )
        ax.set_title("PR Curve — Logistic Regression")
        path = REPORTS_DIR / "pr_curve_logreg.png"
        fig.savefig(path, bbox_inches="tight")
        plt.close(fig)
        mlflow.log_artifact(str(path))

        # Confusion matrix at optimal threshold
        preds_test = (prob_test >= best_thresh).astype(int)
        cm = confusion_matrix(y_test, preds_test)
        fig2, ax2 = plt.subplots(figsize=(5, 4))
        ConfusionMatrixDisplay(cm, display_labels=["No spike", "Spike"]).plot(ax=ax2)
        ax2.set_title(f"Confusion Matrix (thresh={best_thresh:.3f})")
        path2 = REPORTS_DIR / "confusion_matrix_logreg.png"
        fig2.savefig(path2, bbox_inches="tight")
        plt.close(fig2)
        mlflow.log_artifact(str(path2))

        run_id = run.info.run_id

    return run_id, pr_auc_val, pr_auc_test, prob_test, best_thresh


# ── Combined PR curve ─────────────────────────────────────────────────────────

def save_combined_pr_curve(test, baseline_scores, logreg_scores,
                            baseline_prauc, logreg_prauc):
    fig, ax = plt.subplots(figsize=(8, 6))
    PrecisionRecallDisplay.from_predictions(
        test["label"], baseline_scores,
        ax=ax, name=f"Baseline z-score  (PR-AUC={baseline_prauc:.3f})"
    )
    PrecisionRecallDisplay.from_predictions(
        test["label"], logreg_scores,
        ax=ax, name=f"Logistic Regression  (PR-AUC={logreg_prauc:.3f})"
    )
    # Chance line
    pos_rate = test["label"].mean()
    ax.axhline(pos_rate, linestyle="--", color="grey", lw=1,
               label=f"Chance / class prevalence ({pos_rate:.2f})")
    ax.set_title("PR Curves — Baseline vs. Logistic Regression (Test Set)", fontsize=13)
    ax.legend(fontsize=9)
    path = REPORTS_DIR / "pr_curves_combined.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"[train] combined PR curve → {path}")
    return path


# ── Evidently: test vs. train ─────────────────────────────────────────────────

def run_evidently_report(train, test):
    try:
        from evidently.metric_preset import DataDriftPreset, DataQualityPreset
        from evidently.report import Report

        report = Report(metrics=[DataDriftPreset(), DataQualityPreset()])
        report.run(
            reference_data=train[FEATURE_COLS].fillna(0).reset_index(drop=True),
            current_data=test[FEATURE_COLS].fillna(0).reset_index(drop=True),
        )
        out = Path("reports/evidently/test_vs_train_report.html")
        out.parent.mkdir(parents=True, exist_ok=True)
        report.save_html(str(out))
        print(f"[train] Evidently report → {out}")
    except Exception as e:
        print(f"[train] Evidently report skipped: {e}", file=sys.stderr)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Train baseline + LR model, log to MLflow")
    p.add_argument("--features",    default="data/processed/features.parquet")
    p.add_argument("--experiment",  default="volatility-m3")
    p.add_argument("--tracking-uri", default=None,
                   help="MLflow tracking URI (default: MLFLOW_TRACKING_URI env or local)")
    return p.parse_args()


def main():
    args = parse_args()

    tracking_uri = (
        args.tracking_uri
        or os.getenv("MLFLOW_TRACKING_URI")
        or "http://localhost:5001"
    )
    mlflow.set_tracking_uri(tracking_uri)
    print(f"[train] MLflow tracking → {tracking_uri}")

    train, val, test = load_and_split(args.features)

    # ── Baseline ──────────────────────────────────────────────────────────
    baseline_run_id, bl_prauc_val, bl_prauc_test, baseline_test_scores = \
        run_baseline(train, val, test, args.experiment)

    # ── Logistic Regression ───────────────────────────────────────────────
    lr_run_id, lr_prauc_val, lr_prauc_test, lr_test_scores, best_thresh = \
        run_logistic(train, val, test, args.experiment)

    # ── Combined PR curve ─────────────────────────────────────────────────
    save_combined_pr_curve(test, baseline_test_scores, lr_test_scores,
                            bl_prauc_test, lr_prauc_test)

    # ── Evidently test vs train ───────────────────────────────────────────
    run_evidently_report(train, test)

    # ── Summary ───────────────────────────────────────────────────────────
    winner = "Logistic Regression" if lr_prauc_test >= bl_prauc_test else "Baseline z-score"
    print(f"\n{'─'*55}")
    print(f"  {'Model':<28} {'Val PR-AUC':>10} {'Test PR-AUC':>11}")
    print(f"  {'─'*28} {'─'*10} {'─'*11}")
    print(f"  {'Baseline z-score':<28} {bl_prauc_val:>10.4f} {bl_prauc_test:>11.4f}")
    print(f"  {'Logistic Regression':<28} {lr_prauc_val:>10.4f} {lr_prauc_test:>11.4f}")
    print(f"{'─'*55}")
    print(f"  Winner: {winner}")
    print(f"\n  Baseline run ID : {baseline_run_id}")
    print(f"  LR       run ID : {lr_run_id}")
    print(f"  View at         : {tracking_uri}")
    print(f"{'─'*55}\n")

    # Save run IDs for infer.py
    Path("models/artifacts").mkdir(parents=True, exist_ok=True)
    with open("models/artifacts/run_ids.txt", "w") as f:
        f.write(f"baseline_run_id={baseline_run_id}\n")
        f.write(f"lr_run_id={lr_run_id}\n")
        f.write(f"lr_best_threshold={best_thresh}\n")


if __name__ == "__main__":
    main()
