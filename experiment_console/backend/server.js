/**
 * experiment_console/backend/server.js
 *
 * Express.js backend for the Experiment / Fault Injection Console.
 * Serves:
 *   - /api/experiments/*  → fault injection management
 *   - /api/evaluate/*     → quantitative evaluation
 *   - /health             → liveness probe
 *   - /*                  → React static frontend (built into ./public)
 *
 * Connects to the SAME MongoDB database as the existing system:
 *   log_analysis_dashboard
 *
 * New collection used:
 *   fault_experiments
 *
 * Existing collections read (never modified):
 *   window_history, recovery_actions
 */

import express from 'express';
import { MongoClient } from 'mongodb';
import axios from 'axios';
import cors from 'cors';
import path from 'path';
import { fileURLToPath } from 'url';
import dotenv from 'dotenv';

dotenv.config();

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();

app.use(cors());
app.use(express.json());

// Serve built React frontend from ./public
app.use(express.static(path.join(__dirname, 'public')));

// ─── Configuration ──────────────────────────────────────────────────────────

const MONGO_URI = process.env.MONGO_URI;
const PORT = parseInt(process.env.PORT || '6060', 10);
const MAX_DURATION_SEC = 300;   // 5-minute safety cap on any experiment
const MAX_INTENSITY = 0.95;     // cap error rate at 95%
const ML_THRESHOLD = 0.7;       // must match log_processor.py

// Docker service names → internal ports (all services use 5002 internally)
const SERVICE_URLS = {
  'auth-service':    process.env.AUTH_SERVICE_URL    || 'http://auth-service:5002',
  'order-service':   process.env.ORDER_SERVICE_URL   || 'http://order-service:5002',
  'payment-service': process.env.PAYMENT_SERVICE_URL || 'http://payment-service:5002',
};

const VALID_SERVICES = Object.keys(SERVICE_URLS);
const VALID_FAULT_TYPES = [
  'NORMAL', 'ERROR_INJECTION', 'LATENCY_SPIKE',
  'TRAFFIC_SPIKE', 'SERVICE_FAILURE',
];

// ─── MongoDB ─────────────────────────────────────────────────────────────────

const mongoClient = new MongoClient(MONGO_URI);
await mongoClient.connect();
console.log('✅ MongoDB connected: log_analysis_dashboard');

const db                  = mongoClient.db('log_analysis_dashboard');
const faultExperimentsCol = db.collection('fault_experiments');
const windowHistoryCol    = db.collection('window_history');
const recoveryActionsCol  = db.collection('recovery_actions');

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Safely stringify MongoDB documents (ObjectId → string) */
function sanitize(doc) {
  if (!doc) return null;
  const d = { ...doc };
  if (d._id) d._id = d._id.toString();
  return d;
}

const LOCAL_FALLBACK_URLS = {
  'auth-service':    'http://localhost:5002',
  'order-service':   'http://localhost:5003',
  'payment-service': 'http://localhost:5004',
};

