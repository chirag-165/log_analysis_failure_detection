import os
import time
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [CONTROLLER] - %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI")
RECONCILE_INTERVAL_SEC = int(os.getenv("RECONCILE_INTERVAL_SEC", "10"))
AGENT_TIMEOUT_SEC = int(os.getenv("AGENT_TIMEOUT_SEC", "10"))
AGENT_PORT = int(os.getenv("AGENT_PORT", "8000"))

SERVICE_IMAGES = {
    "auth-service": "log-analysis-auth-service:latest",
    "order-service": "log-analysis-order-service:latest",
    "payment-service": "log-analysis-payment-service:latest",
}

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["log_analysis_dashboard"]
actions_col = db["recovery_actions"]
desired_state_col = db["desired_state"]


import smtplib
from email.mime.text import MIMEText

DEFAULT_ESCALATION_EMAIL = os.getenv("ESCALATION_EMAIL", "shettychirag16@gmail.com")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")


def send_escalation_email(service, doc):
    recipient = DEFAULT_ESCALATION_EMAIL
    reason = doc.get("decision_reason", "Repeated service failure threshold exceeded")
    host_ip = doc.get("host_ip", "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()

    subject = f"🚨 [CRITICAL ALERT] On-Call Escalation Triggered for {service}"
    body = f"""
===================================================================
CRITICAL ON-CALL ESCALATION ALERT
===================================================================
Service: {service}
Host IP: {host_ip}
Action: ESCALATE_ON_CALL
Reason: {reason}
Timestamp: {timestamp}

The automated self-healing controller has escalated this incident because 
repeated automated restarts/healing actions did not resolve the service failure.

Please inspect the system dashboard and microservice health immediately.
===================================================================
"""
    logger.warning("🚨 ESCALATION ALERT triggered for %s! Recipient: %s", service, recipient)

    if SMTP_HOST and SMTP_USER and SMTP_PASS:
        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = SMTP_USER
            msg["To"] = recipient

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_USER, [recipient], msg.as_string())
            logger.info("✅ Escalation email sent via SMTP to %s", recipient)
        except Exception as err:
            logger.error("⚠️ Failed to send SMTP escalation email to %s: %s", recipient, err)
    else:
        logger.info("📧 [ON-CALL EMAIL DISPATCH SIMULATION] To: %s | Subject: %s", recipient, subject)


def mark(action_id, status, error=None):
    update = {"status": status, "executed_at": datetime.now(timezone.utc)}
    if error:
        update["error"] = str(error)
    actions_col.update_one({"_id": action_id}, {"$set": update})


def agent_url_for(host_ip):
    if not host_ip:
        return f"http://agent:{AGENT_PORT}"
    return f"http://agent:{AGENT_PORT}"


def get_working_agent_url(primary_url):
    urls = [primary_url, f"http://localhost:{AGENT_PORT}"] if primary_url else [f"http://localhost:{AGENT_PORT}"]
    for url in urls:
        if not url:
            continue
        try:
            resp = requests.get(f"{url}/health", timeout=2)
            if resp.status_code == 200:
                return url
        except requests.exceptions.RequestException:
            pass
    return primary_url or f"http://agent:{AGENT_PORT}"


