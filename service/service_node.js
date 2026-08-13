import os from "os";
import http from "http";
import axios from "axios";
import { v4 as uuidv4 } from "uuid";
import dotenv from "dotenv";
import readline from "readline";
dotenv.config();

// ---------------- IDENTITY ----------------
// Usage: node service_node.js [service_name] [container_id]
const SERVICE_NAME = process.env.SERVICE_NAME || process.argv[2] || "auth-service";
const HOSTNAME = os.hostname();
const CONTAINER_ID =
  process.env.CONTAINER_ID || process.argv[3] || HOSTNAME;
const COLLECTOR_URL = process.env.COLLECTOR_URL || "http://localhost:5001/logs";

// Port for the fault-injection API.
// Override via INJECTION_API_PORT env var if needed.
// Does NOT conflict with Collector (5001), agent (8000),
// dashboard-backend (5000), or dashboard-frontend (5173).
const INJECTION_API_PORT = parseInt(process.env.INJECTION_API_PORT ?? "5002", 10);

async function resolveHostIp() {
  // Tier 1: EC2 metadata service (IMDSv2 - token-based)
  try {
    const tokenResp = await axios.put(
      "http://169.254.169.254/latest/api/token",
      null,
      {
        headers: { "X-aws-ec2-metadata-token-ttl-seconds": "21600" },
        timeout: 500,
      }
    );
    const ipResp = await axios.get(
      "http://169.254.169.254/latest/meta-data/local-ipv4",
      {
        headers: { "X-aws-ec2-metadata-token": tokenResp.data },
        timeout: 500,
      }
    );
    if (ipResp.data) {
      console.log(`📍 Resolved host_ip from EC2 metadata: ${ipResp.data}`);
      return ipResp.data;
    }
  } catch (err) {
    console.warn(`⚠️  Could not access EC2 metadata service: ${err.message}`);
  }

  // Tier 2: explicit override (used in docker-compose for local demo)
  if (process.env.HOST_IP) {
    console.log(`📍 Resolved host_ip from HOST_IP env var: ${process.env.HOST_IP}`);
    return process.env.HOST_IP;
  }

  // Tier 3: last resort, never blocks startup
  console.warn(`⚠️  Could not resolve EC2 metadata or HOST_IP env var, falling back to hostname: ${HOSTNAME}`);
  return HOSTNAME;
}

// ---------------- CHAOS STATE ----------------
// These are the same variables the keyboard controls already use.
// The injection API writes to them directly — no duplicate mechanism.
let FAILURE_MODE = false;
let LATENCY_SPIKE = false;

function generateLog(hostIp) {
  let level = "INFO";
  let response_time = Math.floor(Math.random() * 100) + 50; // Normal: 50-150ms

  const rand = Math.random();

  if (FAILURE_MODE) {
    if (rand < 0.3) level = "ERROR";
    else if (rand < 0.5) level = "WARN";
    response_time += 400;
  } else {
    if (rand < 0.02) level = "ERROR";
    else if (rand < 0.05) level = "WARN";
  }

  if (LATENCY_SPIKE) {
    response_time += Math.floor(Math.random() * 2000) + 1000;
  }

  return {
    service: SERVICE_NAME,
    container_id: CONTAINER_ID,   // used by log_processor.py for targeted restarts
    host_ip: hostIp,              // used by controller.py to locate the right agent
    hostname: HOSTNAME,
    level,
    response_time,
    message:
      level === "INFO"
        ? "Request processed"
        : `Internal dependency timeout in ${SERVICE_NAME}`,
    timestamp: new Date().toISOString(),
    request_id: uuidv4(),
  };
}

// ---------------- SENDING LOGS ----------------
async function sendLog(hostIp) {
  const log = generateLog(hostIp);
  try {
    await axios.post(COLLECTOR_URL, log, { timeout: 2000 });
    console.log(
      `[${log.timestamp}] ${log.level} | ${log.container_id}@${log.host_ip} | ${log.response_time}ms`
    );
  } catch (err) {
    console.error(`Collector unreachable: ${err.message}`);
  }
}

// ---------------- FAULT INJECTION API ----------------
// Minimal HTTP server using Node's built-in `http` module — zero new
// dependencies. Runs on INJECTION_API_PORT (default 5002).
//
// Endpoints:
//
//   POST /inject/failure
//     Body (JSON, all fields optional):
//       { "failure_mode": true|false, "latency_spike": true|false }
//     Omitting a field leaves that mode unchanged.
//     Example — enable both:   {"failure_mode": true, "latency_spike": true}
//     Example — disable both:  {"failure_mode": false, "latency_spike": false}
//     Example — toggle only errors: {"failure_mode": true}
//
//   GET /inject/state
//     Returns current fault-injection state, no side effects.
//
// Both endpoints return the same JSON shape:
//   {
//     "service": "auth-service",
//     "container_id": "auth-service",
//     "failure_mode": false,
//     "latency_spike": false,
//     "effects": {
//       "error_rate": "~30% (30% ERROR, 20% WARN) [ACTIVE]" | "~2% (normal)",
//       "latency_added_ms": "1000–3000ms extra [ACTIVE]" | "none"
//     }
//   }

