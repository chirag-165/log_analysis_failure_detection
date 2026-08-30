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


def mark(action_id, status, error=None):
    update = {"status": status, "executed_at": datetime.now(timezone.utc)}
    if error:
        update["error"] = str(error)
    actions_col.update_one({"_id": action_id}, {"$set": update})


def agent_url_for(host_ip):
    if not host_ip:
        return None
    return f"http://agent:{AGENT_PORT}"


def execute_restart(agent_url, service, target_container):
    if not target_container or target_container == "unknown":
        target_container = service  # fall back to the service's primary container name
        resp = requests.get(f"{agent_url}/containers/{service}", timeout=AGENT_TIMEOUT_SEC)
        if resp.status_code == 200:
            containers = resp.json().get("containers", [])
            if containers:
                for c in containers:
                    requests.post(f"{agent_url}/restart/{c['id']}", timeout=AGENT_TIMEOUT_SEC)
            else:
                raise Exception(f"No containers found for service '{service}' on agent {agent_url}")
            return {"status": "success", "action": "restart", "service": service, "host_ip": agent_url.split("//")[1].split(":")[0]}


    resp = requests.post(f"{agent_url}/restart/{target_container}", timeout=AGENT_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()


def execute_scale(agent_url, service, desired_replicas, host_ip):
    payload = {
        "desired_replicas": desired_replicas,
        "image": SERVICE_IMAGES.get(service, f"{service}:latest"),
    }
    resp = requests.post(f"{agent_url}/scale/{service}", json=payload, timeout=AGENT_TIMEOUT_SEC)
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

        if not agent_url:
            logger.error("No agent_url/host_ip on action doc for %s, cannot execute", service)
            mark(doc["_id"], "FAILED", error="missing host_ip/agent_url on action document")
            continue

        try:
            if action == "TARGETED_RESTART":
                result = execute_restart(agent_url, service, doc.get("target_container"))
            elif action == "GLOBAL_RESTART":
                result = execute_restart(agent_url, service, target_container=service)
            elif action == "SCALE_UP":
                data = desired_state_col.find_one({"service": service})
                desired_replicas = data["desired_replicas"] if data else 3
                result = execute_scale(agent_url, service, desired_replicas, host_ip=host_ip)
            elif action == "SCALE_DOWN":
                mark(doc["_id"], "SUCCESS")
                continue  # No action needed; desired_state already reflects the scale-down
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