def execute_restart(agent_url, service, target_container):
    url = get_working_agent_url(agent_url)
    if not target_container or target_container == "unknown":
        target_container = service  # fall back to the service's primary container name
        resp = requests.get(f"{url}/containers/{service}", timeout=AGENT_TIMEOUT_SEC)
        if resp.status_code == 200:
            containers = resp.json().get("containers", [])
            if containers:
                for c in containers:
                    requests.post(f"{url}/restart/{c['name']}", timeout=AGENT_TIMEOUT_SEC)
                return {"status": "success", "action": "restart", "service": service, "host_ip": url.split("//")[-1].split(":")[0]}
            else:
                # Try single container restart
                resp_single = requests.post(f"{url}/restart/{service}", timeout=AGENT_TIMEOUT_SEC)
                if resp_single.status_code == 200:
                    return resp_single.json()
                raise Exception(f"No containers found for service '{service}' on agent {url}")

    resp = requests.post(f"{url}/restart/{target_container}", timeout=AGENT_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()


def execute_scale(agent_url, service, desired_replicas, host_ip):
    url = get_working_agent_url(agent_url)
    payload = {
        "desired_replicas": desired_replicas,
        "image": SERVICE_IMAGES.get(service, f"{service}:latest"),
    }
    resp = requests.post(f"{url}/scale/{service}", json=payload, timeout=AGENT_TIMEOUT_SEC)
    resp.raise_for_status()

    desired_state_col.update_one(
        {"service": service},
        {"$set": {
            "desired_replicas": desired_replicas,
            "host_ip": host_ip,
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    return resp.json()


def process_pending_actions():
    pending = list(actions_col.find({"status": "PENDING"}))
    for doc in pending:
        service = doc["service"]
        action = doc["action"]
        host_ip = doc.get("host_ip")
        agent_url = doc.get("agent_url") or agent_url_for(host_ip)

        try:
            if action in ("TARGETED_RESTART", "RESTART"):
                target = doc.get("target_container") or service
                result = execute_restart(agent_url, service, target)
            elif action == "GLOBAL_RESTART":
                result = execute_restart(agent_url, service, target_container=service)
            elif action in ("SCALE_UP", "SCALE"):
                data = desired_state_col.find_one({"service": service})
                desired_replicas = doc.get("desired_replicas") or (data["desired_replicas"] if data else 3)
                result = execute_scale(agent_url, service, desired_replicas, host_ip=host_ip)
            elif action == "SCALE_DOWN":
                data = desired_state_col.find_one({"service": service})
                desired_replicas = doc.get("desired_replicas") or 1
                result = execute_scale(agent_url, service, desired_replicas, host_ip=host_ip)
            elif action == "ESCALATE_ON_CALL":
                send_escalation_email(service, doc)
                result = {"status": "success", "escalated": True, "recipient": DEFAULT_ESCALATION_EMAIL}
            else:
                logger.warning("Unknown action type '%s' for %s, skipping", action, service)
                mark(doc["_id"], "FAILED", error="unknown action type")
                continue

            mark(doc["_id"], "SUCCESS")
            logger.info("Executed %s for %s @ %s -> %s", action, service, host_ip, result)

        except requests.exceptions.RequestException as e:
            logger.error("Agent unreachable at %s for %s (%s): %s", agent_url, service, action, e)
            mark(doc["_id"], "FAILED", error=e)
        except Exception as e:
            logger.error("Execution failed for %s (%s): %s", service, action, e, exc_info=True)
            mark(doc["_id"], "FAILED", error=e)


def reconcile_desired_state():
    """
    Safety net: periodically verify actual replica counts on each service's
    last-known host match desired_state, independent of whatever event
    originally triggered the change. Same idea as Kubernetes' ReplicaSet
    controller re-checking actual pod count against spec.replicas on every
    tick, not just in response to a single creation event.
    """
    for state in desired_state_col.find({}):
        service = state["service"]
        desired = state.get("desired_replicas", 1)
        host_ip = state.get("host_ip")
        agent_url = agent_url_for(host_ip)
        if not agent_url:
            continue
        try:
            resp = requests.get(f"{agent_url}/containers/{service}", timeout=AGENT_TIMEOUT_SEC)
            resp.raise_for_status()
            running = [c for c in resp.json()["containers"] if c["status"] == "running"]
            if len(running) != desired:
                logger.info(
                    "Drift detected for %s @ %s: desired=%d actual=%d, reconciling",
                    service, host_ip, desired, len(running),
                )
                execute_scale(agent_url, service, desired_replicas=desired, host_ip=host_ip)
        except requests.exceptions.RequestException as e:
            logger.error("Could not reach agent %s for %s during drift check: %s", agent_url, service, e)


def reconcile_loop():
    logger.info("Controller starting (host_ip-driven, no static registry)")
    while True:
        try:
            process_pending_actions()
            reconcile_desired_state()
        except Exception as e:
            logger.error("Reconcile loop error: %s", e, exc_info=True)
        time.sleep(RECONCILE_INTERVAL_SEC)


if __name__ == "__main__":
    reconcile_loop()
