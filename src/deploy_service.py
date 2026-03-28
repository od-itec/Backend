"""Buildpack-based build orchestration using Kaniko (daemonless, K8s-native).

Workflow:
1. User clicks "Deploy" → backend creates a Kaniko Job in K8s
2. Kaniko reads source from the user pod's /workspace via an initContainer
3. Kaniko builds the image using Cloud Native Buildpacks builder
4. Image is pushed to a registry (configurable)
5. A Deployment + Service are created to run the built image
"""

import logging
import os
import time
from typing import Optional
from uuid import UUID

from kubernetes import client
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream

from k8s_service import (
    NAMESPACE,
    _core,
    _pod_name,
    exec_in_pod,
)

logger = logging.getLogger(__name__)

_batch = client.BatchV1Api()
_apps = client.AppsV1Api()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REGISTRY = os.getenv("DEPLOY_REGISTRY", "localhost:5000")
BUILDPACK_BUILDER = os.getenv(
    "BUILDPACK_BUILDER", "gcr.io/buildpacks/builder:google-22"
)
DEPLOY_NAMESPACE = os.getenv("DEPLOY_NAMESPACE", NAMESPACE)


# ---------------------------------------------------------------------------
# Build with Kaniko
# ---------------------------------------------------------------------------
def _build_job_name(user_id: UUID, pod_type: str) -> str:
    return f"build-{str(user_id)[:8]}-{pod_type}"


def _image_tag(user_id: UUID, pod_type: str) -> str:
    return f"{REGISTRY}/itec-{str(user_id)[:8]}-{pod_type}:latest"


def start_build(user_id: UUID, pod_type: str) -> str:
    """Create a K8s Job that runs pack build via lifecycle-based approach.

    We use the pack CLI inside a builder pod rather than Kaniko since buildpacks
    produce OCI images differently from Dockerfiles.

    Strategy: create a Job that:
    1. Copies source from the user's workspace pod via an initContainer
    2. Runs `pack build` with the specified builder
    3. Pushes the resulting image to the registry
    """
    job_name = _build_job_name(user_id, pod_type)
    image_tag = _image_tag(user_id, pod_type)
    source_pod = _pod_name(user_id, pod_type)

    # Clean up any previous job
    try:
        _batch.delete_namespaced_job(
            name=job_name,
            namespace=NAMESPACE,
            body=client.V1DeleteOptions(propagation_policy="Foreground"),
        )
        # Wait for cleanup
        time.sleep(2)
    except ApiException as e:
        if e.status != 404:
            raise

    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=NAMESPACE,
            labels={"app": "itec-build", "itec/user-id": str(user_id)},
        ),
        spec=client.V1JobSpec(
            backoff_limit=0,
            ttl_seconds_after_finished=300,
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    # Init container copies source from workspace pod
                    init_containers=[
                        client.V1Container(
                            name="copy-source",
                            image="bitnami/kubectl:latest",
                            command=[
                                "sh",
                                "-c",
                                f"kubectl cp {NAMESPACE}/{source_pod}:/workspace /source --container=workspace",
                            ],
                            volume_mounts=[
                                client.V1VolumeMount(
                                    name="source-vol", mount_path="/source"
                                ),
                            ],
                        )
                    ],
                    containers=[
                        client.V1Container(
                            name="builder",
                            image="buildpacksio/pack:latest",
                            command=[
                                "pack",
                                "build",
                                image_tag,
                                "--path",
                                "/source",
                                "--builder",
                                BUILDPACK_BUILDER,
                                "--publish",
                            ],
                            volume_mounts=[
                                client.V1VolumeMount(
                                    name="source-vol", mount_path="/source"
                                ),
                            ],
                        )
                    ],
                    volumes=[
                        client.V1Volume(
                            name="source-vol",
                            empty_dir=client.V1EmptyDirVolumeSource(),
                        ),
                    ],
                )
            ),
        ),
    )

    _batch.create_namespaced_job(namespace=NAMESPACE, body=job)
    logger.info("Created build job %s → %s", job_name, image_tag)
    return job_name


