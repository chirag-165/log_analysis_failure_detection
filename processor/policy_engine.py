import os
import logging
from pymongo import MongoClient

class PolicyEngine:
    def __init__(self, mongo_uri="mongodb+srv://chirag:chirag@cluster0.kvmfrst.mongodb.net/log_analysis_dashboard?retryWrites=true&w=majority"):
        self.client = MongoClient(mongo_uri)
        self.db = self.client["predictive_system"]
        self.collection = self.db["window_history"]

        # Dependency Map: Service -> depends on these services
        self.DEPENDENCY_MAP = {
            "auth-service": ["payment-service"],
            "order-service": ["payment-service"],
            "payment-service": []
        }

    def check_dependency_health(self, service):
        """
        Returns False if a dependency is in HIGH RISK, blocking recovery for the parent.
        """
        dependencies = self.DEPENDENCY_MAP.get(service, [])
        for dep in dependencies:
            # Look up the latest risk assessment for the dependency
            latest_dep_state = self.collection.find_one(
                {"service": dep}, 
                sort=[("timestamp", -1)]
            )
            
            if latest_dep_state and latest_dep_state.get("risk") == "HIGH":
                logging.warning(f"🚫 Dependency Alert: {service} is failing because {dep} is HIGH RISK.")
                return False # Dependency is unhealthy
        return True # All clear

    def decide_action(self, service, risk, top_container, traffic_delta):
        if risk != "HIGH":
            return "NO_ACTION"

        # 1. Dependency Awareness (The "Root Cause" Check)
        if not self.check_dependency_health(service):
            logging.info(f"⏳ Waiting for dependency recovery before acting on {service}")
            return "WAITING_FOR_DEPENDENCY"

        # 2. Decision Logic: Scaling vs. Restarting
        if traffic_delta > 1.5:
            return self.execute_scale(service)
        
        # 3. Decision Logic: Targeted vs. Global
        return self.execute_restart(service, top_container)

    def execute_scale(self, service):
        logging.warning(f"🚀 ACTION: Scaling UP {service} (Load spike detected)")
        # os.system(f"docker-compose up -d --scale {service}=2")
        return "SCALE_UP"

    def execute_restart(self, service, top_container):
        if top_container and top_container != "unknown":
            logging.warning(f"🔄 ACTION: Targeted Restart of {top_container}")
            # os.system(f"docker restart {top_container}")
            return "TARGETED_RESTART"
        else:
            logging.warning(f"🔄 ACTION: Global Restart of {service}")
            # os.system(f"docker restart {service}")
            return "GLOBAL_RESTART"