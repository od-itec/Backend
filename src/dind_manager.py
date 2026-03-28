"""
Docker-in-Docker container lifecycle manager.

Each user gets one privileged DinD container with their workspace
files mounted at /workspace.  The backend talks to the host Docker
daemon (via the mounted socket) and creates / destroys DinD
containers on behalf of users.
"""

import asyncio
import logging
import os
import tarfile
import io
import time
from typing import Dict, Optional
from uuid import UUID

import docker
from docker.errors import NotFound, APIError

logger = logging.getLogger(__name__)

DIND_IMAGE = os.getenv("DIND_IMAGE", "itec-dind:latest")
DIND_FALLBACK_IMAGE = "docker:dind"
CONTAINER_PREFIX = "dind-"
IDLE_TIMEOUT_SECONDS = int(os.getenv("DIND_IDLE_TIMEOUT", "1800"))  # 30 min
USER_CPU_LIMIT = float(os.getenv("DIND_CPU_LIMIT", "1.0"))  # cores
USER_MEM_LIMIT = os.getenv("DIND_MEM_LIMIT", "1g")
MAX_APP_CONTAINERS = 2  # frontend + backend per user

# Port range allocation: each user gets a small range
PORT_RANGE_SIZE = 10
PORT_RANGE_START = int(os.getenv("DIND_PORT_RANGE_START", "11000"))


class DinDSession:
    """Tracks a single user's DinD container."""

    def __init__(self, user_id: str, container_id: str, port_base: int):
        self.user_id = user_id
        self.container_id = container_id
        self.port_base = port_base
        self.last_activity = time.time()
        self.app_containers: Dict[str, dict] = {}  # name -> {image, port, status}

    def touch(self):
        self.last_activity = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_activity


