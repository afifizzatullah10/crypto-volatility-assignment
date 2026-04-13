/* =========================================================
   CRYPTO VOL INTEL — dashboard/app.js
   Dual-mode: static (data/dashboard.json) + live (SSE stream)
   ========================================================= */

// ── Design tokens (mirrors CSS vars for Chart.js)
const TOKENS = {
  blueprint: '#2B4CFF',
  orange:    '#FF4D00',
  green:     '#00B884',
  ink:       '#1A1A1A',
  paper:     '#F2F0E9',
  muted:     '#6B6B6B',
  grid:      '#2A2A2A',
};

const SSE_URL       = 'http://localhost:8766/stream';
const W4_API_BASE   = 'http://localhost:8000';
const CHART_MAX_PTS = 300;
const VOL_SCALE     = 10_000;   // ×10⁴ for readable axis
const W4_REPLAY_CNT = 12;
const W4_REFRESH_MS = 10_000;

// ── State
let chartInstance  = null;
let activePair     = 'BTC-USD';
let dashData       = null;
let isLive         = false;
let forcedStatic   = false;
let activeES       = null;
let liveSpikeEvents = [];
let w4Health       = null;
let w4Version      = null;
let w4StatusTimer  = null;
let lastW4LatencyMs = null;

// Per-pair live buffers
const liveBuffers = {
  'BTC-USD': { labels: [], price: [], realized: [], spikes: [] },
  'ETH-USD': { labels: [], price: [], realized: [], spikes: [] },
};
const liveState = { 'BTC-USD': {}, 'ETH-USD': {} };


// ── ─────────────────────────────────────────────────────── ──
// STATIC MODE
// ── ─────────────────────────────────────────────────────── ──

async function loadDashboard() {
  const res = await fetch('data/dashboard.json');
  if (!res.ok) throw new Error('dashboard.json not found');
  return res.json();
}

function renderTicker(data) {
  const ps = data.price_summary || {};
  const m  = data.metrics || {};
  const lr = m.logistic_regression || {};
  const bs = m.baseline || {};

  const parts = [];
  for (const [pair, info] of Object.entries(ps)) {
    const sign = (info.delta_pct ?? 0) >= 0 ? '+' : '';
    const cls  = (info.delta_pct ?? 0) >= 0 ? 'hi' : 'lo';
    parts.push(
      `<span class="ticker-item">${pair} ` +
      `$${fmtNum(info.last, 2)} ` +
      `<span class="${cls}">${sign}${(info.delta_pct ?? 0).toFixed(2)}%</span></span>`
    );
  }
  parts.push(`<span class="ticker-item"><span class="sep">|</span> PR-AUC LR ${lr.pr_auc?.toFixed(4) ?? '—'}</span>`);
  parts.push(`<span class="ticker-item"><span class="sep">|</span> PR-AUC BASELINE ${bs.pr_auc?.toFixed(4) ?? '—'}</span>`);
  parts.push(`<span class="ticker-item"><span class="sep">|</span> FEATURE ROWS ${(data.feature_rows || 0).toLocaleString()}</span>`);
  parts.push(`<span class="ticker-item"><span class="sep">|</span> LABEL RATE ${((data.label_rate || 0) * 100).toFixed(1)}%</span>`);

  // Duplicate for seamless looping
  const content = parts.join('');
  const el = document.getElementById('ticker-scroll');
  if (el) el.innerHTML = content + '&nbsp;&nbsp;&nbsp;&nbsp;' + content;
}

function renderPriceBoard(data) {
  const ps  = data.price_summary || {};
  const btc = ps['BTC-USD'] || {};
  const eth = ps['ETH-USD'] || {};
  setText('btc-price', btc.last != null ? '$' + fmtNum(btc.last, 2) : '—');
  setText('eth-price', eth.last != null ? '$' + fmtNum(eth.last, 2) : '—');
  setDelta('btc-delta', btc.delta_pct);
  setDelta('eth-delta', eth.delta_pct);

  // Session info
  const start = String(data.session_start || '').slice(0, 16).replace('T', ' ');
  const end   = String(data.session_end   || '').slice(11, 16);
  const mins  = data.session_minutes != null ? data.session_minutes.toFixed(1) + ' MIN' : '— MIN';
  const rows  = data.feature_rows ? data.feature_rows.toLocaleString() + ' TICKS' : '— TICKS';
  const dateLabel = String(data.session_start || '').slice(0, 10);
  const el = document.getElementById('price-session');
  if (el) el.innerHTML = `SESSION · ${dateLabel}<br>${start} → ${end} UTC<br>${mins} · ${rows}`;
}

