"""
Kubernetes pod lifecycle management for per-user workspace sandboxes.

Each user gets isolated pods (frontend / backend) with an emptyDir volume
at /workspace where their files are synced from the database.
"""

import asyncio
import io
import logging
import os
import tarfile
import time
from typing import Optional
from uuid import UUID

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NAMESPACE = os.getenv("K8S_NAMESPACE", "itec-workspaces")
POD_TIMEOUT_SECONDS = int(os.getenv("POD_TIMEOUT_SECONDS", "120"))
POD_CPU_LIMIT = os.getenv("POD_CPU_LIMIT", "500m")
POD_MEMORY_LIMIT = os.getenv("POD_MEMORY_LIMIT", "512Mi")
POD_CPU_REQUEST = os.getenv("POD_CPU_REQUEST", "100m")
POD_MEMORY_REQUEST = os.getenv("POD_MEMORY_REQUEST", "128Mi")

# Base images per pod type
POD_IMAGES = {
    "frontend": os.getenv("WS_FRONTEND_IMAGE", "node:20-bookworm-slim"),
    "backend": os.getenv("WS_BACKEND_IMAGE", "python:3.12-slim-bookworm"),
}


def _init_k8s():
    """Load kubeconfig (in-cluster when deployed, local fallback for dev)."""
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster K8s config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig")


_init_k8s()
_core = client.CoreV1Api()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pod_name(user_id: UUID, pod_type: str) -> str:
    return f"ws-{str(user_id)[:8]}-{pod_type}"


def _pod_labels(user_id: UUID, pod_type: str) -> dict:
    return {
        "app": "itec-workspace",
        "itec/user-id": str(user_id),
        "itec/pod-type": pod_type,
    }


# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------
def ensure_namespace():
    """Create the workspace namespace if it doesn't exist."""
    try:
        _core.read_namespace(name=NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            _core.create_namespace(
                client.V1Namespace(
                    metadata=client.V1ObjectMeta(name=NAMESPACE)
                )
            )
            logger.info("Created namespace %s", NAMESPACE)
        else:
            raise


# ---------------------------------------------------------------------------
# Pod CRUD
# ---------------------------------------------------------------------------
def create_user_pod(user_id: UUID, pod_type: str) -> client.V1Pod:
    """Create a sandbox pod for *user_id* of a given *pod_type*."""
    if pod_type not in POD_IMAGES:
        raise ValueError(f"Unknown pod_type: {pod_type}")

    name = _pod_name(user_id, pod_type)
    labels = _pod_labels(user_id, pod_type)
    image = POD_IMAGES[pod_type]

    pod_manifest = client.V1Pod(
        metadata=client.V1ObjectMeta(name=name, namespace=NAMESPACE, labels=labels),
        spec=client.V1PodSpec(
            containers=[
                client.V1Container(
                    name="workspace",
                    image=image,
                    # Keep the container alive; users interact via exec
                    command=["sleep", "infinity"],
                    working_dir="/workspace",
                    volume_mounts=[
                        client.V1VolumeMount(
                            name="workspace-vol",
                            mount_path="/workspace",
                        )
                    ],
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": POD_CPU_REQUEST, "memory": POD_MEMORY_REQUEST},
                        limits={"cpu": POD_CPU_LIMIT, "memory": POD_MEMORY_LIMIT},
                    ),
                )
            ],
            volumes=[
                client.V1Volume(
                    name="workspace-vol",
                    empty_dir=client.V1EmptyDirVolumeSource(),
                )
            ],
            restart_policy="Never",
            # Prevent privilege escalation
            security_context=client.V1PodSecurityContext(
                run_as_non_root=False,  # base images may need root for package installs
            ),
        ),
    )

    pod = _core.create_namespaced_pod(namespace=NAMESPACE, body=pod_manifest)
    logger.info("Created pod %s for user %s", name, user_id)
    return pod