/** Push a fault config to a running service container */
async function pushFaultToService(service, config) {
  const urls = [
    `${SERVICE_URLS[service]}/fault/start`,
    `${LOCAL_FALLBACK_URLS[service]}/fault/start`
  ];
  let lastErr;
  for (const url of urls) {
    try {
      const resp = await axios.post(url, config, { timeout: 3000 });
      return resp.data;
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr;
}

/** Clear fault state on a service container */
async function clearFaultOnService(service) {
  const urls = [
    `${SERVICE_URLS[service]}/fault/stop`,
    `${LOCAL_FALLBACK_URLS[service]}/fault/stop`
  ];
  for (const url of urls) {
    try {
      await axios.post(url, {}, { timeout: 3000 });
      return;
    } catch {
      // try next
    }
  }
  console.warn(`⚠️  Could not clear fault on ${service}`);
}

// ─── Experiment Routes ───────────────────────────────────────────────────────

/**
 * POST /api/experiments/start
 * Body: { service, fault_type, intensity, duration_sec, latency_ms? }
 */
app.post('/api/experiments/start', async (req, res) => {
  try {
    const {
      service,
      fault_type   = 'NORMAL',
      intensity    = 0.0,
      duration_sec = 60,
      latency_ms   = 0,
    } = req.body;

    if (!VALID_SERVICES.includes(service)) {
      return res.status(400).json({ error: `Invalid service. Choose: ${VALID_SERVICES.join(', ')}` });
    }
    if (!VALID_FAULT_TYPES.includes(fault_type)) {
      return res.status(400).json({ error: `Invalid fault_type. Choose: ${VALID_FAULT_TYPES.join(', ')}` });
    }
    if (duration_sec > MAX_DURATION_SEC) {
      return res.status(400).json({ error: `duration_sec exceeds max (${MAX_DURATION_SEC}s)` });
    }
    if (fault_type === 'ERROR_INJECTION' && intensity > MAX_INTENSITY) {
      return res.status(400).json({ error: `intensity exceeds max (${MAX_INTENSITY * 100}%)` });
    }

    const existing = await faultExperimentsCol.findOne({ service, status: 'RUNNING' });
    if (existing) {
      return res.status(409).json({
        error: `An experiment is already running on ${service}. Stop it first.`,
        experiment_id: existing.experiment_id,
      });
    }

    const experiment_id    = `exp-${service.replace('-service', '')}-${Date.now()}`;
    const start_time       = new Date();
    const planned_end_time = new Date(start_time.getTime() + duration_sec * 1000);

    const doc = {
      experiment_id,
      service,
      fault_type,
      intensity,
      latency_ms,
      duration_sec,
      start_time,
      planned_end_time,
      actual_end_time: null,
      status: 'RUNNING',
      expected_failure: fault_type !== 'NORMAL',
      notes: '',
    };

    await faultExperimentsCol.insertOne(doc);

    if (fault_type !== 'NORMAL') {
      try {
        await pushFaultToService(service, { experiment_id, fault_type, intensity, latency_ms, duration_sec });
      } catch (err) {
        await faultExperimentsCol.updateOne(
          { experiment_id },
          { $set: { status: 'FAILED', actual_end_time: new Date(), notes: err.message } }
        );
        return res.status(502).json({ error: `Could not reach service: ${err.message}` });
      }
    }

    // Server-side safety auto-stop
    setTimeout(async () => {
      const current = await faultExperimentsCol.findOne({ experiment_id, status: 'RUNNING' });
      if (current) {
        await faultExperimentsCol.updateOne(
          { experiment_id },
          { $set: { status: 'COMPLETED', actual_end_time: new Date() } }
        );
        await clearFaultOnService(service);
        console.log(`⏰ Auto-completed experiment ${experiment_id}`);
      }
    }, duration_sec * 1000);

    res.status(201).json(sanitize(doc));
  } catch (err) {
    console.error('Start experiment error:', err);
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/experiments/:id/stop', async (req, res) => {
  try {
    const { id } = req.params;
    const doc = await faultExperimentsCol.findOne({ experiment_id: id });
    if (!doc) return res.status(404).json({ error: 'Experiment not found' });
    if (doc.status !== 'RUNNING') {
      return res.status(400).json({ error: `Experiment is not running (status: ${doc.status})` });
    }

    await clearFaultOnService(doc.service);
    const actual_end_time = new Date();
    await faultExperimentsCol.updateOne(
      { experiment_id: id },
      { $set: { status: 'STOPPED', actual_end_time } }
    );

    res.json({ ...sanitize(doc), status: 'STOPPED', actual_end_time });
  } catch (err) {
    console.error('Stop experiment error:', err);
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/experiments/active', async (req, res) => {
  try {
    const docs = await faultExperimentsCol.find({ status: 'RUNNING' }).toArray();
    res.json(docs.map(sanitize));
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/experiments', async (req, res) => {
  try {
    const docs = await faultExperimentsCol
      .find({})
      .sort({ start_time: -1 })
      .limit(50)
      .toArray();
    res.json(docs.map(sanitize));
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/experiments/:id', async (req, res) => {
  try {
    const doc = await faultExperimentsCol.findOne({ experiment_id: req.params.id });
    if (!doc) return res.status(404).json({ error: 'Not found' });
    res.json(sanitize(doc));
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/service/:service/state', async (req, res) => {
  const { service } = req.params;
  if (!VALID_SERVICES.includes(service)) {
    return res.status(400).json({ error: 'Invalid service' });
  }
  try {
    const resp = await axios.get(`${SERVICE_URLS[service]}/inject/state`, { timeout: 3000 });
    res.json(resp.data);
  } catch (err) {
    res.status(502).json({ error: `Cannot reach ${service}: ${err.message}` });
  }
});

// ─── Evaluation Helpers ──────────────────────────────────────────────────────

/**
 * Ground truth: window.timestamp ∈ [experiment.start_time, experiment.end_time]
 *               AND experiment.expected_failure === true
 * Prediction:   whatever detectedFn(window) returns
 */
function computeDetectionMetrics(windows, experiment, detectedFn) {
  const startTime = new Date(experiment.start_time);
  const endTime   = new Date(experiment.actual_end_time || experiment.planned_end_time);

  let tp = 0, fp = 0, tn = 0, fn = 0;

  for (const w of windows) {
    const t = new Date(w.timestamp);
    const inFaultPeriod = t >= startTime && t <= endTime && experiment.expected_failure;
    const detected = detectedFn(w);

    if      ( inFaultPeriod &&  detected) tp++;
    else if (!inFaultPeriod &&  detected) fp++;
    else if (!inFaultPeriod && !detected) tn++;
    else if ( inFaultPeriod && !detected) fn++;
  }

  const precision = tp + fp > 0 ? tp / (tp + fp) : 0;
  const recall    = tp + fn > 0 ? tp / (tp + fn) : 0;
  const f1        = precision + recall > 0 ? 2 * precision * recall / (precision + recall) : 0;
  const accuracy  = tp + fp + tn + fn > 0 ? (tp + tn) / (tp + fp + tn + fn) : 0;
  const fpr       = fp + tn > 0 ? fp / (fp + tn) : 0;

  return { tp, fp, tn, fn, precision, recall, f1, accuracy, fpr };
}

/**
 * Detection Latency = first detection timestamp − experiment.start_time
 */
function computeDetectionLatency(windows, experiment, detectedFn) {
  const startTime = new Date(experiment.start_time);
  const endTime   = new Date(experiment.actual_end_time || experiment.planned_end_time);

  const faultWindows = windows
    .filter(w => { const t = new Date(w.timestamp); return t >= startTime && t <= endTime; })
    .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));

  const first = faultWindows.find(w => detectedFn(w));
  if (!first) return { detected: false, latency_sec: null, first_detection_time: null };

  const latencySec = Math.max(0, Math.round((new Date(first.timestamp) - startTime) / 1000));
  return {
    detected: true,
    latency_sec: latencySec,
    first_detection_time: first.timestamp,
  };
}

function computePredictionStats(windows, experiment) {
  const startTime = new Date(experiment.start_time);
  const endTime   = new Date(experiment.actual_end_time || experiment.planned_end_time);
  const fw = windows.filter(w => { const t = new Date(w.timestamp); return t >= startTime && t <= endTime; });
  if (fw.length === 0) return { min: null, max: null, avg: null, count: 0 };
  const probs = fw.map(w => w.probability || 0);
  return {
    min:   +Math.min(...probs).toFixed(4),
    max:   +Math.max(...probs).toFixed(4),
    avg:   +(probs.reduce((a, b) => a + b, 0) / probs.length).toFixed(4),
    count: fw.length,
  };
}

/**
 * MTTR = time from fault injection start → first successful recovery action completion
 */
function computeMTTR(recoveryActions, experiment) {
  const startTime = new Date(experiment.start_time);

  // Filter actions relevant to this experiment window
  const expActions = recoveryActions.filter(a => {
    if (a.experiment_id === experiment.experiment_id) return true;
    const createdAt = new Date(a.created_at);
    return createdAt >= startTime;
  });

  const successful = expActions
    .filter(a => a.status === 'SUCCESS' && a.executed_at)
    .sort((a, b) => new Date(a.executed_at) - new Date(b.executed_at));

  const actionCounts = {};
  for (const a of expActions) {
    actionCounts[a.action] = (actionCounts[a.action] || 0) + 1;
  }

  if (successful.length === 0) {
    return {
      triggered: expActions.length > 0,
      action: expActions[0]?.action || null,
      status: expActions[0]?.status || null,
      executed_at: null,
      mttr_sec: null,
      recovery_latency_sec: null,
      success_count: 0,
      total_count: expActions.length,
      action_counts: actionCounts,
    };
  }

  const first   = successful[0];
  const mttrSec = Math.max(0, Math.round((new Date(first.executed_at) - startTime) / 1000));

  return {
    triggered: true,
    action: first.action,
    status: first.status,
    executed_at: first.executed_at,
    mttr_sec: mttrSec,
    recovery_latency_sec: mttrSec,
    success_count: successful.length,
    total_count: expActions.length,
    action_counts: actionCounts,
  };
}

// ─── Evaluation Route ────────────────────────────────────────────────────────

app.get('/api/evaluate/:id', async (req, res) => {
  try {
    const experiment = await faultExperimentsCol.findOne({ experiment_id: req.params.id });
    if (!experiment) return res.status(404).json({ error: 'Experiment not found' });

    const endTime    = new Date(experiment.actual_end_time || experiment.planned_end_time);
    const queryStart = new Date(new Date(experiment.start_time).getTime() - 5 * 60 * 1000);
    const queryEnd   = new Date(endTime.getTime() + 5 * 60 * 1000);

    const [windows, recoveryActions] = await Promise.all([
      windowHistoryCol
        .find({ service: experiment.service, timestamp: { $gte: queryStart, $lte: queryEnd } })
        .sort({ timestamp: 1 })
        .toArray(),
      recoveryActionsCol
        .find({ service: experiment.service, created_at: { $gte: queryStart, $lte: queryEnd } })
        .sort({ created_at: 1 })
        .toArray(),
    ]);

    const hybridDetect = w => w.risk === 'HIGH' || w.risk === 'COOLDOWN';

    const detection        = computeDetectionMetrics(windows, experiment, hybridDetect);
    const detectionLatency = computeDetectionLatency(windows, experiment, hybridDetect);
    const predictionStats  = computePredictionStats(windows, experiment);
    const healing          = computeMTTR(recoveryActions, experiment);

    const baselines = [
      {
        method: 'Simple Threshold',
        description: 'weighted_error_rate > 10%',
        ...computeDetectionMetrics(windows, experiment, w => (w.weighted_error_rate || 0) > 0.10),
        ...computeDetectionLatency(windows, experiment, w => (w.weighted_error_rate || 0) > 0.10),
      },
      {
        method: 'Statistical Anomaly',
        description: 'anomaly_count ≥ 2',
        ...computeDetectionMetrics(windows, experiment, w => (w.anomaly_count || 0) >= 2),
        ...computeDetectionLatency(windows, experiment, w => (w.anomaly_count || 0) >= 2),
      },
      {
        method: 'ML Only',
        description: `P(failure) ≥ ${ML_THRESHOLD}`,
        ...computeDetectionMetrics(windows, experiment, w => (w.probability || 0) >= ML_THRESHOLD),
        ...computeDetectionLatency(windows, experiment, w => (w.probability || 0) >= ML_THRESHOLD),
      },
      {
        method: 'Hybrid (Proposed)',
        description: 'risk = HIGH or COOLDOWN',
        ...detection,
        ...detectionLatency,
      },
    ];

    const ablation = [
      {
        config: 'Stat only',
        ...computeDetectionMetrics(windows, experiment, w => (w.anomaly_count || 0) >= 2),
        ...computeDetectionLatency(windows, experiment, w => (w.anomaly_count || 0) >= 2),
      },
      {
        config: 'ML only',
        ...computeDetectionMetrics(windows, experiment, w => (w.probability || 0) >= ML_THRESHOLD),
        ...computeDetectionLatency(windows, experiment, w => (w.probability || 0) >= ML_THRESHOLD),
      },
      {
        config: 'Rules only',
        ...computeDetectionMetrics(windows, experiment, w => w.rule_triggered === true),
        ...computeDetectionLatency(windows, experiment, w => w.rule_triggered === true),
      },
      {
        config: 'Stat + ML',
        ...computeDetectionMetrics(windows, experiment, w => (w.anomaly_count || 0) >= 2 || (w.probability || 0) >= ML_THRESHOLD),
        ...computeDetectionLatency(windows, experiment, w => (w.anomaly_count || 0) >= 2 || (w.probability || 0) >= ML_THRESHOLD),
      },
      {
        config: 'Stat + Rules',
        ...computeDetectionMetrics(windows, experiment, w => (w.anomaly_count || 0) >= 2 || w.rule_triggered === true),
        ...computeDetectionLatency(windows, experiment, w => (w.anomaly_count || 0) >= 2 || w.rule_triggered === true),
      },
      {
        config: 'Full Hybrid (Proposed)',
        ...detection,
        ...detectionLatency,
      },
    ];

    const timeseries = windows.map(w => ({
      timestamp:           w.timestamp,
      probability:         +(w.probability         || 0).toFixed(4),
      weighted_error_rate: +(w.weighted_error_rate || 0).toFixed(4),
      p95_latency:         +(w.p95_latency         || 0).toFixed(1),
      anomaly_count:        w.anomaly_count        || 0,
      risk:                 w.risk                 || 'LOW',
      request_count:        w.request_count        || 0,
    }));

    res.json({
      experiment:       sanitize(experiment),
      detection,
      detection_latency: detectionLatency,
      prediction_stats:  predictionStats,
      healing,
      baselines,
      ablation,
      timeseries,
      fault_period: {
        start: experiment.start_time,
        end:   experiment.actual_end_time || experiment.planned_end_time,
      },
      recovery_events: recoveryActions.map(a => ({
        timestamp:   a.created_at,
        executed_at: a.executed_at,
        action:      a.action,
        status:      a.status,
      })),
    });
  } catch (err) {
    console.error('Evaluate error:', err);
    res.status(500).json({ error: err.message });
  }
});

// ─── Health ──────────────────────────────────────────────────────────────────

app.get('/health', (_req, res) => {
  res.json({ status: 'ok', service: 'experiment-console-backend', port: PORT });
});

// ─── SPA catch-all ───────────────────────────────────────────────────────────

app.get('*', (_req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// ─── Start ───────────────────────────────────────────────────────────────────

app.listen(PORT, () => {
  console.log(`🚀 Experiment Console running on http://localhost:${PORT}`);
});