function renderKPIs(data) {
  const m  = data.metrics || {};
  const lr = m.logistic_regression || {};
  setText('kpi-bars',       (data.feature_rows || 0).toLocaleString());
  setText('kpi-label-rate', data.label_rate != null ? ((data.label_rate) * 100).toFixed(1) + '%' : '—');
  setText('kpi-prauc',      lr.pr_auc != null ? lr.pr_auc.toFixed(4) : '—');
  setText('kpi-f1',         lr.f1_at_threshold != null ? lr.f1_at_threshold.toFixed(4) : '—');
  setText('kpi-split',      m.train_rows != null
    ? `${m.train_rows.toLocaleString()} / ${m.validation_rows.toLocaleString()} / ${m.test_rows.toLocaleString()}`
    : '—');
}

function renderScorecard(data) {
  const m  = data.metrics || {};
  const lr = m.logistic_regression || {};
  const bs = m.baseline || {};
  setText('b-prauc', bs.pr_auc?.toFixed(4) ?? '—');
  setText('b-f1',    bs.f1_at_threshold?.toFixed(4) ?? '—');
  setText('b-pos',   bs.positive_rate != null ? (bs.positive_rate * 100).toFixed(1) + '%' : '—');
  setText('lr-prauc', lr.pr_auc?.toFixed(4) ?? '—');
  setText('lr-f1',    lr.f1_at_threshold?.toFixed(4) ?? '—');
  setText('lr-pos',   lr.positive_rate != null ? (lr.positive_rate * 100).toFixed(1) + '%' : '—');
}

function renderDeltaBars(data) {
  const m  = data.metrics || {};
  const lr = m.logistic_regression || {};
  const bs = m.baseline || {};
  if (lr.pr_auc == null || bs.pr_auc == null) return;
  const praucDelta = (lr.pr_auc - bs.pr_auc) * 100;
  const f1Delta    = ((lr.f1_at_threshold ?? 0) - (bs.f1_at_threshold ?? 0)) * 100;
  setTimeout(() => {
    const pb = document.getElementById('delta-prauc-bar');
    const fb = document.getElementById('delta-f1-bar');
    if (pb) pb.style.width = Math.min(Math.abs(praucDelta) / 20 * 100, 100) + '%';
    if (fb) fb.style.width = Math.min(Math.abs(f1Delta)    / 20 * 100, 100) + '%';
  }, 200);
  setText('delta-prauc-val', (praucDelta >= 0 ? '+' : '') + praucDelta.toFixed(2) + ' PP');
  setText('delta-f1-val',    (f1Delta    >= 0 ? '+' : '') + f1Delta.toFixed(2)    + ' PP');
}

function renderSpikeRadar(data) {
  const status = document.getElementById('spike-status');
  const host   = document.getElementById('spike-radar-list');
  if (!status || !host) return;

  const rows = (data.recent_spikes || []).slice(0, 8);
  if (!rows.length) {
    status.textContent = 'NO ACTIVE SPIKE';
    status.className   = 'badge badge-muted';
    host.innerHTML     = `<p style="color:var(--muted);font-size:11px">No spike events exported yet.</p>`;
    return;
  }
  const latest = rows[0];
  status.textContent = `${latest.product_id || 'BTC'} SPIKE`;
  status.className   = 'badge badge-orange';
  host.innerHTML = rows.map(row => {
    const time = String(row.window_end_ts || '').slice(11, 19);
    const prob = row.logistic_probability != null ? `${(row.logistic_probability * 100).toFixed(1)}%` : '—';
    const vol  = row.realized_vol_60s != null ? row.realized_vol_60s.toExponential(2) : '—';
    return `<div class="spike-row">
      <div class="spike-dot"></div>
      <span class="spike-time">${time}</span>
      <span class="spike-pair">${row.product_id || '—'}</span>
      <span class="spike-copy">vol ${vol} · $${fmtNum(row.midprice, 2)}</span>
      <span class="spike-prob">${prob}</span>
    </div>`;
  }).join('');
}

function renderOutlook(data, pair = activePair) {
  const outlooks = data.probability_outlook || {};
  const outlook  = outlooks[pair];
  setText('outlook-pair', pair);
  if (!outlook) return;
  setText('outlook-minute-up',   fmtPct100(outlook.next_minute?.higher_turbulence));
  setText('outlook-minute-down', fmtPct100(outlook.next_minute?.calmer_conditions) + ' calmer');
  setText('outlook-hour-up',     fmtPct100(outlook.next_hour?.higher_turbulence));
  setText('outlook-hour-down',   fmtPct100(outlook.next_hour?.calmer_conditions) + ' calmer');
  setText('outlook-day-up',      fmtPct100(outlook.next_day?.higher_turbulence));
  setText('outlook-day-down',    fmtPct100(outlook.next_day?.calmer_conditions) + ' calmer');
  setText('student-summary', outlook.student_summary || '');
}