function stateResponse() {
  return JSON.stringify({
    service: SERVICE_NAME,
    container_id: CONTAINER_ID,
    failure_mode: FAILURE_MODE,
    latency_spike: LATENCY_SPIKE,
    effects: {
      error_rate: FAILURE_MODE
        ? "~30% ERROR / 20% WARN + 400ms base latency [ACTIVE]"
        : "~2% ERROR / 3% WARN (normal)",
      latency_added_ms: LATENCY_SPIKE
        ? "1000–3000ms extra [ACTIVE]"
        : "none",
    },
  }, null, 2);
}

function startInjectionApi() {
  const server = http.createServer((req, res) => {
    const { method, url } = req;

    // ── GET /inject/state ─────────────────────────────────────────────
    if (method === "GET" && url === "/inject/state") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(stateResponse());
      return;
    }

    // ── POST /inject/failure ──────────────────────────────────────────
    if (method === "POST" && url === "/inject/failure") {
      let body = "";
      req.on("data", (chunk) => { body += chunk; });
      req.on("end", () => {
        try {
          const payload = body.trim() ? JSON.parse(body) : {};

          // Only update modes that were explicitly provided in the request.
          // Omitting a key leaves the current value untouched.
          if (typeof payload.failure_mode === "boolean") {
            FAILURE_MODE = payload.failure_mode;
            console.log(`🌐 API → FAILURE_MODE: ${FAILURE_MODE ? "ON" : "OFF"}`);
          }
          if (typeof payload.latency_spike === "boolean") {
            LATENCY_SPIKE = payload.latency_spike;
            console.log(`🌐 API → LATENCY_SPIKE: ${LATENCY_SPIKE ? "ON" : "OFF"}`);
          }

          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(stateResponse());
        } catch {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "Invalid JSON body" }));
        }
      });
      return;
    }

    // ── 404 for anything else ─────────────────────────────────────────
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      error: "Not found",
      available: [
        "GET  /inject/state",
        "POST /inject/failure  body: {failure_mode?: bool, latency_spike?: bool}",
      ],
    }));
  });

  server.listen(INJECTION_API_PORT, () => {
    console.log(
      `🔧 Fault injection API: http://localhost:${INJECTION_API_PORT}`
    );
    console.log(`   GET  /inject/state`);
    console.log(`   POST /inject/failure  {"failure_mode":true,"latency_spike":true}`);
  });

  // Non-fatal: if the port is already in use, log and continue
  server.on("error", (err) => {
    console.error(`⚠️  Injection API failed to start on ${INJECTION_API_PORT}: ${err.message}`);
    console.error(`   Keyboard controls still work. Set INJECTION_API_PORT env var to use a different port.`);
  });
}

// ---------------- LIVE CONTROLS ----------------
// Keyboard controls unchanged — still work when running attached (npm start / docker attach)
function setupKeyboardControls() {
  if (!process.stdin.isTTY) return;

  readline.emitKeypressEvents(process.stdin);
  process.stdin.setRawMode(true);

  process.stdin.on("keypress", (str, key) => {
    if (key.name === "f") {
      FAILURE_MODE = !FAILURE_MODE;
      console.log(`\n🔥 FAILURE MODE: ${FAILURE_MODE ? "ON" : "OFF"}\n`);
    }
    if (key.name === "l") {
      LATENCY_SPIKE = !LATENCY_SPIKE;
      console.log(`\n⏳ LATENCY SPIKE: ${LATENCY_SPIKE ? "ON" : "OFF"}\n`);
    }
    if (key.ctrl && key.name === "c") shutdown();
  });

  console.log("Controls: [f] Toggle Failure | [l] Toggle Latency | [ctrl+c] Exit");
}

// ---------------- GRACEFUL SHUTDOWN ----------------
let logIntervalHandle = null;

function shutdown() {
  console.log(`\n👋 Shutting down ${SERVICE_NAME} (${CONTAINER_ID})`);
  if (logIntervalHandle) clearInterval(logIntervalHandle);
  process.exit(0);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

// ---------------- MAIN ----------------
async function main() {
  const hostIp = await resolveHostIp();

  console.log(`🚀 Starting ${SERVICE_NAME} | container_id=${CONTAINER_ID} | host_ip=${hostIp}`);
  setupKeyboardControls();
  startInjectionApi();

  const interval = Math.floor(Math.random() * 500) + 500; // 500-1000ms jitter
  logIntervalHandle = setInterval(() => sendLog(hostIp), interval);
}

main();