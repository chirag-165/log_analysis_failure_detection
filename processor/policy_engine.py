"""
policy_engine.py

PURE DECISION LOGIC. No Docker calls, no subprocess, no network calls to
agents. Given a risk assessment (and the host_ip the offending container was
last seen on), it decides what SHOULD happen and writes that intent to
MongoDB as a recovery_action document.

The Controller is the only component that reads recovery_actions and acts.
This file doesn't know or care whether the target host is on the same
machine, a different EC2 instance, or anything else — that's the entire
point of carrying host_ip as data instead of looking it up from a config.
"""

import os
import logging
from datetime import datetime, timezone
from pymongo import MongoClient

logger = logging.getLogger(__name__)

SCALE_TRAFFIC_DELTA_THRESHOLD = 0.7
AGENT_PORT = int(os.getenv("AGENT_PORT", "8000"))


class PolicyEngine:
    def __init__(self, mongo_uri=None, db_name="log_analysis_dashboard"):
        mongo_uri = mongo_uri or os.getenv("MONGO_URI")
        if not mongo_uri:
            raise ValueError("MONGO_URI is required for PolicyEngine")

        self.client = MongoClient(mongo_uri)
        self.db = self.client[db_name]
        self.history = self.db["window_history"]
        self.actions = self.db["recovery_actions"]
        self.desired_state = self.db["desired_state"]

        # Service -> services it depends on. If a dependency is currently
        # HIGH risk, hold off acting on the dependent service since fixing
        # it won't address the actual root cause.
        self.DEPENDENCY_MAP = {
            "auth-service": ["payment-service"],
            "order-service": ["payment-service"],
            "payment-service": [],
        }

    def check_dependency_health(self, service):
        for dep in self.DEPENDENCY_MAP.get(service, []):
            latest_dep_state = self.history.find_one(
                {"service": dep}, sort=[("timestamp", -1)]
            )
            if latest_dep_state and latest_dep_state.get("risk") == "HIGH":
                logger.warning(
                    "Dependency alert: %s is downstream of unhealthy %s", service, dep
                )
                return False
        return True

    def decide_action(self, service, risk, top_container, host_ip, traffic_delta):
        """
        host_ip: the last known IP of the host that produced top_container's
        logs (passed in by log_processor.py, sourced from the log entries
        themselves — see HOST_IP CACHING note in log_processor.py).

        Returns the action name for logging only. Never executes anything.
        """
        if risk != "HIGH":
            return "NO_ACTION"

        if not self.check_dependency_health(service):
            logger.info("Waiting for dependency recovery before acting on %s", service)
            self._record_action(service, "WAITING_FOR_DEPENDENCY", top_container, host_ip, status="SKIPPED")
            return "WAITING_FOR_DEPENDENCY"

        if not host_ip:
            # No host to target at all — nothing we can safely send anywhere.
            logger.error("No known host_ip for %s, cannot dispatch action", service)
            self._record_action(service, "NO_HOST_KNOWN", top_container, host_ip, status="FAILED")
            return "NO_HOST_KNOWN"

        if traffic_delta > SCALE_TRAFFIC_DELTA_THRESHOLD:
            action = "SCALE_UP"
        elif top_container and top_container != "unknown":
            action = "TARGETED_RESTART"
        else:
            action = "GLOBAL_RESTART"

        self._record_action(service, action, top_container, host_ip, status="PENDING")
        return action

    def _record_action(self, service, action, target_container, host_ip, status):
        doc = {
            "service": service,
            "action": action,
            "target_container": target_container,
            "host_ip": host_ip,
            "agent_url": f"http://agent:{AGENT_PORT}" if host_ip else None,
            "status": status,  # PENDING | SUCCESS | FAILED | SKIPPED
            "created_at": datetime.now(timezone.utc),
            "executed_at": None,
            "error": None,
        }
        result = self.actions.insert_one(doc)

        if action == "SCALE_UP":
            self.desired_state.update_one(
                {"service": service},
                {"$set": {
                    "desired_replicas": 3,
                    "host_ip": host_ip,
                    "updated_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )

        logger.warning("Recorded action %s for %s @ %s (id=%s)", action, service, host_ip, result.inserted_id)
        return result.inserted_id
