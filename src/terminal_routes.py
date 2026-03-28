"""
WebSocket terminal endpoint.

Provides a real interactive shell inside the user's DinD container
by bridging xterm.js on the frontend to `docker exec -it … /bin/sh`
on the backend via WebSocket.
"""

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from fastapi_users.jwt import decode_jwt
from jose import JWTError

from auth_config import SECRET
from dind_manager import dind_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])


async def _authenticate_ws(token: str) -> str | None:
    """Validate a JWT token and return the user id (as string), or None."""
    try:
        payload = decode_jwt(token, SECRET, audience=["fastapi-users:auth"])
        user_id = payload.get("sub")
        return user_id
    except Exception:
        return None


@router.websocket("/ws/terminal/{session_id}")
async def terminal_ws(
    websocket: WebSocket,
    session_id: str,
    token: str = Query(...),
):
    """Full-duplex terminal over WebSocket.

    The frontend sends raw keystrokes; the backend forwards them to the
    shell running inside the user's DinD container and streams output back.
    """
    user_id = await _authenticate_ws(token)
    if user_id is None:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    await websocket.accept()

    try:
        # Ensure DinD is running
        session = await dind_manager.ensure_dind(user_id)

        # Create an interactive exec (PTY shell)
        exec_id, raw_sock = await dind_manager.exec_attach(user_id)

        # The Docker SDK returns a socket-like object — get the raw fd
        sock = raw_sock._sock  # underlying socket

        loop = asyncio.get_event_loop()

        # ----------------------------------------------------------
        # Task 1: read from Docker socket → send to WebSocket
        # ----------------------------------------------------------
        async def docker_to_ws():
            try:
                while True:
                    data = await loop.run_in_executor(None, sock.recv, 4096)
                    if not data:
                        break
                    await websocket.send_bytes(data)
            except (OSError, WebSocketDisconnect):
                pass

        # ----------------------------------------------------------
        # Task 2: read from WebSocket → write to Docker socket
        # ----------------------------------------------------------
        async def ws_to_docker():
            try:
                while True:
                    message = await websocket.receive()
                    if "bytes" in message:
                        payload = message["bytes"]
                    elif "text" in message:
                        payload = message["text"].encode("utf-8")
                    else:
                        break
                    await loop.run_in_executor(None, sock.sendall, payload)
                    session.touch()
            except (WebSocketDisconnect, RuntimeError):
                pass

        # Run both in parallel; cancel the other when one finishes
        t1 = asyncio.create_task(docker_to_ws())
        t2 = asyncio.create_task(ws_to_docker())

        done, pending = await asyncio.wait(
            [t1, t2], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

    except WebSocketDisconnect:
        logger.debug("Terminal WebSocket disconnected for user %s", user_id)
    except Exception as exc:
        logger.exception("Terminal error for user %s: %s", user_id, exc)
        try:
            await websocket.close(code=1011, reason="Internal error")
        except Exception:
            pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