function renderMarketScenarioCard(pair, scenario, outlook) {
  const slug = pair.startsWith('BTC') ? 'btc' : 'eth';
  const bias = document.getElementById(`market-${slug}-bias`);
  if (bias) {
    bias.textContent = scenario.bias_label || 'MIXED';
    bias.className = 'badge ' + (
      scenario.bias_label === 'UP BIAS'   ? 'badge-green'  :
      scenario.bias_label === 'DOWN BIAS' ? 'badge-orange' : 'badge-muted'
    );
  }
  setText(`market-${slug}-price`, fmtPx(scenario.current_price));
  const volCopy = outlook
    ? `VOLATILITY PRESSURE: ${(outlook.next_hour.higher_turbulence * 100).toFixed(0)}% NEXT HOUR`
    : 'VOLATILITY PRESSURE: —';
  setText(`market-${slug}-vol`, volCopy);

  setText(`market-${slug}-hour-up-prob`,  fmtPct100(scenario.next_hour?.up_probability));
  setText(`market-${slug}-hour-up-move`,  scenario.next_hour?.up_move_usd != null ? `+${fmtPx(scenario.next_hour.up_move_usd)}` : '—');
  setText(`market-${slug}-hour-up-price`, scenario.next_hour?.up_target != null ? `to ${fmtPx(scenario.next_hour.up_target)}` : '—');
  setText(`market-${slug}-hour-down-prob`,  fmtPct100(scenario.next_hour?.down_probability));
  setText(`market-${slug}-hour-down-move`,  scenario.next_hour?.down_move_usd != null ? `-${fmtPx(scenario.next_hour.down_move_usd)}` : '—');
  setText(`market-${slug}-hour-down-price`, scenario.next_hour?.down_target != null ? `to ${fmtPx(scenario.next_hour.down_target)}` : '—');

  setText(`market-${slug}-day-up-prob`,  fmtPct100(scenario.next_day?.up_probability));
  setText(`market-${slug}-day-up-move`,  scenario.next_day?.up_move_usd != null ? `+${fmtPx(scenario.next_day.up_move_usd)}` : '—');
  setText(`market-${slug}-day-up-price`, scenario.next_day?.up_target != null ? `to ${fmtPx(scenario.next_day.up_target)}` : '—');
  setText(`market-${slug}-day-down-prob`,  fmtPct100(scenario.next_day?.down_probability));
  setText(`market-${slug}-day-down-move`,  scenario.next_day?.down_move_usd != null ? `-${fmtPx(scenario.next_day.down_move_usd)}` : '—');
  setText(`market-${slug}-day-down-price`, scenario.next_day?.down_target != null ? `to ${fmtPx(scenario.next_day.down_target)}` : '—');
}

function renderMarketOutlook(data) {
  const scenarios = data.price_scenarios  || {};
  const outlooks  = data.probability_outlook || {};
  for (const pair of ['BTC-USD', 'ETH-USD']) {
    if (scenarios[pair]) renderMarketScenarioCard(pair, scenarios[pair], outlooks[pair]);
  }
}

function renderPredictions(data) {
  const rows  = (data.predictions || []).slice(-20).reverse();
  const tbody = document.querySelector('#pred-table tbody');
  if (!tbody) return;
  tbody.innerHTML = rows.map(row => {
    const ts   = String(row.window_end_ts || '').slice(11, 19);
    const pair = row.product_id || '—';
    const lbl  = row.label ?? row.label_true;
    const pred = row.predicted_label ?? row.label_pred;
    const prob = row.logistic_probability ?? row.pred_prob;
    const ok   = lbl === pred;
    return `<tr>
      <td>${ts}</td>
      <td>${pair}</td>
      <td class="${lbl === 1 ? 'cell-pos' : 'cell-neg'}">${lbl === 1 ? 'SPIKE' : 'calm'}</td>
      <td class="${pred === 1 ? 'cell-pos' : 'cell-neg'}">${pred === 1 ? 'SPIKE' : 'calm'}</td>
      <td class="${prob != null && prob > 0.6 ? 'cell-high' : ''}">${prob != null ? (prob * 100).toFixed(1) + '%' : '—'}</td>
    </tr>`;
  }).join('');
}


// ── ─────────────────────────────────────────────────────── ──
// CHART (dual-axis: absolute price left, vol×10⁴ right)
// ── ─────────────────────────────────────────────────────── ──

function buildChart(pair) {
  const seriesAll = (dashData?.chart_series || {});
  const series    = seriesAll[pair] || [];
  if (!series.length) {
    // If this pair has no data, show empty chart
    _createChart(pair, [], [], [], []);
    return;
  }
  const step = Math.max(1, Math.floor(series.length / 400));
  const s    = series.filter((_, i) => i % step === 0);

  const labels = s.map(r => String(r.window_end_ts).slice(11, 19));
  const price  = s.map(r => r.midprice ?? null);
  const vol    = s.map(r => r.realized_vol_60s != null ? r.realized_vol_60s * VOL_SCALE : null);
  const spikes = s.map(r => r.predicted_spike ? (r.realized_vol_60s || 0) * VOL_SCALE : null);
  _createChart(pair, labels, price, vol, spikes);
}

