import express from "express";
import bodyParser from "body-parser";
import { createClient } from "redis";

const app = express();
app.use(bodyParser.json());

// ---------------- REDIS SETUP ----------------
const redisClient = createClient();
redisClient.connect();

redisClient.on("connect", () => {
  console.log("✅ Redis connected");
});

// ---------------- VALIDATION FUNCTION ----------------
function validateLog(log) {
  const requiredFields = [
    "service",
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

  if (typeof log.service !== "string") return "service must be string";
  if (!["INFO", "ERROR"].includes(log.level)) return "invalid log level";
  if (typeof log.response_time !== "number") return "response_time must be number";
  if (typeof log.timestamp !== "number") return "timestamp must be number";
  if (typeof log.request_id !== "string") return "request_id must be string";

  return null; // valid
}

// ---------------- LOG INGEST API ----------------
app.post("/logs", async (req, res) => {
  const log = req.body;

  const error = validateLog(log);
  if (error) {
    return res.status(400).json({
      status: "REJECTED",
      reason: error
    });
  }

  // push valid log to Redis list
  await redisClient.lPush("LOG_STREAM", JSON.stringify(log));

  return res.status(200).json({
    status: "ACCEPTED"
  });
});

// ---------------- START SERVER ----------------
app.listen(5000, () => {
  console.log("🚀 Log Collector running on port 5000");
});
