import axios from "axios";
import { v4 as uuidv4 } from "uuid";

//source venv/bin/activate
//python3 log_processor.py


// ---------------- COMMAND LINE ARGS ----------------
// Usage: node service_node.js [service_name] [container_id]
const SERVICE_NAME = process.argv[2] || "auth-service";
const CONTAINER_ID = process.argv[3] || `${SERVICE_NAME}-${Math.floor(Math.random() * 1000)}`;
const COLLECTOR_URL = "http://localhost:5001/logs";

console.log(`🚀 Starting ${SERVICE_NAME} (Node: ${CONTAINER_ID})`);

// ---------------- CONFIG & CHAOS ----------------
let FAILURE_MODE = false; 
let LATENCY_SPIKE = false;

// Function to generate realistic behavior
function generateLog() {
  let level = "INFO";
  let response_time = Math.floor(Math.random() * 100) + 50; // Normal: 50-150ms

  const rand = Math.random();

  if (FAILURE_MODE) {
    // 30% chance of ERROR, 20% chance of WARN under failure
    if (rand < 0.3) level = "ERROR";
    else if (rand < 0.5) level = "WARN";
    response_time += 400; // General slowdown
  } else {
    // Normal operation: 2% errors, 3% warnings
    if (rand < 0.02) level = "ERROR";
    else if (rand < 0.05) level = "WARN";
  }

  if (LATENCY_SPIKE) {
    // Simulates a database bottleneck or slow API
    response_time += Math.floor(Math.random() * 2000) + 1000; 
  }

  return {
    service: SERVICE_NAME,
    container_id: CONTAINER_ID, // CRITICAL: For your micro-isolation logic
    level: level,
    response_time: response_time,
    message: level === "INFO" ? "Request processed" : `Internal dependency timeout in ${SERVICE_NAME}`,
    timestamp: new Date().toISOString(),
    request_id: uuidv4()
  };
}


// ---------------- SENDING LOGS ----------------
async function sendLog() {
  const log = generateLog();
  try {
    await axios.post("http://localhost:5001/logs", log);
    console.log(`[${log.timestamp}] ${log.level} | ${log.container_id} | ${log.response_time}ms`);
  } catch (err) {
    console.error("Collector Offline");
    console.error("Connection Error:", err.message);
  }
}

// ---------------- LIVE CONTROLS ----------------
// You can toggle failure modes while the script is running by pressing keys
import readline from 'readline';
readline.emitKeypressEvents(process.stdin);
process.stdin.setRawMode(true);

process.stdin.on('keypress', (str, key) => {
  if (key.name === 'f') {
    FAILURE_MODE = !FAILURE_MODE;
    console.log(`\n🔥 FAILURE MODE: ${FAILURE_MODE ? 'ON' : 'OFF'}\n`);
  }
  if (key.name === 'l') {
    LATENCY_SPIKE = !LATENCY_SPIKE;
    console.log(`\n⏳ LATENCY SPIKE: ${LATENCY_SPIKE ? 'ON' : 'OFF'}\n`);
  }
  if (key.ctrl && key.name === 'c') process.exit();
});

console.log("Controls: [f] Toggle Failure | [l] Toggle Latency | [ctrl+c] Exit");

// Run loop
const interval = Math.floor(Math.random() * 500) + 500; // Randomize interval slightly
setInterval(sendLog, interval);