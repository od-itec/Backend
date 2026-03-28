"""WebSocket endpoint that bridges browser ↔ K8s exec PTY for a real terminal."""

import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt

from auth_config import SECRET
from k8s_service import open_terminal_stream, get_user_pod

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])

# JWT algorithm must match fastapi-users config
JWT_ALGORITHM = "HS256"


def _authenticate_ws(token: str):
    """Validate JWT and return the user id (UUID string).

    Raises ValueError on failure.
    """
    try:
        payload = jwt.decode(token, SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as exc:
        raise ValueError("Invalid token") from exc

    # fastapi-users stores user id under "sub"
    uid = payload.get("sub")
    if not uid:
        raise ValueError("Token missing subject")
    return uid


@router.websocket("/ws/terminal")
async def terminal_ws(
    ws: WebSocket,
    token: str = Query(...),
    pod_type: str = Query("frontend"),
):
    """Full interactive terminal over WebSocket.

    Query params:
        token    – JWT bearer token (WebSocket can't use Authorization header easily)
        pod_type – "frontend" or "backend"
    """
    # Authenticate before accepting
    try:
        user_id = _authenticate_ws(token)
    except ValueError:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()

    # Verify pod is running
    from uuid import UUID

    uid = UUID(user_id)
    pod = get_user_pod(uid, pod_type)
    if not pod or pod.status.phase != "Running":
        await ws.send_text("\r\n\x1b[31mPod is not running. Start workspace first.\x1b[0m\r\n")
        await ws.close(code=4002, reason="Pod not running")
        return

    # Open interactive PTY stream to the K8s pod
    k8s_ws = open_terminal_stream(uid, pod_type)

    # Two async tasks: pod→browser and browser→pod
    stop = asyncio.Event()

    async def _pod_to_browser():
        """Read from K8s exec stream and forward to the browser."""
        loop = asyncio.get_event_loop()
        try:
            while not stop.is_set():
                # k8s_stream .read_stdout() is blocking — run in thread
                data = await loop.run_in_executor(None, _read_k8s, k8s_ws)
                if data:
                    await ws.send_text(data)
                else:
                    await asyncio.sleep(0.05)
        except Exception:
            pass
        finally:
            stop.set()

    async def _browser_to_pod():
        """Read from browser WebSocket and forward to the K8s exec stream."""
        loop = asyncio.get_event_loop()
        try:
            while not stop.is_set():
                data = await ws.receive_text()
                # Check for resize messages (JSON with type "resize")
                if data.startswith("{"):
                    import json

                    try:
                        msg = json.loads(data)
                        if msg.get("type") == "resize":
                            # K8s exec resize: channel 4 with JSON {"Width": w, "Height": h}
                            resize_payload = json.dumps(
                                {"Width": msg["cols"], "Height": msg["rows"]}
                            )
                            await loop.run_in_executor(
                                None,
                                lambda: k8s_ws.write_channel(4, resize_payload),
                            )
                            continue
                    except (json.JSONDecodeError, KeyError):
                        pass

                await loop.run_in_executor(
                    None, lambda d=data: k8s_ws.write_stdin(d)
                )
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            stop.set()

    try:
        await asyncio.gather(_pod_to_browser(), _browser_to_pod())
    finally:
        try:
            k8s_ws.close()
        except Exception:
            pass


def _read_k8s(k8s_ws) -> str:
    """Blocking helper: read available stdout from the K8s exec stream."""
    k8s_ws.update(timeout=1)
    out = ""
    if k8s_ws.peek_stdout():
        out += k8s_ws.read_stdout()
    if k8s_ws.peek_stderr():
        out += k8s_ws.read_stderr()
    return out