function _createChart(pair, labels, price, vol, spikes) {
  if (chartInstance) { chartInstance.destroy(); chartInstance = null; }

  const pairColor = pair === 'BTC-USD' ? TOKENS.blueprint : TOKENS.green;
  const volColor  = pair === 'BTC-USD' ? '#5B7CFF'        : '#33C99A';
  const currency  = pair.split('-')[0];

  const ctx = document.getElementById('vol-chart');
  if (!ctx) return;
  Chart.defaults.font.family = '"JetBrains Mono", monospace';
  Chart.defaults.color       = TOKENS.muted;

  chartInstance = new Chart(ctx.getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: `${currency} PRICE`,
          data: price,
          yAxisID: 'yPrice',
          borderColor: pairColor,
          backgroundColor: 'transparent',
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0.15,
          order: 2,
        },
        {
          label: `${currency} VOL ×10⁻⁴`,
          data: vol,
          yAxisID: 'yVol',
          borderColor: volColor,
          backgroundColor: 'transparent',
          borderWidth: 1.5,
          borderDash: [5, 3],
          pointRadius: 0,
          pointHoverRadius: 3,
          tension: 0.2,
          order: 3,
        },
        {
          label: '⚡ SPIKE',
          data: spikes,
          yAxisID: 'yVol',
          borderColor: 'transparent',
          backgroundColor: TOKENS.orange,
          pointRadius: 5,
          pointStyle: 'circle',
          showLine: false,
          order: 1,
        },
      ],
    },
    options: _chartOptions(pair),
  });
}

function _chartOptions(pair) {
  const currency = (pair || 'BTC-USD').split('-')[0];
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 0 },
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#111111',
        borderColor: TOKENS.blueprint,
        borderWidth: 1,
        titleColor: '#F2F0E9',
        bodyColor: '#A0A0A0',
        titleFont: { family: '"JetBrains Mono", monospace', size: 11, weight: '700' },
        bodyFont:  { family: '"JetBrains Mono", monospace', size: 10 },
        padding: 10,
        callbacks: {
          label: (ctx) => {
            const v = ctx.raw;
            if (v == null) return null;
            if (ctx.datasetIndex === 0) return ` ${ctx.dataset.label}: ${fmtPx(v)}`;
            return ` ${ctx.dataset.label}: ${v.toFixed(4)} ×10⁻⁴`;
          },
        },
      },
    },
    scales: {
      x: {
        ticks: { maxTicksLimit: 8, maxRotation: 0, font: { size: 10 }, color: TOKENS.muted },
        grid:  { color: TOKENS.grid },
      },
      yPrice: {
        type: 'linear',
        position: 'left',
        ticks: {
          font: { size: 10 },
          color: TOKENS.muted,
          callback: v => '$' + Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 }),
        },
        grid: { color: TOKENS.grid },
        title: { display: true, text: `${currency} PRICE (USD)`, font: { size: 9 }, color: TOKENS.muted },
      },
      yVol: {
        type: 'linear',
        position: 'right',
        ticks: {
          font: { size: 10 },
          color: TOKENS.muted,
          callback: v => v.toFixed(3),
        },
        grid: { drawOnChartArea: false },
        title: { display: true, text: 'VOL × 10⁻⁴', font: { size: 9 }, color: TOKENS.muted },
      },
    },
  };
}

function bindPairTabs() {
  document.querySelectorAll('.pair-tabs .tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.pair-tabs .tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activePair = btn.dataset.pair;
      if (isLive) {
        rebuildLiveChart(activePair);
        renderLiveOutlook(activePair);
        renderLiveMarketOutlook();
      } else {
        buildChart(activePair);
        renderOutlook(dashData, activePair);
      }
    });
  });
}


// ── ─────────────────────────────────────────────────────── ──
// WEEK 4 API MODULE
// ── ─────────────────────────────────────────────────────── ──

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? '—';
}

function setBadge(id, label, tone) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = label;
  el.className = `badge ${tone}`;
}

function fmtLatency(ms) {
  if (ms == null || !Number.isFinite(ms)) return '—';
  return ms < 1000 ? `${ms.toFixed(1)} ms` : `${(ms / 1000).toFixed(3)} s`;
}

async function fetchJson(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

async function fetchText(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.text();
}

function parsePrometheus(text) {
  const vals = {};
  for (const line of String(text || '').split('\n')) {
    if (!line || line.startsWith('#')) continue;
    const m = line.match(/^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)$/);
    if (!m) continue;
    const [, name, , raw] = m;
    if (name.endsWith('_created')) continue;
    const v = Number(raw);
    if (!Number.isFinite(v)) continue;
    (vals[name] = vals[name] || []).push(v);
  }
  return vals;
}

function metricSum(metrics, name) {
  return (metrics[name] || []).reduce((a, b) => a + b, 0);
}

