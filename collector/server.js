import express from "express";
import bodyParser from "body-parser";
import dotenv from "dotenv";
import { createClient } from "redis";

dotenv.config();
const app = express();
app.use(bodyParser.json());

// ---------------- REDIS SETUP ----------------
const redisClient = createClient({url: process.env.REDIS_URL || "redis://redis:6379"});
redisClient.connect().catch(err => console.error("❌ Redis Connection Error", err));

redisClient.on("connect", () => {
  console.log("✅ Redis connected");
});

// ---------------- UPGRADED VALIDATION ----------------
function validateLog(log) {
  const requiredFields = [
    "service",
    "container_id", // NEW: Required for Micro-Isolation
    "hostname",
    "host_ip", // NEW: Required for Micro-Isolation
    "level",
    "response_time",
    "timestamp",
    "request_id"
  ];

  for (let field of requiredFields) {
    if (!(field in log)) {
      return `Missing field: ${field}`;
    }
  }

  // 1. Service & Container Validation
  if (typeof log.service !== "string") return "service must be string";
  if (typeof log.container_id !== "string") return "container_id must be string";
  if (typeof log.hostname !== "string") return "hostname must be string";
  if (typeof log.host_ip !== "string") return "host_ip must be string";

  // 2. Log Level Expansion (Now accepts WARN/WARNING)
  const validLevels = ["INFO", "WARN", "WARNING", "ERROR"];
  if (!validLevels.includes(log.level)) {
    return `invalid log level: ${log.level}. Must be one of ${validLevels.join(", ")}`;
  }

  // 3. Metric Types
  if (typeof log.response_time !== "number") return "response_time must be number";
  
  // Accept both Unix numbers and ISO strings for flexibility
  if (typeof log.timestamp !== "number" && typeof log.timestamp !== "string") {
      return "timestamp must be number or ISO string";
  }

  return null; // valid
}

// ---------------- LOG INGEST API ----------------
app.post("/logs", async (req, res) => {
  const log = req.body;
  const error = validateLog(log);
  if (error) {
    console.warn(`⚠️ Rejected log from ${log.service || 'unknown'}: ${error}`);
    return res.status(400).json({
      status: "REJECTED",
      reason: error
    });
  }

  try {
    // Push valid log to Redis list
    // The Python Analyzer is waiting for this via r.brpop("LOG_STREAM")
    await redisClient.lPush("LOG_STREAM", JSON.stringify(log));

    return res.status(200).json({
      status: "ACCEPTED"
    });
  } catch (dbError) {
    console.error("❌ Redis Push Failed", dbError);
    return res.status(500).json({ status: "ERROR", reason: "Internal Buffer Full" });
  }
});

// ---------------- HEALTH CHECK (For the System) ----------------
app.get("/health", (req, res) => {
    res.status(200).send("Collector is Alive");
});

// ---------------- START SERVER ----------------
const PORT = 5001;
app.listen(PORT, () => {
  console.log(`🚀 Log Collector Gateway running on port ${PORT}`);
  console.log(`📡 Ingesting logs for the Predictive ML Engine...`);
});