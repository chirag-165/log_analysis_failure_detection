import axios from "axios";
import { v4 as uuidv4 } from "uuid";

const SERVICES = ["auth", "order", "payment"];
const COLLECTOR_URL = "http://localhost:5000/logs";

// ---------------- CONFIG ----------------
const FAILURE_MODE = true; // set true to simulate failures

function generateLog() {
  const isError = FAILURE_MODE
    ? Math.random() < 1    // 40% errors in failure mode
    : Math.random() < 0.05;  // 5% errors normally

  return {
    service: SERVICES[Math.floor(Math.random() * SERVICES.length)],
    level: isError ? "ERROR" : "INFO",
    response_time: isError
      ? Math.floor(Math.random() * 1500) + 500
      : Math.floor(Math.random() * 200) + 100,
    timestamp: Date.now(),
    request_id: uuidv4()
  };
}

// ---------------- SEND LOGS ----------------
async function sendLog() {
  const log = generateLog();

  try {
    await axios.post(COLLECTOR_URL, log);
    console.log("Sent:", log.level, log.service);
  } catch (err) {
    console.error("Rejected:", err.response?.data);
  }
}

// ---------------- CONTINUOUS GENERATION ----------------
setInterval(sendLog, 100); // 10 logs/sec
