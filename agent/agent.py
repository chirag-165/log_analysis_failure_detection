"""
agent.py

Runs ONE per host (one per EC2 instance in production). Has local-only
access to that host's Docker socket via the mounted /var/run/docker.sock.
Exposes a small constrained HTTP API so the Controller never needs direct
Docker access or any hardcoded knowledge of where containers live - the
Controller gets that from host_ip carried in recovery_action documents,
which ultimately came from the logs themselves.

This is the kubelet analog: a node-local agent with a narrow, purpose-built
API, not a generic Docker API proxy.
"""

import os
import socket
import logging
import random
from contextlib import asynccontextmanager

import docker
from docker.errors import NotFound, APIError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [AGENT] - %(message)s")
logger = logging.getLogger(__name__)

docker_client: docker.DockerClient | None = None

# Identifies which physical host this agent instance is running on - useful
# when debugging which agent actually answered a request during the demo.
SELF_HOST_IP = os.getenv("HOST_IP", socket.gethostname())


@asynccontextmanager
async def lifespan(app: FastAPI):
    global docker_client
    docker_client = docker.from_env()
    logger.info("Agent online on host=%s, connected to local Docker daemon", SELF_HOST_IP)
    yield
    docker_client.close()


app = FastAPI(title="Self-Healing Agent", lifespan=lifespan)


class ScaleRequest(BaseModel):
    desired_replicas: int
    image: str
    network: str = "log-analysis_monitoring"  # default to the shared monitoring network for simplicity


@app.get("/health")
def health():
    try:
        docker_client.ping()
        return {"status": "ok", "host_ip": SELF_HOST_IP}
    except Exception as e:
        raise HTTPException(503, f"Docker daemon unreachable: {e}")



@app.post("/restart/{container_name}")
def restart_container(container_name: str):
    try:
        container = docker_client.containers.get(container_name)
        container.restart(timeout=10)
        logger.warning("Restarted container: %s (host=%s)", container_name, SELF_HOST_IP)
        return {
            "status": "success",
            "action": "restart",
            "container": container_name,
            "host_ip": SELF_HOST_IP,
        }
    except NotFound:
        raise HTTPException(
            404, f"Container '{container_name}' not found on host {SELF_HOST_IP}"
        )
    except Exception as e:
        logger.error("Error restarting container %s on host %s: %s", container_name, SELF_HOST_IP, e)
        raise HTTPException(500, f"Error restarting container: {e}")


@app.post("/scale/{service}")
def scale_service(service: str, req: ScaleRequest):
    """
    Reconciles the number of running containers labeled service=<service>
    on THIS host to req.desired_replicas. Idempotent: safe to call
    repeatedly with the same target.
    """
    try:
        running = docker_client.containers.list(filters={"label": f"service={service}"})
        current = len(running)
        actions = []

        if current < req.desired_replicas:
            for i in range(req.desired_replicas - current):
                name = f"{service}-{random.randint(1000, 9999)}"
                docker_client.containers.run(
                    image=req.image,
                    name=name,
                    detach=True,
                    network=req.network,
                    labels={"service": service, "managed-by": "self-healing-agent"},
                    environment={
                        "SERVICE_NAME": service,
                        "HOST_IP": SELF_HOST_IP,
                        "COLLECTOR_URL": "http://collector:5001/logs",
                    },
                )
                actions.append(f"created {name}")
                logger.warning("Scaled up: created %s on host=%s", name, SELF_HOST_IP)

        elif current > req.desired_replicas:
            excess = running[: current - req.desired_replicas]
            for container in excess:
                container.stop(timeout=5)
                container.remove()
                actions.append(f"removed {container.name}")
                logger.warning("Scaled down: removed %s on host=%s", container.name, SELF_HOST_IP)

        final_count = len(
            docker_client.containers.list(filters={"label": f"service={service}"})
        )
        return {
            "status": "success",
            "service": service,
            "host_ip": SELF_HOST_IP,
            "previous_count": current,
            "current_count": final_count,
            "actions": actions,
        }

    except Exception as e:
        logger.error("Error scaling service %s on host %s: %s", service, SELF_HOST_IP, e)
        raise HTTPException(500, f"Error scaling service: {e}")


@app.get("/containers/{service}")
def list_service_containers(service: str):
    """Lets the Controller verify actual state for a service on this host."""
    running = docker_client.containers.list(all=True, filters={"label": f"service={service}"})
    return {
        "service": service,
        "host_ip": SELF_HOST_IP,
        "containers": [
            {"name": c.name, "status": c.status, "id": c.short_id} for c in running
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
