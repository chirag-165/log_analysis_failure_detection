"""
policy_engine.py  ── Fixed & Optimal Version

PURE DECISION LOGIC — no Docker calls, no HTTP, no subprocess.
The Controller is the only component that reads recovery_actions and acts.

CHANGES FROM ORIGINAL:
════════════════════════
1. DEPENDENCY CHECK REMOVED
   Removed: check_dependency_health() and DEPENDENCY_MAP.
   Reason: it caused correct HIGH-risk services to be silently skipped
   (WAITING_FOR_DEPENDENCY) without any visible feedback, making the
   self-healing appear broken. It added complexity without clear benefit
   for a prototype. Removed entirely as requested.

2. SMARTER RECOVERY POLICY (Issue #9)
   Added: repeated failure escalation.
   If a service has been restarted 3+ times in the last 10 minutes
   without recovering, escalate from restart → SCALE_UP.
   Rationale: repeated restarts of the same failing container suggest
   a resource problem, not a transient crash. More capacity (scale up)
   is more likely to help than another restart.

3. SCALE-DOWN POLICY (Issue #5)
   Added: maybe_scale_down() called by log_processor on every LOW window.
   Only fires after SCALE_DOWN_STABLE_WIN consecutive LOW windows,
   preventing oscillation when a service briefly recovers between failures.
   Updates desired_state immediately so Controller drift-check sees target.

4. ENRICHED ACTION DOCUMENTS
   Added: decision_source, decision_reason, mttr_ms (null until Controller
   fills it), execution_ms (null until Controller fills it).
   Existing fields preserved exactly for dashboard compatibility.
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
AGENT_PORT               = int(os.getenv("AGENT_PORT", "8000"))
SCALE_UP_REPLICAS        = int(os.getenv("SCALE_UP_REPLICAS", "3"))
SCALE_DOWN_REPLICAS      = int(os.getenv("SCALE_DOWN_REPLICAS", "1"))
# Traffic delta above which scale-up preferred over restart
SCALE_TRAFFIC_DELTA_THR  = float(os.getenv("SCALE_TRAFFIC_DELTA_THR", "0.6"))
# Consecutive LOW windows before scale-down fires
SCALE_DOWN_STABLE_WIN    = int(os.getenv("SCALE_DOWN_STABLE_WIN", "3"))
# Repeated failure: if restarted N+ times recently, escalate to SCALE_UP
REPEATED_FAILURE_N       = int(os.getenv("REPEATED_FAILURE_N", "3"))
REPEATED_FAILURE_MIN     = int(os.getenv("REPEATED_FAILURE_MIN", "10"))


class PolicyEngine:

    def __init__(self, mongo_uri=None, db_name="log_analysis_dashboard"):
        mongo_uri = mongo_uri or os.getenv("MONGO_URI")
        if not mongo_uri:
            raise ValueError("MONGO_URI is required for PolicyEngine")

        self.client = MongoClient(mongo_uri)
        self.db     = self.client[db_name]

        # All existing collections preserved
        self.history       = self.db["window_history"]
        self.actions       = self.db["recovery_actions"]
        self.desired_state = self.db["desired_state"]

    # ── Repeated failure detection ─────────────────────────────────────────

    def _recent_restart_count(self, service):
        """
        Count successful restart actions for this service in the last
        REPEATED_FAILURE_MIN minutes. If >= REPEATED_FAILURE_N, the
        service keeps failing after restarts → escalate to SCALE_UP.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=REPEATED_FAILURE_MIN)
        return self.actions.count_documents({
            "service":    service,
            "action":     {"$in": ["TARGETED_RESTART", "GLOBAL_RESTART"]},
            "status":     "SUCCESS",
            "created_at": {"$gte": cutoff},
        })

    # ── Scale-down policy ──────────────────────────────────────────────────

    def maybe_scale_down(self, service, low_streak):
        """
        Called from log_processor on every LOW-risk window.

        Only fires SCALE_DOWN after low_streak >= SCALE_DOWN_STABLE_WIN
        AND only when desired_replicas > SCALE_DOWN_REPLICAS.

        This prevents replicas accumulating forever after a load spike
        resolves — completes the self-healing loop (up AND down).
        """
        state = self.desired_state.find_one({"service": service})
        if not state or state.get("desired_replicas", 1) <= SCALE_DOWN_REPLICAS:
            return  # already at baseline

        if low_streak < SCALE_DOWN_STABLE_WIN:
            return  # not stable enough yet

        host_ip = state.get("host_ip")
        logger.info(
            "Scale-down triggered for %s (%d consecutive LOW windows)",
            service, low_streak
        )

        self._record_action(
            service          = service,
            action           = "SCALE_DOWN",
            target_container = None,
            host_ip          = host_ip,
            status           = "PENDING",
            decision_source  = "Policy",
            decision_reason  = (
                f"Stable LOW risk for {low_streak} windows — "
                f"restoring to {SCALE_DOWN_REPLICAS} replica(s)"
            ),
        )
        # Update desired_state immediately for drift-reconciliation
        self.desired_state.update_one(
            {"service": service},
            {"$set": {
                "desired_replicas": SCALE_DOWN_REPLICAS,
                "updated_at":       datetime.now(timezone.utc),
            }},
        )

    # ── Main decision entry point ──────────────────────────────────────────

    def decide_action(
        self, service, risk, top_container, host_ip, traffic_delta,
        decision_source="Hybrid", decision_reason=""
    ):
        """
        Decide and record recovery action. Never executes anything.

        Policy logic (in order):
          1. Guard: no host_ip → can't target anything
          2. Repeated failure escalation → SCALE_UP
          3. High traffic load spike     → SCALE_UP
          4. Known bad container         → TARGETED_RESTART
          5. Unknown container           → GLOBAL_RESTART
        """
        if risk != "HIGH":
            return "NO_ACTION"

        if not host_ip:
            logger.error("No host_ip for %s — cannot dispatch action", service)
            self._record_action(
                service, "NO_HOST_KNOWN", top_container, host_ip,
                "FAILED", decision_source, "host_ip missing"
            )
            return "NO_HOST_KNOWN"

        # Repeated failure check: if the service keeps failing after
        # restarts, escalate to SCALE_UP instead of restarting again
        recent_restarts = self._recent_restart_count(service)
        if recent_restarts >= REPEATED_FAILURE_N:
            action = "SCALE_UP"
            decision_reason = (
                f"{decision_reason} | escalated: "
                f"{recent_restarts} restarts in {REPEATED_FAILURE_MIN}min"
            )
            logger.warning(
                "Escalating to SCALE_UP for %s (%d recent restarts)",
                service, recent_restarts
            )
        elif traffic_delta > SCALE_TRAFFIC_DELTA_THR:
            action = "SCALE_UP"
        elif top_container and top_container != "unknown":
            action = "TARGETED_RESTART"
        else:
            action = "GLOBAL_RESTART"

        self._record_action(
            service, action, top_container, host_ip,
            "PENDING", decision_source, decision_reason
        )
        return action

    # ── Internal record helper ─────────────────────────────────────────────

    def _record_action(
        self, service, action, target_container, host_ip,
        status, decision_source, decision_reason
    ):
        """
        Write recovery_action document to MongoDB.

        Existing fields (dashboard-compatible): service, action,
        target_container, host_ip, agent_url, status, created_at,
        executed_at, error — all preserved exactly.

        New fields: decision_source, decision_reason, mttr_ms,
        execution_ms — added for audit and MTTR calculation.
        The Controller fills mttr_ms and execution_ms on SUCCESS.
        """
        doc = {
            # Original fields — preserved exactly
            "service":          service,
            "action":           action,
            "target_container": target_container,
            "host_ip":          host_ip,
            "agent_url":        f"http://agent:{AGENT_PORT}" if host_ip else None,
            "status":           status,
            "created_at":       datetime.now(timezone.utc),
            "executed_at":      None,
            "error":            None,
            # New fields — audit and MTTR
            "decision_source":  decision_source,
            "decision_reason":  decision_reason,
            "mttr_ms":          None,   # filled by Controller on SUCCESS
            "execution_ms":     None,   # filled by Controller
        }
        result = self.actions.insert_one(doc)

        # Maintain desired_state for Controller drift-reconciliation
        if action == "SCALE_UP":
            self.desired_state.update_one(
                {"service": service},
                {"$set": {
                    "desired_replicas": SCALE_UP_REPLICAS,
                    "host_ip":          host_ip,
                    "updated_at":       datetime.now(timezone.utc),
                }},
                upsert=True,
            )

        logger.warning(
            "Action recorded: %s for %s [%s] id=%s",
            action, service, decision_source, result.inserted_id
        )
        return result.inserted_id
