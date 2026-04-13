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

## Dashboard

The `dashboard/` folder is a self-contained static site. Open it locally with:

```bash
cd dashboard && python3 -m http.server 8080
# → http://localhost:8080
```

For live streaming (replays saved data at 10 rows/sec):

```bash
python scripts/dashboard_server.py   # SSE on :8766
```

For the Week 4 FastAPI replay panel:

```bash
python scripts/run_w4_api.py         # API on :8000, docs at :8000/docs
```

---

## Deployment

The dashboard is deployed as a static site on **Vercel**. The root of the
deployment is the `dashboard/` folder; `vercel.json` routes all traffic there.

### Update the live snapshot

`dashboard/data/dashboard.json` is the static data snapshot the deployed site
reads. Regenerate it any time you retrain the model or collect new data:

```bash
# 1. (re)train if needed
python models/train.py

# 2. score all features
python models/infer.py --output predictions_test.csv

# 3. rebuild the snapshot
python scripts/export_dashboard_json.py

# 4. commit and push — Vercel redeploys automatically
git add dashboard/data/dashboard.json
git commit -m "chore: refresh dashboard snapshot"
git push
```

### First-time Vercel setup

```bash
npm i -g vercel          # install CLI once
vercel                   # follow prompts — set root to dashboard/
vercel --prod            # promote to production
```

Or connect the GitHub repo in the Vercel dashboard and set **Root Directory**
to `dashboard`. Every push to `main` will trigger a redeployment.

---

## Repository Layout

```
crypto-volatility/
├── dashboard/
│   ├── index.html          # neobrutalist single-page app
│   ├── style.css           # design system
│   ├── app.js              # static + live SSE logic
│   └── data/
│       └── dashboard.json  # committed snapshot (Vercel reads this)
├── data/
│   ├── raw/                # NDJSON ticks from WebSocket  (gitignored)
│   └── processed/          # Parquet feature files        (gitignored)
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
├── mlruns/                    MLflow artifact store        (gitignored)
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
│   ├── replay.py              (Milestone 2)
│   ├── export_dashboard_json.py (Milestone 4)
│   ├── dashboard_server.py    (Milestone 4 — SSE)
│   └── run_w4_api.py          (Milestone 4 — FastAPI)
├── vercel.json
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
| 2 | Feature engineering, EDA, Evidently | ✅ |
| 3 | Modeling, MLflow tracking, model card | ✅ |
| 4 | Neobrutalist dashboard, live SSE, W4 API | ✅ |
| 5 | Static deployment on Vercel | ✅ |
