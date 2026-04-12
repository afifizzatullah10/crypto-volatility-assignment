# Crypto Volatility Detector

Real-time volatility spike classifier for BTC-USD using Coinbase WebSocket data,
Apache Kafka, scikit-learn, MLflow, and Evidently.

**Course:** 45-886 Responsible AI in Production — CMU Tepper

---

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Copy env template

```bash
cp .env.example .env   # edit if Kafka/MLflow are not on localhost
```

### 3. Start Kafka + MLflow

```bash
docker compose -f docker/compose.yaml up -d
```

### 4. Ingest live ticks (15 minutes, save to disk)

```bash
python scripts/ws_ingest.py --pair BTC-USD --minutes 15 --output-dir data/raw
```

### 5. Validate Kafka messages

```bash
python scripts/kafka_consume_check.py --topic ticks.raw --min 100
```

### 6. Build ingestor image (optional)

```bash
docker build -f docker/Dockerfile.ingestor -t ws-ingestor .
```

---

## Repository Layout

```
crypto-volatility/
├── data/
│   ├── raw/            # NDJSON ticks from WebSocket
│   └── processed/      # Parquet feature files
├── docker/
│   ├── compose.yaml
│   └── Dockerfile.ingestor
├── docs/
│   ├── scoping_brief.md
│   ├── feature_spec.md        (Milestone 2)
│   ├── model_card_v1.md       (Milestone 3)
│   └── genai_appendix.md      (Milestone 3)
├── features/
│   └── featurizer.py          (Milestone 2)
├── handoff/                   (Milestone 3)
├── mlruns/                    MLflow artifact store
├── models/
│   ├── train.py               (Milestone 3)
│   ├── infer.py               (Milestone 3)
│   └── artifacts/
├── notebooks/
│   └── eda.ipynb              (Milestone 2)
├── reports/
│   └── evidently/
├── scripts/
│   ├── ws_ingest.py
│   ├── kafka_consume_check.py
│   └── replay.py              (Milestone 2)
├── config.yaml
├── requirements.txt
├── .env.example
└── README.md
```

---

## Prediction Goal

Classify whether 60-second forward realized volatility of BTC-USD mid-price
returns exceeds threshold τ (set at ~92nd percentile of training data).

See `docs/scoping_brief.md` for full problem statement.

---

## Milestones

| Milestone | Goal | Status |
|---|---|---|
| 1 | Streaming setup, Kafka, scoping | ✅ |
| 2 | Feature engineering, EDA, Evidently | ⬜ |
| 3 | Modeling, MLflow tracking, model card | ⬜ |
