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

let resolvedHostIpCache = process.env.HOST_IP || HOSTNAME;

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
      resolvedHostIpCache = ipResp.data;
      return ipResp.data;
    }
  } catch (err) {
    console.warn(`⚠️  Could not access EC2 metadata service: ${err.message}`);
  }

  // Tier 2: explicit override (used in docker-compose for local demo)
  if (process.env.HOST_IP) {
    console.log(`📍 Resolved host_ip from HOST_IP env var: ${process.env.HOST_IP}`);
    resolvedHostIpCache = process.env.HOST_IP;
    return process.env.HOST_IP;
  }

  // Tier 3: last resort, never blocks startup
  console.warn(`⚠️  Could not resolve EC2 metadata or HOST_IP env var, falling back to hostname: ${HOSTNAME}`);
  resolvedHostIpCache = HOSTNAME;
  return HOSTNAME;
}

// ---------------- CHAOS STATE ----------------
// These are the same variables the keyboard controls already use.
// The injection API writes to them directly — no duplicate mechanism.
let FAILURE_MODE = false;
let LATENCY_SPIKE = false;

// Extended experiment-driven fault state
let EXTENDED_FAULT = {
  experiment_id: null,
  fault_type: "NORMAL",
  intensity: 0.0,
  latency_ms: 0,
  active: false,
};
let faultAutoStopTimer = null;
let trafficSpikeInterval = null;

function setExtendedFault(config) {
  if (faultAutoStopTimer) clearTimeout(faultAutoStopTimer);
  if (trafficSpikeInterval) clearInterval(trafficSpikeInterval);

  EXTENDED_FAULT = {
    experiment_id: config.experiment_id || null,
    fault_type: config.fault_type || "NORMAL",
    intensity: config.intensity || 0.0,
    latency_ms: config.latency_ms || 0,
    active: config.fault_type && config.fault_type !== "NORMAL",
  };

  const duration_sec = config.duration_sec || 60;

  if (EXTENDED_FAULT.fault_type === "TRAFFIC_SPIKE" && EXTENDED_FAULT.active) {
    // Generate extra request volume dynamically based on intensity
    const intervalMs = Math.max(20, Math.floor(100 / (config.intensity || 1.0)));
    trafficSpikeInterval = setInterval(() => {
      if (EXTENDED_FAULT.active) sendLog(resolvedHostIpCache);
    }, intervalMs);
  }

  if (EXTENDED_FAULT.active && duration_sec > 0) {
    faultAutoStopTimer = setTimeout(() => {
      console.log(`⏰ Fault experiment ${EXTENDED_FAULT.experiment_id} auto-expired`);
      clearExtendedFault();
    }, duration_sec * 1000);
  }

  console.log(`⚡ EXTENDED FAULT SET: ${EXTENDED_FAULT.fault_type} (Intensity: ${EXTENDED_FAULT.intensity}, Latency: ${EXTENDED_FAULT.latency_ms}ms)`);
}

function clearExtendedFault() {
  if (faultAutoStopTimer) clearTimeout(faultAutoStopTimer);
  if (trafficSpikeInterval) clearInterval(trafficSpikeInterval);

  EXTENDED_FAULT = {
    experiment_id: null,
    fault_type: "NORMAL",
    intensity: 0.0,
    latency_ms: 0,
    active: false,
  };
  console.log(`🧹 EXTENDED FAULT CLEARED — Restored to NORMAL state`);
}

function generateLog(hostIp) {
  const effectiveHostIp = hostIp || resolvedHostIpCache || process.env.HOST_IP || HOSTNAME;
  let level = "INFO";
  let response_time = Math.floor(Math.random() * 100) + 50; // Normal: 50-150ms

  const rand = Math.random();

  if (EXTENDED_FAULT.active) {
    switch (EXTENDED_FAULT.fault_type) {
      case "ERROR_INJECTION": {
        const errProb = EXTENDED_FAULT.intensity || 0.2;
        const warnProb = errProb * 0.5;
        if (rand < errProb) level = "ERROR";
        else if (rand < errProb + warnProb) level = "WARN";
        break;
      }
      case "LATENCY_SPIKE": {
        response_time += (EXTENDED_FAULT.latency_ms || 1000);
        break;
      }
      case "SERVICE_FAILURE": {
        level = "ERROR";
        response_time += 5000;
        break;
      }
      default:
        break;
    }
  }

  if (FAILURE_MODE) {
    if (rand < 0.3) level = "ERROR";
    else if (rand < 0.5) level = "WARN";
    response_time += 400;
  } else if (!EXTENDED_FAULT.active) {
    if (rand < 0.02) level = "ERROR";
    else if (rand < 0.05) level = "WARN";
  }

  if (LATENCY_SPIKE) {
    response_time += Math.floor(Math.random() * 2000) + 1000;
  }

  return {
    service: SERVICE_NAME,
    container_id: CONTAINER_ID,   // used by log_processor.py for targeted restarts
    host_ip: effectiveHostIp,     // used by controller.py to locate the right agent
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
function stateResponse() {
  return JSON.stringify({
    service: SERVICE_NAME,
    container_id: CONTAINER_ID,
    failure_mode: FAILURE_MODE,
    latency_spike: LATENCY_SPIKE,
    extended_fault: EXTENDED_FAULT,
    effects: {
      error_rate: FAILURE_MODE || EXTENDED_FAULT.fault_type === "ERROR_INJECTION" || EXTENDED_FAULT.fault_type === "SERVICE_FAILURE"
        ? "[ACTIVE] Elevated Error Rate"
        : "~2% ERROR / 3% WARN (normal)",
      latency_added_ms: LATENCY_SPIKE || EXTENDED_FAULT.fault_type === "LATENCY_SPIKE" || EXTENDED_FAULT.fault_type === "SERVICE_FAILURE"
        ? "[ACTIVE] Increased Latency"
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

    // ── POST /fault/start ─────────────────────────────────────────────
    if (method === "POST" && url === "/fault/start") {
      let body = "";
      req.on("data", (chunk) => { body += chunk; });
      req.on("end", () => {
        try {
          const payload = body.trim() ? JSON.parse(body) : {};
          setExtendedFault(payload);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(stateResponse());
        } catch {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "Invalid JSON body" }));
        }
      });
      return;
    }

    // ── POST /fault/stop ──────────────────────────────────────────────
    if (method === "POST" && url === "/fault/stop") {
      clearExtendedFault();
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

          if (typeof payload.failure_mode === "boolean") {
            FAILURE_MODE = payload.failure_mode;
            console.log(`🌐 API → FAILURE_MODE: ${FAILURE_MODE ? "ON" : "OFF"}`);
          }
          if (typeof payload.latency_spike === "boolean") {
            LATENCY_SPIKE = payload.latency_spike;
            console.log(`🌐 API → LATENCY_SPIKE: ${LATENCY_SPIKE ? "ON" : "OFF"}`);
          }

          if (payload.fault_type) {
            setExtendedFault(payload);
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
        "POST /fault/start",
        "POST /fault/stop",
        "POST /inject/failure",
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
  resolvedHostIpCache = hostIp;

  console.log(`🚀 Starting ${SERVICE_NAME} | container_id=${CONTAINER_ID} | host_ip=${hostIp}`);
  setupKeyboardControls();
  startInjectionApi();

  const interval = Math.floor(Math.random() * 500) + 500; // 500-1000ms jitter
  logIntervalHandle = setInterval(() => sendLog(hostIp), interval);
}

main();