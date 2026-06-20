# Distributed Log Analysis, Predictive Failure Detection & Self-Healing System

A prototype platform that monitors containerized microservices, detects anomalies and predicts failures using a hybrid statistical + machine learning approach, and automatically takes recovery action (restart / scale) — modeled loosely on how Kubernetes separates **decision** (controller logic) from **execution** (node-local agents).

> Final year CSE project — Dept. of CS&E, MITE, Moodabidri
> Status: Phase 1 (design + literature survey) complete. Phase 2 (implementation) in progress.

---

## Table of Contents

- [What this project does](#what-this-project-does)
- [Architecture](#architecture)
- [Repository structure](#repository-structure)
- [How the self-healing flow works end-to-end](#how-the-self-healing-flow-works-end-to-end)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Running locally](#running-locally)
- [Demoing failure scenarios](#demoing-failure-scenarios)
- [Retraining the ML model](#retraining-the-ml-model)
- [Running across multiple EC2 instances](#running-across-multiple-ec2-instances)
- [Known limitations / things to watch for](#known-limitations--things-to-watch-for)
- [Roadmap](#roadmap)
- [References](#references)

---

## What this project does

Modern microservice systems generate huge volumes of logs, and most monitoring is **reactive** — it tells you a service has already failed. This project tries to be **proactive**:

1. **Collects** structured JSON logs from every microservice in real time.
2. **Aggregates** them into 60-second windows per service.
3. **Detects anomalies** statistically using rolling Z-scores (latency, error rate, warnings, traffic delta).
4. **Predicts failure probability** using a Random Forest model trained on fault-injected data.
5. **Classifies risk** as `LOW` / `MEDIUM` / `HIGH` using a **hybrid rule + ML** policy (a raw-rate rule override catches cases the ML model's z-score features alone might miss).
6. **Decides** a recovery action (restart or scale) — but does not execute it directly.
7. **Executes** that action via a separate Controller + per-host Agent layer, so the system that decides is never the system that touches Docker — mirroring how Kubernetes' control plane and kubelet are separate.

---

## Architecture

```
 ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │ auth-service │   │ order-service│   │payment-service│   (Node.js, generate
 └──────┬───────┘   └──────┬───────┘   └──────┬───────┘    structured JSON logs)
        │                  │                  │
        └──────────────────┼──────────────────┘
                            ▼
                      ┌───────────┐
                      │ Collector │  (receives logs, stamps host_ip,
                      └─────┬─────┘   pushes to Redis stream)
                            ▼
                      ┌───────────┐
                      │   Redis   │  (buffer — LOG_STREAM)
                      └─────┬─────┘
                            ▼
                  ┌───────────────────┐
                  │     Processor      │  60s window aggregation,
                  │  (log_processor.py)│  Z-score anomaly detection,
                  │                     │  Random Forest prediction,
                  │  ┌───────────────┐  │  hybrid risk classification
                  │  │ Policy Engine │  │
                  │  │(decides ONLY) │  │
                  │  └───────┬───────┘  │
                  └──────────┼──────────┘
                             ▼
                      ┌─────────────┐
                      │  MongoDB    │  window_history /
                      │   Atlas     │  recovery_actions / desired_state
                      └──────┬──────┘
                             ▼
                      ┌─────────────┐
                      │ Controller  │  reconcile loop — reads PENDING
                      │             │  actions + desired_state, calls
                      └──────┬──────┘  the right Agent over HTTP
                             ▼
                   ┌───────────────────┐
                   │   Agent (1/host)  │  local-only Docker SDK access,
                   │                   │  restart / scale on THIS host
                   └─────────┬─────────┘
                             ▼
                      ┌─────────────┐
                      │   Docker    │
                      │  Containers │
                      └─────────────┘
                             │
                             ▼
                      ┌─────────────┐
                      │  Dashboard  │  React.js + Chart.js, reads
                      │             │  MongoDB for live visibility
                      └─────────────┘
```

**Why decide and execute are separate services:** the Processor/Policy Engine only ever *writes intent* (a `recovery_action` document) to MongoDB. It never calls Docker directly. The Controller is the only thing that reads pending actions and calls an Agent. This means:
- A hang in a Docker call never blocks log analysis.
- The Controller can crash and restart without losing track — it just re-reads MongoDB state.
- The same Controller code works whether everything runs on one machine or across multiple EC2 instances — the only thing that changes is *where* the Agent it's calling happens to be.

**Why `host_ip` instead of a static host registry:** earlier versions of this system used a hardcoded Python dict mapping service name → EC2 IP. That's a manually maintained piece of state that can silently drift from reality. Instead, every log line carries the `host_ip` of the machine it was generated on (read from the Docker container directly, or — in real EC2 deployment — from instance metadata). That IP flows through `window_history` → `recovery_actions` → the Controller, which builds the Agent URL fresh from the data each time. Nothing to keep in sync by hand.

---

## Repository structure

```
.
├── service/                  # Node.js microservice (auth/order/payment, same image, different env)
│   ├── service_node.js
│   ├── package.json
│   └── Dockerfile
├── collector/                 # Receives logs over HTTP, stamps host_ip, pushes to Redis
│   └── ...
├── processor/
│   ├── log_processor.py       # Window aggregation, Z-scores, ML inference, hybrid risk
│   ├── policy_engine.py        # PURE decision logic — writes intent, never executes
│   ├── train_model.py          # Retrains the Random Forest model with CV + PR-curve threshold tuning
│   ├── dataset_v2.csv          # Training data (z-scored features + label)
│   ├── failure_model_v2.pkl    # Trained model + feature names + threshold + metadata
│   ├── requirements.txt
│   └── Dockerfile
├── controller/
│   ├── controller.py           # Reconcile loop — reads MongoDB, calls Agents over HTTP
│   ├── requirements.txt
│   └── Dockerfile
├── agent/
│   ├── agent.py                 # Per-host executor — local Docker SDK, restart/scale endpoints
│   ├── requirements.txt
│   └── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## How the self-healing flow works end-to-end

1. A microservice container emits a log line including `service`, `container_id`, `host_ip`, `level`, `response_time`.
2. The Collector receives it and pushes it onto Redis (`LOG_STREAM`).
3. The Processor reads from Redis, buffers for 60 seconds, then for each service:
   - Computes `weighted_error_rate`, `p95_latency`, `warn_frequency`, `traffic_delta`.
   - Computes rolling Z-scores against the last 30 windows.
   - Builds a feature vector and gets a failure probability from the Random Forest model.
   - Applies **hybrid risk logic**:
     ```python
     if weighted_error_rate > 0.20 or prob >= ml_threshold or anomaly_count >= 2:
         risk = "HIGH"
     elif prob > 0.2 or anomaly_count == 1:
         risk = "MEDIUM"
     else:
         risk = "LOW"
     ```
     The raw-rate rule override exists because the training dataset only contains Z-scored features, not raw percentages — Z-scores are relative to a rolling baseline, so a genuinely bad raw error rate can still produce a small Z-score against a noisy baseline. The rule guarantees obviously bad situations are never missed.
4. If risk is `HIGH` and the service isn't in cooldown, the Policy Engine checks dependency health (e.g. don't restart `auth-service` if `payment-service` — which it depends on — is already `HIGH`), then writes a `recovery_action` document with `status: PENDING`, carrying the `host_ip` of the container that caused the alert.
5. The Controller's reconcile loop (polls every 5s) picks up `PENDING` actions, looks up the Agent at `http://{host_ip}:8000`, and calls `/restart/{container}` or `/scale/{service}`.
6. The Agent — which has local-only access to that host's Docker socket — performs the actual restart/scale and reports back. The Controller marks the action `SUCCESS` or `FAILED`.
7. A separate drift-reconciliation pass compares `desired_state` (e.g. "payment-service should have 3 replicas") against actual running containers on every tick, independent of any specific triggering event — the same pattern Kubernetes' ReplicaSet controller uses.
8. The Dashboard reads `window_history` and `recovery_actions` from MongoDB to show live risk, anomaly trends, and recovery history.

---

## Prerequisites

- Docker + Docker Compose v2
- Node.js 20+ (only needed if running a service outside Docker for debugging)
- Python 3.11+ (only needed if retraining the model outside Docker)
- A MongoDB Atlas connection string (or a local `mongo` container if you prefer — adjust `MONGO_URI` accordingly)

---

## Setup

1. **Clone and enter the repo:**
   ```bash
   git clone <your-repo-url>
   cd <repo-folder>
   ```

2. **Create `.env` files** (these are git-ignored — never commit real credentials):

   `processor/.env`:
   ```
   MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/predictive_system
   REDIS_HOST=redis
   REDIS_PORT=6379
   MODEL_PATH=failure_model_v2.pkl
   ```

   `controller/.env`:
   ```
   MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/predictive_system
   RECONCILE_INTERVAL_SEC=5
   AGENT_TIMEOUT_SEC=10
   AGENT_PORT=8000
   ```

3. **Make sure `failure_model_v2.pkl` is present** inside `processor/` — if you don't have one yet, see [Retraining the ML model](#retraining-the-ml-model) below.

4. **Double-check your Docker network name** in `docker-compose.yml` matches what you actually use everywhere (Controller's `DOCKER_NETWORK` env var, Agent's default `network` field) — a mismatch here is a common source of "container can't reach the collector" bugs. Confirm with:
   ```bash
   docker network ls
   ```

---

## Running locally

```bash
docker compose up -d --build
```

This brings up: `redis`, `collector`, `auth-service`, `order-service`, `payment-service`, `processor`, `agent`, `controller`, `dashboard-backend`, `dashboard-frontend`.

**Verify the pipeline is alive, in order:**

```bash
# 1. Logs are flowing into Redis
docker exec -it redis redis-cli MONITOR
# you should see LPUSH / BRPOP activity on LOG_STREAM

# 2. Processor is consuming and writing to MongoDB
docker logs -f processor
# look for lines like: AUTH-SERVICE | risk=LOW | P=0.02 | ...

# 3. Agent is reachable and reporting its host
curl http://localhost:8000/health

# 4. Controller is running its reconcile loop
docker logs -f controller
```

**View the dashboard:** open `http://localhost:3000`.

---

## Demoing failure scenarios

Each `service_node.js` instance accepts live keyboard toggles if run attached to a TTY:

| Key | Effect |
|---|---|
| `f` | Toggle failure mode (30% error rate, 20% warning rate, +400ms latency) |
| `l` | Toggle latency spike (+1000–3000ms on top of current latency) |
| `Ctrl+C` | Graceful shutdown |

To trigger a HIGH-risk window and watch the full self-healing chain fire:

```bash
docker attach payment-service
# press 'f' to enable failure mode, wait ~1-2 minutes for a 60s window
# press 'f' again to disable, then Ctrl+P, Ctrl+Q to detach without killing it
```

Then watch:
```bash
docker logs -f processor    # should show risk escalating to HIGH
docker logs -f controller   # should show an action being picked up and executed
docker logs -f agent        # should show the actual restart/scale call
```

---

## Retraining the ML model

```bash
cd processor
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 train_model.py
```

This performs stratified 5-fold cross-validation, tunes the decision threshold against the precision-recall curve (optimizing for recall — a missed failure is costlier than a false-alarm restart), evaluates on a held-out test set never used during training or threshold selection, and saves `failure_model_v2.pkl` with full metadata (`features`, `threshold`, `trained_at`, dataset notes).

**Known dataset limitation:** `dataset_v2.csv` contains only Z-scored features, no raw percentage columns. This is exactly why the hybrid rule override in `log_processor.py` exists as a permanent safety net rather than a temporary stopgap. If you regenerate the dataset from real fault-injection runs, include raw `weighted_error_rate`, raw `p95_latency`, and raw `warn_frequency` alongside the Z-scores — that would let a future model learn raw-rate thresholds directly.

**Important:** the model's `features` list must exactly match the keys built in `log_processor.py`'s `feature_dict` (e.g. `is_auth`, not `is_auth-service`) — a mismatch here silently zero-fills the missing columns instead of raising an error, so always sanity-check `model_data["features"]` against `log_processor.py` after retraining.

---

## Running across multiple EC2 instances

The architecture was deliberately designed so this requires **no code changes**, only configuration:

1. Provision 3 EC2 instances, one per service (`auth-service`, `order-service`, `payment-service`).
2. On each instance, run that service's container **and** one `agent` container, mounting `/var/run/docker.sock`.
3. Set each service's `HOST_IP` env var (or let it auto-resolve via EC2 instance metadata — `service_node.js` tries this first automatically).
4. Run `redis`, `collector`, `processor`, `controller`, and the `dashboard` centrally (a 4th small instance, or even locally) — these don't need to be co-located with the services they monitor.
5. Open inbound port `8000` on each service EC2's security group, restricted to the Controller's IP/security-group only — the Agent can create and destroy containers on that box, so it shouldn't be open to the internet.

The Controller never hardcodes which EC2 a service lives on — it reads `host_ip` straight from each `recovery_action` document, which came from the log line that triggered the alert.

---

## Known limitations / things to watch for

- **Prototype scope**: tested in a controlled local/Docker environment with synthetically injected failures, not a real production system.
- **No autoscaling down yet**: `desired_state` only ever scales *up* automatically; bringing replica count back down once load normalizes currently requires manual intervention. A scale-down policy (triggered on sustained LOW risk) is planned but not yet implemented.
- **Docker labels matter**: every container — including the original Compose-managed ones, not just Agent-created replicas — must carry a `labels: service: "<name>"` entry, or label-filtered queries (`/scale`, `/containers/{service}`, drift reconciliation) will silently undercount them.
- **Network name consistency**: the Docker network name used by Compose, the Agent's default, and any explicit `network` field passed by the Controller must all match exactly, or replica containers get created on an unreachable network and can't reach the Collector.
- **`host_ip` trust**: currently trusted as self-reported by each service. Fine for this controlled project scope; a hardened version would have the Collector stamp `host_ip` from the actual TCP connection source instead of trusting a field the service wrote into its own payload.
- **Kubernetes not used**: scaling is simulated via Docker SDK + a custom Controller/Agent pair, not real container orchestration — explicitly noted as a scope limitation, not an oversight.

---

## Roadmap

- **Week 1** — Controller + Agent service, Docker SDK–based recovery actions, `recovery_actions` collection.
- **Week 2** — Improve failure prediction (hybrid rule + ML refinement), better risk scoring, scale-down policy.
- **Week 3** — Load balancing across replicas, multi-replica traffic distribution demo.
- **Week 4** — Telegram bot for mobile control/alerts, MTTR metric tracking, final evaluation.

---

## References

1. N. Kaushik, H. Kumar, V. Raj, P. Garg, "Proactive Fault Prediction in Microservices Applications Using Trace Logs and Monitoring Metrics," ICPIDS 2024.
2. K. Alam, K. Kifayat, G. A. Sampedro, V. Karovič Jr., T. Naeem, "SXAD: Shapley eXplainable AI-Based Anomaly Detection Using Log Data," IEEE Access, vol. 12, 2024.
3. A. Aziz, M. Munir, "Anomaly Detection in Logs Using Deep Learning," IEEE Access, vol. 12, 2024.
4. M. Du, F. Li, G. Zheng, V. Srikumar, "DeepLog: Anomaly Detection and Diagnosis from System Logs through Deep Learning," ACM CCS 2017.
5. Docker Inc., Docker Documentation — https://docs.docker.com
6. Redis Labs, Redis Streams Documentation — https://redis.io/docs/data-types/streams
7. MongoDB Inc., MongoDB Manual — https://www.mongodb.com/docs/manual
8. Scikit-learn Developers, Scikit-learn: Machine Learning in Python — https://scikit-learn.org