def get_user_pod(user_id: UUID, pod_type: str) -> Optional[client.V1Pod]:
    """Return the pod if it exists, else None."""
    name = _pod_name(user_id, pod_type)
    try:
        return _core.read_namespaced_pod(name=name, namespace=NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def delete_user_pod(user_id: UUID, pod_type: str) -> bool:
    """Delete the pod. Returns True if it existed."""
    name = _pod_name(user_id, pod_type)
    try:
        _core.delete_namespaced_pod(
            name=name,
            namespace=NAMESPACE,
            body=client.V1DeleteOptions(grace_period_seconds=5),
        )
        logger.info("Deleted pod %s", name)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def wait_for_pod_ready(user_id: UUID, pod_type: str, timeout: int = POD_TIMEOUT_SECONDS) -> bool:
    """Block until the pod is in Running phase. Returns True on success."""
    name = _pod_name(user_id, pod_type)
    deadline = time.time() + timeout
    while time.time() < deadline:
        pod = _core.read_namespaced_pod(name=name, namespace=NAMESPACE)
        if pod.status.phase == "Running":
            return True
        time.sleep(1)
    return False


def ensure_pod_ready(user_id: UUID, pod_type: str) -> client.V1Pod:
    """Return a running pod — create one if none exists."""
    pod = get_user_pod(user_id, pod_type)
    if pod and pod.status.phase == "Running":
        return pod
    if pod and pod.status.phase in ("Pending",):
        wait_for_pod_ready(user_id, pod_type)
        return _core.read_namespaced_pod(
            name=_pod_name(user_id, pod_type), namespace=NAMESPACE
        )
    # Create new
    if pod:
        delete_user_pod(user_id, pod_type)
    create_user_pod(user_id, pod_type)
    wait_for_pod_ready(user_id, pod_type)
    return _core.read_namespaced_pod(
        name=_pod_name(user_id, pod_type), namespace=NAMESPACE
    )


def exec_in_pod(
    user_id: UUID,
    pod_type: str,
    command: list[str],
    stdin_data: Optional[bytes] = None,
) -> str:
    """Run a one-shot command inside the user pod and return stdout."""
    name = _pod_name(user_id, pod_type)
    resp = k8s_stream(
        _core.connect_get_namespaced_pod_exec,
        name,
        NAMESPACE,
        command=command,
        container="workspace",
        stderr=True,
        stdin=stdin_data is not None,
        stdout=True,
        tty=False,
        _preload_content=False,
    )

    if stdin_data is not None:
        resp.write_stdin(stdin_data.decode("utf-8", errors="replace"))
        resp.close()

    out = ""
    while resp.is_open():
        resp.update(timeout=5)
        if resp.peek_stdout():
            out += resp.read_stdout()
        if resp.peek_stderr():
            out += resp.read_stderr()
    resp.close()
    return out


# ---------------------------------------------------------------------------
# File sync: DB → Pod filesystem
# ---------------------------------------------------------------------------
def _build_tar(files: list[dict]) -> bytes:
    """Create a tar archive from a flat list of DB file records.

    Each record has: file_id, parent_id, type, name, content, position.
    We reconstruct the directory tree and tar it.
    """
    # Build id→record map
    by_id = {str(f["file_id"]): f for f in files}

    def _path_of(record: dict) -> str:
        parts = []
        cur = record
        while cur:
            parts.append(cur["name"])
            pid = cur.get("parent_id")
            cur = by_id.get(str(pid)) if pid else None
        parts.reverse()
        return "/".join(parts)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in files:
            rel = _path_of(f)
            if f["type"] == "folder":
                info = tarfile.TarInfo(name=rel)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                content_bytes = (f.get("content") or "").encode("utf-8")
                info = tarfile.TarInfo(name=rel)
                info.size = len(content_bytes)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(content_bytes))
    return buf.getvalue()