function nextCursor() {
  const total  = Number(w4Health?.replay_rows  || 0);
  const cursor = Number(w4Health?.replay_cursor || 0);
  if (!total) return 0;
  return cursor >= total ? 0 : cursor;
}

function renderW4Offline(msg) {
  setBadge('w4-api-badge', 'OFFLINE', 'badge-muted');
  ['w4-service-name','w4-service-version','w4-designation',
   'w4-replay-rows','w4-replay-cursor','w4-threshold',
   'w4-request-count','w4-pred-row-count','w4-last-latency']
    .forEach(id => setText(id, '—'));
  setText('w4-api-copy', msg || 'Week 4 API offline. Run `python scripts/run_w4_api.py`.');
}

function renderW4Status(health, version, metrics) {
  const reqCount  = metricSum(metrics, 'crypto_api_requests_total');
  const predRows  = metricSum(metrics, 'crypto_api_prediction_rows_total');
  const latCnt    = metricSum(metrics, 'crypto_api_inference_seconds_count');
  const latSum    = metricSum(metrics, 'crypto_api_inference_seconds_sum');
  const avgLatMs  = latCnt > 0 ? (latSum / latCnt) * 1000 : null;
  const latDisplay = lastW4LatencyMs ?? avgLatMs;

  setBadge('w4-api-badge', health?.model_loaded ? 'ONLINE' : 'LOADING',
           health?.model_loaded ? 'badge-green' : 'badge-muted');
  setText('w4-service-name',    health?.service   || '—');
  setText('w4-service-version', health?.version   || version?.version || '—');
  setText('w4-designation',     version?.designation || '—');
  setText('w4-replay-rows',     Number.isFinite(+health?.replay_rows)   ? (+health.replay_rows).toLocaleString()   : '—');
  setText('w4-replay-cursor',   Number.isFinite(+health?.replay_cursor) ? (+health.replay_cursor).toLocaleString() : '—');
  setText('w4-threshold',       Number.isFinite(+version?.threshold)    ? (+version.threshold).toFixed(4) : '—');
  setText('w4-request-count',   reqCount  ? reqCount.toLocaleString()  : '0');
  setText('w4-pred-row-count',  predRows  ? predRows.toLocaleString()  : '0');
  setText('w4-last-latency',    fmtLatency(latDisplay));
  setText('w4-api-copy',
    health
      ? `${Number(health.replay_rows || 0).toLocaleString()} rows loaded. Cursor at ${Number(health.replay_cursor || 0).toLocaleString()}.`
      : 'Week 4 API status unavailable.'
  );
}

function renderW4ReplayPlaceholder(msg) {
  const tb = document.querySelector('#w4-replay-table tbody');
  if (tb) tb.innerHTML = `<tr><td colspan="3" style="color:var(--muted);padding:18px 10px">${msg}</td></tr>`;
}

function renderW4ReplaySample(payload, threshold) {
  const tb = document.querySelector('#w4-replay-table tbody');
  if (!tb) return;
  const scores = payload?.scores || [];
  if (!scores.length) { renderW4ReplayPlaceholder('No replay rows returned.'); return; }

  const startIdx = payload?.replay_start_index ?? 0;
  const thresh   = threshold ?? 0.5;
  tb.innerHTML = scores.map((score, i) => {
    const isSpike  = score >= thresh;
    const scoreCls = score >= 0.6 ? 'cell-high' : '';
    const predCls  = isSpike ? 'cell-pos' : 'cell-neg';
    return `<tr>
      <td>${startIdx + i}</td>
      <td class="${scoreCls}">${(score * 100).toFixed(1)}%</td>
      <td class="${predCls}">${isSpike ? 'SPIKE' : 'CALM'}</td>
    </tr>`;
  }).join('');

  setText('w4-replay-copy',
    `Scored ${scores.length} rows (idx ${payload.replay_start_index ?? '—'}–${payload.replay_end_index ?? '—'}). ` +
    `Variant: ${payload.model_variant || '—'} · ts: ${String(payload.ts || '').slice(11, 19)} UTC`
  );

  if (w4Health && Number.isFinite(+payload?.replay_end_index)) {
    w4Health = { ...w4Health, replay_cursor: +payload.replay_end_index };
    setText('w4-replay-cursor', (+payload.replay_end_index).toLocaleString());
  }
}

async function refreshW4Status() {
  try {
    const [health, version, metricsText] = await Promise.all([
      fetchJson(`${W4_API_BASE}/health`),
      fetchJson(`${W4_API_BASE}/version`),
      fetchText(`${W4_API_BASE}/metrics`),
    ]);
    w4Health  = health;
    w4Version = version;
    renderW4Status(health, version, parsePrometheus(metricsText));
    const btn = document.getElementById('w4-launch-btn');
    if (btn) { btn.textContent = 'OPEN DOCS'; btn.classList.add('badge-green-btn'); }
    return true;
  } catch (err) {
    w4Health = null; w4Version = null;
    renderW4Offline(`Week 4 API offline: ${err.message}`);
    renderW4ReplayPlaceholder('Start the Week 4 API to load replay predictions.');
    setText('w4-replay-copy', 'Replay unavailable. Run `python scripts/run_w4_api.py`.');
    return false;
  }
}

