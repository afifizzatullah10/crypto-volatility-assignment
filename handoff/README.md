# Team Handoff — BTC-USD Volatility Spike Classifier

**Version:** 1.0 | **Date:** April 2026

This folder contains everything a new team member needs to reproduce results,
run the live pipeline, or extend the model.

---

## Integration Type

**Selected-base** — standalone Python + Docker stack. No external services beyond
Coinbase public WebSocket and a local Docker Compose environment.

---

## Contents

```
handoff/
├── README.md                          ← this file
docker/
├── compose.yaml                       ← Kafka + MLflow services
└── Dockerfile.ingestor                ← containerised ingestor
docs/
├── feature_spec.md                    ← feature definitions, τ, look-ahead rules
└── model_card_v1.md                   ← model card
models/artifacts/
├── run_ids.txt                        ← MLflow run IDs for baseline + LR
data/raw/
└── slice_10min.ndjson                 ← 10-minute raw tick slice (copy manually)
data/processed/
└── features_10min.parquet             ← feature table for the slice (copy manually)
reports/
├── pr_curves_combined.png             ← PR curve comparison
├── evidently/
│   ├── drift_report.html              ← Milestone 2 drift (early vs. late)
│   └── test_vs_train_report.html      ← Milestone 3 drift (test vs. train)
predictions_test.csv                   ← model predictions on test set
.env.example                           ← environment variable template
requirements.txt                       ← pinned Python dependencies
```

---

## Quick Start (5 minutes)

### 1. Prerequisites
- Docker Desktop running
- Python 3.11 (install via `brew install python@3.11` on macOS)

### 2. Environment setup

```bash
git clone https://github.com/afifizzatullah10/crypto-volatility-assignment.git
cd crypto-volatility-assignment

/opt/homebrew/bin/python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install "setuptools<74"
pip install -r requirements.txt
```

### 3. Start services

```bash
docker compose -f docker/compose.yaml up -d
# Kafka on localhost:9092
# MLflow UI on http://localhost:5001
```

### 4. Collect live data

```bash
python scripts/ws_ingest.py --pair BTC-USD --minutes 15 --output-dir data/raw
```

### 5. Build feature table

```bash
python scripts/replay.py --raw "data/raw/*.ndjson" --out data/processed/features.parquet
```

### 6. Train models

```bash
python models/train.py
# Opens MLflow at http://localhost:5001 to see logged runs
```

### 7. Run inference

```bash
python models/infer.py --features data/processed/features.parquet --output predictions.csv
```

---

## MLflow Model Loading

```python
import mlflow.sklearn, os
os.environ["MLFLOW_TRACKING_URI"] = "http://localhost:5001"

run_id = open("models/artifacts/run_ids.txt").readlines()[1].split("=")[1].strip()
pipe = mlflow.sklearn.load_model(f"runs:/{run_id}/model")
# pipe is a sklearn Pipeline (StandardScaler + LogisticRegression)
```

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| PR-AUC as primary metric | Class imbalance (~8% positive); accuracy and ROC-AUC are misleading |
| τ at 92nd percentile | Balances rarity (~8% positives) with enough signal to train on |
| Time-based splits only | Prevents look-ahead data leakage |
| Label generated offline | Forward label requires future data; never computed at inference time |
| Logistic Regression over XGBoost | More interpretable; sufficient capacity for 6 features; fast inference |

---

## Known Limitations

- Trained on a single short data window — retrain periodically
- Performance may degrade in low-volatility regimes
- No position sizing or execution logic included
- Kafka dependency — a Kafka outage halts live feature production

---

## Contact

Course: 45-886 Responsible AI in Production — CMU Tepper  
Student: Afif Izzatullah