def get_build_status(user_id: UUID, pod_type: str) -> dict:
    """Return job status and log tail."""
    job_name = _build_job_name(user_id, pod_type)
    try:
        job = _batch.read_namespaced_job(name=job_name, namespace=NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return {"status": "not_found"}
        raise

    conditions = job.status.conditions or []
    if job.status.succeeded:
        return {"status": "succeeded", "image": _image_tag(user_id, pod_type)}
    if job.status.failed:
        reason = conditions[0].reason if conditions else "Unknown"
        return {"status": "failed", "reason": reason}

    return {"status": "building"}


def get_build_logs(user_id: UUID, pod_type: str) -> str:
    """Return the builder job's logs."""
    job_name = _build_job_name(user_id, pod_type)
    # Find pod belonging to this job
    pods = _core.list_namespaced_pod(
        namespace=NAMESPACE,
        label_selector=f"job-name={job_name}",
    )
    if not pods.items:
        return ""
    pod_name = pods.items[0].metadata.name
    try:
        return _core.read_namespaced_pod_log(
            name=pod_name,
            namespace=NAMESPACE,
            container="builder",
            tail_lines=200,
        )
    except ApiException:
        return ""


# ---------------------------------------------------------------------------
# Deploy the built image
# ---------------------------------------------------------------------------
def _deploy_name(user_id: UUID, pod_type: str) -> str:
    return f"deploy-{str(user_id)[:8]}-{pod_type}"


def deploy_image(user_id: UUID, pod_type: str) -> dict:
    """Create (or update) a Deployment + Service to run the built image."""
    name = _deploy_name(user_id, pod_type)
    image = _image_tag(user_id, pod_type)
    labels = {
        "app": "itec-deploy",
        "itec/user-id": str(user_id),
        "itec/pod-type": pod_type,
    }

    # --- Deployment ---
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=name, namespace=DEPLOY_NAMESPACE, labels=labels
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="app",
                            image=image,
                            ports=[client.V1ContainerPort(container_port=8080)],
                            resources=client.V1ResourceRequirements(
                                requests={"cpu": "100m", "memory": "128Mi"},
                                limits={"cpu": "500m", "memory": "256Mi"},
                            ),
                        )
                    ]
                ),
            ),
        ),
    )

    try:
        _apps.read_namespaced_deployment(name=name, namespace=DEPLOY_NAMESPACE)
        _apps.replace_namespaced_deployment(
            name=name, namespace=DEPLOY_NAMESPACE, body=deployment
        )
        logger.info("Updated deployment %s", name)
    except ApiException as e:
        if e.status == 404:
            _apps.create_namespaced_deployment(
                namespace=DEPLOY_NAMESPACE, body=deployment
            )
            logger.info("Created deployment %s", name)
        else:
            raise

    # --- Service (NodePort) ---
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=name, namespace=DEPLOY_NAMESPACE, labels=labels
        ),
        spec=client.V1ServiceSpec(
            type="NodePort",
            selector=labels,
            ports=[
                client.V1ServicePort(
                    port=8080, target_port=8080, protocol="TCP"
                )
            ],
        ),
    )

    try:
        existing_svc = _core.read_namespaced_service(
            name=name, namespace=DEPLOY_NAMESPACE
        )
        # Keep the allocated NodePort
        svc.spec.ports[0].node_port = existing_svc.spec.ports[0].node_port
        _core.replace_namespaced_service(
            name=name, namespace=DEPLOY_NAMESPACE, body=svc
        )
    except ApiException as e:
        if e.status == 404:
            _core.create_namespaced_service(namespace=DEPLOY_NAMESPACE, body=svc)
        else:
            raise

    # Read back to get the NodePort
    svc = _core.read_namespaced_service(name=name, namespace=DEPLOY_NAMESPACE)
    node_port = svc.spec.ports[0].node_port

    return {
        "deployment": name,
        "image": image,
        "node_port": node_port,
        "url": f"http://localhost:{node_port}",
    }


def get_deploy_status(user_id: UUID, pod_type: str) -> Optional[dict]:
    """Check if a deployment exists and its status."""
    name = _deploy_name(user_id, pod_type)
    try:
        dep = _apps.read_namespaced_deployment(
            name=name, namespace=DEPLOY_NAMESPACE
        )
        svc = _core.read_namespaced_service(
            name=name, namespace=DEPLOY_NAMESPACE
        )
        node_port = svc.spec.ports[0].node_port
        ready = dep.status.ready_replicas or 0

        return {
            "deployment": name,
            "image": dep.spec.template.spec.containers[0].image,
            "ready_replicas": ready,
            "node_port": node_port,
            "url": f"http://localhost:{node_port}",
        }
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def delete_deployment(user_id: UUID, pod_type: str) -> bool:
    """Remove the deployment and service."""
    name = _deploy_name(user_id, pod_type)
    deleted = False
    for kind, api_call in [
        ("deployment", lambda: _apps.delete_namespaced_deployment(name=name, namespace=DEPLOY_NAMESPACE)),
        ("service", lambda: _core.delete_namespaced_service(name=name, namespace=DEPLOY_NAMESPACE)),
    ]:
        try:
            api_call()
            deleted = True
        except ApiException as e:
            if e.status != 404:
                raise
    return deleted