class DinDManager:
    """Manages per-user DinD containers on the host Docker daemon."""

    def __init__(self):
        self._client: Optional[docker.DockerClient] = None
        self._sessions: Dict[str, DinDSession] = {}  # user_id -> session
        self._next_port_slot = 0
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _container_name(self, user_id: str) -> str:
        return f"{CONTAINER_PREFIX}{user_id}"

    def _allocate_port_base(self) -> int:
        base = PORT_RANGE_START + self._next_port_slot * PORT_RANGE_SIZE
        self._next_port_slot += 1
        return base

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    async def ensure_dind(self, user_id: str) -> DinDSession:
        """Ensure a DinD container is running for this user.  Create if needed."""
        async with self._lock:
            # Already tracked?
            if user_id in self._sessions:
                session = self._sessions[user_id]
                session.touch()
                # Make sure the container is actually running
                try:
                    container = self.client.containers.get(session.container_id)
                    if container.status != "running":
                        container.start()
                except NotFound:
                    # Container vanished — recreate
                    return await self._create_dind(user_id)
                return session

            # Check for an orphaned container from a previous session
            name = self._container_name(user_id)
            try:
                container = self.client.containers.get(name)
                if container.status != "running":
                    container.start()
                port_base = self._allocate_port_base()
                session = DinDSession(user_id, container.id, port_base)
                self._sessions[user_id] = session
                return session
            except NotFound:
                pass

            return await self._create_dind(user_id)

    async def _create_dind(self, user_id: str) -> DinDSession:
        name = self._container_name(user_id)

        # Remove stale container if it exists
        try:
            old = self.client.containers.get(name)
            old.remove(force=True)
        except NotFound:
            pass

        port_base = self._allocate_port_base()

        # Decide which image to use
        image = DIND_IMAGE
        try:
            self.client.images.get(image)
        except Exception:
            logger.info("Custom DinD image %s not found, falling back to %s", image, DIND_FALLBACK_IMAGE)
            image = DIND_FALLBACK_IMAGE
            try:
                self.client.images.get(image)
            except Exception:
                logger.info("Pulling %s …", image)
                self.client.images.pull(image)

        # Build port bindings: expose PORT_RANGE_SIZE ports
        port_bindings = {}
        exposed_ports = {}
        for i in range(PORT_RANGE_SIZE):
            guest_port = 8080 + i
            host_port = port_base + i
            port_bindings[f"{guest_port}/tcp"] = host_port
            exposed_ports[f"{guest_port}/tcp"] = {}

        container = self.client.containers.run(
            image,
            name=name,
            detach=True,
            privileged=True,
            environment={
                "DOCKER_TLS_CERTDIR": "",  # disable TLS inside DinD for simplicity
            },
            ports=port_bindings,
            nano_cpus=int(USER_CPU_LIMIT * 1e9),
            mem_limit=USER_MEM_LIMIT,
            restart_policy={"Name": "unless-stopped"},
            labels={
                "itec.user": user_id,
                "itec.managed": "true",
            },
        )

        # Wait for Docker daemon inside DinD to be ready
        await self._wait_for_docker(container)

        session = DinDSession(user_id, container.id, port_base)
        self._sessions[user_id] = session
        logger.info("Created DinD container %s for user %s (ports %d-%d)",
                     name, user_id, port_base, port_base + PORT_RANGE_SIZE - 1)
        return session

    async def _wait_for_docker(self, container, timeout: int = 60):
        """Poll until `docker info` succeeds inside the DinD container."""
        for _ in range(timeout):
            try:
                exit_code, _ = container.exec_run("docker info", demux=True)
                if exit_code == 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(1)
        logger.warning("Docker daemon inside %s did not become ready within %ds", container.name, timeout)

    async def stop_dind(self, user_id: str):
        """Stop and remove a user's DinD container."""
        async with self._lock:
            session = self._sessions.pop(user_id, None)
            if session is None:
                return
            try:
                container = self.client.containers.get(session.container_id)
                container.remove(force=True)
                logger.info("Removed DinD container for user %s", user_id)
            except NotFound:
                pass

    def get_session(self, user_id: str) -> Optional[DinDSession]:
        session = self._sessions.get(user_id)
        if session:
            session.touch()
        return session

    # ------------------------------------------------------------------
    # File sync
    # ------------------------------------------------------------------

    async def sync_files(self, user_id: str, files: list[dict]):
        """Write flattened file tree into /workspace inside the DinD container.

        Each file dict: {path: str, content: str, type: "file"|"folder"}
        """
        session = self.get_session(user_id)
        if session is None:
            raise RuntimeError("No DinD session for this user")

        container = self.client.containers.get(session.container_id)

        # Build a tar archive in memory
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for f in files:
                if f["type"] == "folder":
                    info = tarfile.TarInfo(name=f["path"])
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(info)
                else:
                    data = (f.get("content") or "").encode("utf-8")
                    info = tarfile.TarInfo(name=f["path"])
                    info.size = len(data)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))

        buf.seek(0)

        # Clean workspace and put files
        container.exec_run("rm -rf /workspace")
        container.exec_run("mkdir -p /workspace")
        container.put_archive("/workspace", buf)
        logger.info("Synced %d items to /workspace for user %s", len(files), user_id)

    # ------------------------------------------------------------------
    # Exec helper (used by terminal WebSocket)
    # ------------------------------------------------------------------

    async def exec_attach(self, user_id: str):
        """Create an interactive exec instance (shell) and return the low-level socket.

        Returns (exec_id, socket) — caller is responsible for
        reading/writing the raw socket.
        """
        session = self.get_session(user_id)
        if session is None:
            raise RuntimeError("No DinD session for this user")

        container = self.client.containers.get(session.container_id)

        exec_instance = self.client.api.exec_create(
            container.id,
            cmd="/bin/sh",
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
        )

        sock = self.client.api.exec_start(
            exec_instance["Id"],
            socket=True,
            tty=True,
        )

        return exec_instance["Id"], sock

    # ------------------------------------------------------------------
    # App container management (build / run / stop inside DinD)
    # ------------------------------------------------------------------

    async def run_in_dind(self, user_id: str, command: str) -> tuple[int, str]:
        """Run a one-shot command inside the DinD container, return (exit_code, output)."""
        session = self.get_session(user_id)
        if session is None:
            raise RuntimeError("No DinD session for this user")

        container = self.client.containers.get(session.container_id)
        exit_code, output = container.exec_run(command, demux=False)
        return exit_code, output.decode("utf-8", errors="replace") if output else ""

    async def build_with_pack(self, user_id: str, project_path: str, image_name: str):
        """Run `pack build` inside the DinD container.

        Returns an async generator that yields output lines.
        """
        session = self.get_session(user_id)
        if session is None:
            raise RuntimeError("No DinD session for this user")

        container = self.client.containers.get(session.container_id)

        # Ensure pack is installed (lazy install)
        exit_code, _ = container.exec_run("which pack", demux=True)
        if exit_code != 0:
            logger.info("Installing pack CLI inside DinD for user %s", user_id)
            install_cmd = (
                "sh -c '"
                "wget -qO /usr/local/bin/pack "
                "https://github.com/buildpacks/pack/releases/download/v0.35.1/pack-v0.35.1-linux.tgz "
                "&& tar xzf /usr/local/bin/pack -C /usr/local/bin/ "
                "&& chmod +x /usr/local/bin/pack"
                "' || "
                "sh -c '"
                "apk add --no-cache curl && "
                "curl -sSL https://github.com/buildpacks/pack/releases/download/v0.35.1/pack-v0.35.1-linux.tgz "
                "| tar xz -C /usr/local/bin/"
                "'"
            )
            container.exec_run(["sh", "-c", install_cmd])

        # Run pack build — stream output
        cmd = f"pack build {image_name} --builder gcr.io/buildpacks/builder:google-22 --path {project_path}"
        exec_instance = self.client.api.exec_create(
            container.id,
            cmd=["sh", "-c", cmd],
            stdout=True,
            stderr=True,
            tty=True,
        )

        output_stream = self.client.api.exec_start(exec_instance["Id"], stream=True, tty=True)
        return exec_instance["Id"], output_stream

    async def run_app_container(
        self, user_id: str, image_name: str, container_name: str, port: int, env: dict | None = None
    ) -> dict:
        """Run a built image inside the DinD container's Docker daemon."""
        session = self.get_session(user_id)
        if session is None:
            raise RuntimeError("No DinD session for this user")

        if len(session.app_containers) >= MAX_APP_CONTAINERS:
            raise RuntimeError(f"Maximum {MAX_APP_CONTAINERS} app containers allowed")

        env_args = ""
        if env:
            for k, v in env.items():
                env_args += f" -e {k}={v}"

        # Map to one of the DinD's exposed guest ports
        guest_port = 8080 + len(session.app_containers)
        host_port = session.port_base + len(session.app_containers)

        cmd = (
            f"docker run -d --name {container_name} "
            f"-p {guest_port}:{port}{env_args} {image_name}"
        )

        exit_code, output = await self.run_in_dind(user_id, cmd)
        if exit_code != 0:
            raise RuntimeError(f"Failed to start container: {output}")

        session.app_containers[container_name] = {
            "image": image_name,
            "guest_port": guest_port,
            "host_port": host_port,
            "app_port": port,
            "status": "running",
        }

        return {"host_port": host_port, "container_name": container_name}

    async def stop_app_container(self, user_id: str, container_name: str):
        """Stop and remove an app container inside DinD."""
        session = self.get_session(user_id)
        if session is None:
            raise RuntimeError("No DinD session for this user")

        await self.run_in_dind(user_id, f"docker rm -f {container_name}")
        session.app_containers.pop(container_name, None)

    async def get_app_status(self, user_id: str) -> list[dict]:
        """Return status of all app containers for a user."""
        session = self.get_session(user_id)
        if session is None:
            return []

        result = []
        for name, info in session.app_containers.items():
            exit_code, output = await self.run_in_dind(
                user_id, f"docker inspect -f '{{{{.State.Status}}}}' {name}"
            )
            status = output.strip() if exit_code == 0 else "unknown"
            result.append({
                "name": name,
                "image": info["image"],
                "host_port": info["host_port"],
                "status": status,
            })
        return result

    async def get_app_logs(self, user_id: str, container_name: str, tail: int = 200) -> str:
        """Get logs from an app container inside DinD."""
        exit_code, output = await self.run_in_dind(
            user_id, f"docker logs --tail {tail} {container_name}"
        )
        return output

    # ------------------------------------------------------------------
    # Idle cleanup
    # ------------------------------------------------------------------

    async def start_cleanup_loop(self):
        """Run a background loop that removes idle DinD containers."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            to_remove = []
            for user_id, session in list(self._sessions.items()):
                if session.idle_seconds > IDLE_TIMEOUT_SECONDS:
                    to_remove.append(user_id)
            for user_id in to_remove:
                logger.info("Idle timeout — removing DinD for user %s", user_id)
                await self.stop_dind(user_id)

    async def shutdown(self):
        """Clean up on application shutdown."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
        # Optionally stop all DinD containers
        for user_id in list(self._sessions.keys()):
            await self.stop_dind(user_id)
        if self._client:
            self._client.close()


# Singleton instance
dind_manager = DinDManager()