async function loadW4ReplaySample(startIdx = nextCursor()) {
  const btn = document.getElementById('w4-replay-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'LOADING...'; }
  const t0 = Date.now();
  try {
    const payload = await fetchJson(`${W4_API_BASE}/predict`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ replay_count: W4_REPLAY_CNT, replay_start_index: startIdx }),
    });
    lastW4LatencyMs = Date.now() - t0;
    renderW4ReplaySample(payload, w4Version?.threshold);
    await refreshW4Status();
  } catch (err) {
    renderW4ReplayPlaceholder('Replay sample failed.');
    setText('w4-replay-copy', `Replay failed: ${err.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'RUN 12-ROW REPLAY'; }
  }
}

async function initW4Module() {
  const btn = document.getElementById('w4-replay-btn');
  if (btn) btn.addEventListener('click', () => loadW4ReplaySample());
  const launchBtn = document.getElementById('w4-launch-btn');
  if (launchBtn) {
    launchBtn.addEventListener('click', () => {
      if (w4Health?.model_loaded) window.open(`${W4_API_BASE}/docs`, '_blank');
      else refreshW4Status();
    });
  }
  renderW4ReplayPlaceholder('Checking Week 4 API status...');
  const online = await refreshW4Status();
  if (online) await loadW4ReplaySample(nextCursor());
  if (!w4StatusTimer) w4StatusTimer = setInterval(refreshW4Status, W4_REFRESH_MS);
}


// ── ─────────────────────────────────────────────────────── ──
// LIVE SSE MODE
// ── ─────────────────────────────────────────────────────── ──

function clamp(v, lo = 0.05, hi = 0.95) { return Math.max(lo, Math.min(hi, v)); }
function mean(arr) { return arr.length ? arr.reduce((s, v) => s + v, 0) / arr.length : 0; }
function stdDev(arr) {
  if (arr.length < 2) return 0;
  const mu = mean(arr);
  return Math.sqrt(arr.reduce((s, v) => s + (v - mu) ** 2, 0) / (arr.length - 1));
}

function buildLiveOutlook(pair) {
  const buf = liveBuffers[pair];
  const st  = liveState[pair] || {};
  const realized = buf.realized.filter(v => v != null && Number.isFinite(v));
  if (!realized.length) return null;

  const latest    = realized[realized.length - 1];
  const sorted    = [...realized].sort((a, b) => a - b);
  const rank      = sorted.findIndex(v => v >= latest);
  const volPct    = rank === -1 ? 1 : (rank + 1) / sorted.length;
  const latestProb = clamp(st.logistic_prob ?? volPct);

  const recent  = realized.slice(-60);
  const prev    = realized.slice(-120, -60);
  const pm      = Math.max(mean(prev) || mean(recent) || 1e-9, 1e-9);
  const trend   = clamp(0.5 + 0.35 * ((mean(recent) - pm) / pm));
  const spikeFrac = buf.spikes.filter(v => v != null).length / Math.max(buf.spikes.length, 1);

  const minUp  = clamp(0.85 * latestProb + 0.15 * volPct);
  const hourUp = clamp(0.55 * latestProb + 0.25 * volPct + 0.20 * trend);
  const dayUp  = clamp(0.30 * latestProb + 0.35 * volPct + 0.20 * spikeFrac + 0.15 * trend);

  return {
    pair,
    next_minute: { higher_turbulence: minUp,  calmer_conditions: 1 - minUp  },
    next_hour:   { higher_turbulence: hourUp, calmer_conditions: 1 - hourUp },
    next_day:    { higher_turbulence: dayUp,  calmer_conditions: 1 - dayUp  },
    student_summary: `${pair} shows a ${(hourUp * 100).toFixed(0)}% chance of rougher trading in the next hour and ${(dayUp * 100).toFixed(0)}% into the next day. This is a turbulence probability, not a price-direction forecast.`,
  };
}

function renderLiveOutlook(pair = activePair) {
  const outlook = buildLiveOutlook(pair);
  if (!outlook) return;
  renderOutlook({ probability_outlook: { [pair]: outlook } }, pair);
}

function buildLivePriceScenario(pair) {
  const buf = liveBuffers[pair];
  const st  = liveState[pair] || {};
  const prices = buf.price.filter(v => Number.isFinite(v) && v > 0);
  if (prices.length < 3) return null;

  const returns = [];
  for (let i = 1; i < prices.length; i++) returns.push(Math.log(prices[i] / prices[i - 1]));

  const cur   = prices[prices.length - 1];
  const realVol = st.realized_vol_60s ?? ((buf.realized[buf.realized.length - 1] || 0) / VOL_SCALE);
  const prob  = clamp(st.logistic_prob ?? 0.5);

  const short  = returns.slice(-30);
  const medium = returns.slice(-120);
  const ss  = mean(short) / Math.max(stdDev(short), 1e-8);
  const ms  = mean(medium) / Math.max(stdDev(medium), 1e-8);
  const score = 0.65 * ss + 0.35 * ms;
  const upP = clamp(0.5 + 0.22 * Math.tanh(1.75 * score), 0.25, 0.75);
  const downP = 1 - upP;

  const floorFr = h => h <= 3600 ? 0.0015 : 0.004;
  const capFr   = h => h <= 3600 ? 0.04   : 0.12;
  const move    = (h) => {
    const raw = cur * Math.max(realVol, 1e-6) * Math.sqrt(h) * (0.7 + 0.9 * prob);
    return Math.min(cur * capFr(h), Math.max(cur * floorFr(h), raw));
  };
  const hm = move(3600), dm = move(86400);
  let bias = 'MIXED';
  if (upP >= 0.57) bias = 'UP BIAS';
  if (upP <= 0.43) bias = 'DOWN BIAS';

  return {
    current_price: cur, bias_label: bias,
    next_hour: { up_probability: upP, down_probability: downP, up_move_usd: hm, down_move_usd: hm, up_target: cur + hm, down_target: cur - hm },
    next_day:  { up_probability: upP, down_probability: downP, up_move_usd: dm, down_move_usd: dm, up_target: cur + dm, down_target: cur - dm },
  };
}

function renderLiveMarketOutlook() {
  for (const pair of ['BTC-USD', 'ETH-USD']) {
    const sc = buildLivePriceScenario(pair);
    const ol = buildLiveOutlook(pair);
    if (sc) renderMarketScenarioCard(pair, sc, ol);
  }
}

function setLiveBadge(live) {
  const badge  = document.getElementById('live-badge');
  const toggle = document.getElementById('mode-toggle-btn');
  if (forcedStatic) {
    if (badge)  { badge.textContent  = '○ STATIC'; badge.className  = 'live-badge is-forced-static'; }
    if (toggle) { toggle.textContent = '○ STATIC — click to go live'; toggle.classList.add('is-static'); }
  } else if (live) {
    if (badge)  { badge.textContent  = '● LIVE'; badge.className  = 'live-badge is-live'; }
    if (toggle) { toggle.textContent = '⬤ LIVE — click to freeze'; toggle.classList.remove('is-static'); }
  } else {
    if (badge)  { badge.textContent  = '● STATIC'; badge.className  = 'live-badge'; }
    if (toggle) { toggle.textContent = '○ STATIC — click to go live'; toggle.classList.add('is-static'); }
  }
}

function switchToStatic() {
  if (activeES) { activeES.close(); activeES = null; }
  isLive = false; forcedStatic = true;
  setLiveBadge(false);
  if (dashData) { buildChart(activePair); renderOutlook(dashData, activePair); renderMarketOutlook(dashData); }
}

function switchToLive() {
  forcedStatic = false;
  setLiveBadge(false);
  trySSE().then(up => { if (up) connectSSE(); });
}

function bindModeToggle() {
  const toggle = () => { if (isLive || !forcedStatic) switchToStatic(); else switchToLive(); };
  const badge  = document.getElementById('live-badge');
  const btn    = document.getElementById('mode-toggle-btn');
  if (badge) badge.addEventListener('click', toggle);
  if (btn)   btn.addEventListener('click', toggle);
}

function pushLiveTick(event) {
  const pid = event.product_id;
  if (!pid || !(pid in liveBuffers)) return;
  liveState[pid] = event;

  const buf     = liveBuffers[pid];
  const ts      = String(event.ts || '').slice(11, 19);
  const volScaled = event.realized_vol_60s != null ? event.realized_vol_60s * VOL_SCALE : null;

  buf.labels.push(ts);
  buf.price.push(event.midprice ?? null);
  buf.realized.push(volScaled);
  buf.spikes.push(event.predicted_spike ? volScaled : null);

  if (buf.labels.length > CHART_MAX_PTS) {
    buf.labels.shift(); buf.price.shift(); buf.realized.shift(); buf.spikes.shift();
  }

  updateLiveChart();
  updateLivePrices();
  renderLiveOutlook(activePair);
  renderLiveMarketOutlook();

  if (event.predicted_spike) {
    liveSpikeEvents.unshift({
      window_end_ts: event.ts,
      product_id: pid,
      midprice: event.midprice,
      realized_vol_60s: event.realized_vol_60s,
      logistic_probability: event.logistic_prob,
    });
    liveSpikeEvents = liveSpikeEvents.slice(0, 8);
    renderSpikeRadar({ recent_spikes: liveSpikeEvents });
    flashSpike();
  }
}

function updateLiveChart() {
  if (!chartInstance) return;
  const buf = liveBuffers[activePair];
  if (!buf.labels.length) return;
  chartInstance.data.labels = buf.labels;
  chartInstance.data.datasets[0].data = buf.price;
  chartInstance.data.datasets[1].data = buf.realized;
  chartInstance.data.datasets[2].data = buf.spikes;
  chartInstance.update('none');
}

function updateLivePrices() {
  for (const [pid, st] of Object.entries(liveState)) {
    const slug = pid.startsWith('BTC') ? 'btc' : 'eth';
    if (st.midprice) {
      setText(`${slug}-price`, '$' + fmtNum(st.midprice, 2));
      const spread = st.spread_bps != null ? ` · ${st.spread_bps.toFixed(2)} bps` : '';
      const prob   = st.logistic_prob != null ? ` · prob ${(st.logistic_prob * 100).toFixed(1)}%` : '';
      const el = document.getElementById(`${slug}-delta`);
      if (el) { el.textContent = `LIVE${spread}${prob}`; el.className = 'price-delta up'; }
    }
  }

  // Live ticker
  const parts = [];
  for (const [pid, st] of Object.entries(liveState)) {
    if (!st.midprice) continue;
    const vol  = st.realized_vol_60s != null ? (st.realized_vol_60s * 1e4).toFixed(2) + '×10⁻⁴' : '—';
    const prob = st.logistic_prob != null ? (st.logistic_prob * 100).toFixed(1) + '%' : '—';
    parts.push(`<span class="ticker-item">${pid} $${fmtNum(st.midprice, 2)} vol=${vol} prob=${prob}${st.predicted_spike ? ' ⚡' : ''}</span>`);
  }
  if (parts.length) {
    const el = document.getElementById('ticker-scroll');
    if (el) el.innerHTML = parts.join('') + '&nbsp;&nbsp;&nbsp;&nbsp;' + parts.join('');
  }
}

function flashSpike() {
  const board = document.getElementById('price-board');
  if (!board) return;
  board.style.borderColor = TOKENS.orange;
  board.style.boxShadow   = `6px 6px 0px ${TOKENS.orange}`;
  setTimeout(() => { board.style.borderColor = ''; board.style.boxShadow = ''; }, 800);
}

async function trySSE() {
  return new Promise(resolve => {
    fetch('http://localhost:8766/status', { signal: AbortSignal.timeout(1500) })
      .then(r => resolve(r.ok))
      .catch(() => resolve(false));
  });
}

function connectSSE() {
  if (forcedStatic) return;
  const es = new EventSource(SSE_URL);
  activeES = es;

  es.onopen = () => {
    isLive = true;
    setLiveBadge(true);
    _createChart(activePair, [], [], [], []);
  };

  es.onmessage = (e) => {
    try { pushLiveTick(JSON.parse(e.data)); } catch { /* ignore */ }
  };

  es.onerror = () => {
    isLive = false; activeES = null;
    es.close();
    setLiveBadge(false);
    if (!forcedStatic) setTimeout(connectSSE, 10_000);
  };
}


// ── ─────────────────────────────────────────────────────── ──
// FORMAT HELPERS
// ── ─────────────────────────────────────────────────────── ──

function fmtNum(v, dec = 2) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  return Number(v).toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function fmtPx(v) {
  if (v == null) return '—';
  return '$' + fmtNum(v, 2);
}

function fmtPct100(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  return Math.round(Number(v) * 100) + '%';
}

function setDelta(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  if (val == null) { el.textContent = '—'; return; }
  el.textContent = `${val >= 0 ? '+' : ''}${val.toFixed(2)}%`;
  el.className   = 'price-delta ' + (val >= 0 ? 'up' : 'down');
}


// ── ─────────────────────────────────────────────────────── ──
// BOOT
// ── ─────────────────────────────────────────────────────── ──

async function init() {
  try {
    dashData = await loadDashboard();
    renderTicker(dashData);
    renderPriceBoard(dashData);
    renderKPIs(dashData);
    renderScorecard(dashData);
    renderDeltaBars(dashData);
    renderPredictions(dashData);
    renderSpikeRadar(dashData);
    renderOutlook(dashData, activePair);
    renderMarketOutlook(dashData);
    bindPairTabs();
    bindModeToggle();
    buildChart(activePair);
  } catch (err) {
    console.error('[CVI] Static load failed:', err);
    setText('kpi-bars', 'ERR');
    document.querySelectorAll('.kpi-value').forEach(el => { if (el.textContent === '—') el.textContent = 'N/A'; });
  }

  await initW4Module();

  const serverUp = await trySSE();
  if (serverUp) {
    connectSSE();
  } else {
    setLiveBadge(false);
    console.log('[CVI] Live server not found at :8766 — static mode');
    console.log('[CVI] To enable live: python scripts/dashboard_server.py');
  }
}

init();