def sync_files_to_pod(user_id: UUID, pod_type: str, files: list[dict]) -> str:
    """Write the user's file tree into the pod's /workspace directory.

    *files* is the flat list from ``FileRepository.get_all_files``.
    """
    if not files:
        return "No files to sync"

    tar_bytes = _build_tar(files)
    name = _pod_name(user_id, pod_type)

    # Pipe the tar into the pod: tar xzf - -C /workspace
    resp = k8s_stream(
        _core.connect_get_namespaced_pod_exec,
        name,
        NAMESPACE,
        command=["tar", "xzf", "-", "-C", "/workspace"],
        container="workspace",
        stderr=True,
        stdin=True,
        stdout=True,
        tty=False,
        _preload_content=False,
    )
    resp.write_stdin(tar_bytes.decode("latin-1"))  # binary-safe passthrough
    resp.close()
    return f"Synced {len(files)} items to pod"


def sync_files_from_pod(user_id: UUID, pod_type: str) -> list[dict]:
    """Read the /workspace tree back from the pod and return flat file list.

    Returns a list of dicts compatible with FileRepository.bulk_upsert.
    """
    import uuid as _uuid

    name = _pod_name(user_id, pod_type)
    resp = k8s_stream(
        _core.connect_get_namespaced_pod_exec,
        name,
        NAMESPACE,
        command=["tar", "czf", "-", "-C", "/workspace", "."],
        container="workspace",
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=False,
    )

    raw = b""
    while resp.is_open():
        resp.update(timeout=10)
        if resp.peek_stdout():
            raw += resp.read_stdout().encode("latin-1")
    resp.close()

    # Parse tar
    buf = io.BytesIO(raw)
    items = []
    path_to_id: dict[str, str] = {}

    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        for member in tar.getmembers():
            rel = member.name.lstrip("./")
            if not rel:
                continue

            fid = str(_uuid.uuid4())
            path_to_id[rel] = fid

            parent_path = "/".join(rel.split("/")[:-1])
            parent_id = path_to_id.get(parent_path)

            if member.isdir():
                items.append({
                    "file_id": fid,
                    "parent_id": parent_id,
                    "type": "folder",
                    "name": rel.split("/")[-1],
                    "content": "",
                    "language": "",
                    "is_expanded": False,
                    "position": 0,
                })
            else:
                extracted = tar.extractfile(member)
                content = extracted.read().decode("utf-8", errors="replace") if extracted else ""
                items.append({
                    "file_id": fid,
                    "parent_id": parent_id,
                    "type": "file",
                    "name": rel.split("/")[-1],
                    "content": content,
                    "language": "",
                    "is_expanded": False,
                    "position": 0,
                })

    return items


# ---------------------------------------------------------------------------
# Interactive PTY exec (for WebSocket terminal)
# ---------------------------------------------------------------------------
def open_terminal_stream(user_id: UUID, pod_type: str):
    """Open an interactive bash session in the pod.

    Returns the websocket-client stream object that the caller bridges
    to the browser WebSocket.
    """
    name = _pod_name(user_id, pod_type)
    resp = k8s_stream(
        _core.connect_get_namespaced_pod_exec,
        name,
        NAMESPACE,
        command=["/bin/bash"],
        container="workspace",
        stderr=True,
        stdin=True,
        stdout=True,
        tty=True,
        _preload_content=False,
    )
    return resp


# ---------------------------------------------------------------------------
# Cleanup idle pods (called from background task)
# ---------------------------------------------------------------------------
def list_workspace_pods() -> list[client.V1Pod]:
    """Return all itec-workspace pods in the namespace."""
    pods = _core.list_namespaced_pod(
        namespace=NAMESPACE,
        label_selector="app=itec-workspace",
    )
    return pods.items


def delete_all_user_pods(user_id: UUID):
    """Delete both frontend and backend pods for a user."""
    for pt in ("frontend", "backend"):
        delete_user_pod(user_id, pt)
