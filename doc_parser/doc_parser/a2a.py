"""A2A (Agent2Agent) 1.0 endpoint over JSON-RPC — no domain code here.

Kept free of doc_parser specifics so it can move to a shared package when a second project
needs it (side_projects/reference/AGENT_PRODUCT.md). A project supplies an Agent Card and a
``TaskHandler``; this module does discovery, JSON-RPC framing and error codes.

Spec checked 2026-11-15 (a2aproject/A2A docs/specification.md, latest release 1.0.0): methods
SendMessage / GetTask / ListTasks / CancelTask, ``TASK_STATE_*`` values, parts as
{"text"} / {"raw", "mediaType", "filename"} / {"url", ...} / {"data"}, error codes -32001..-32007.
Streaming and push notifications are not offered (card says so; the methods answer -32004).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

TERMINAL = ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED")


class A2AError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def task_not_found(tid: str) -> A2AError:
    return A2AError(-32001, "Task not found", {"taskId": tid})


def not_cancelable(tid: str, state: str) -> A2AError:
    return A2AError(-32002, "Task cannot be canceled", {"taskId": tid, "state": state})


def unsupported(what: str) -> A2AError:
    return A2AError(-32004, f"Unsupported operation: {what}")


def content_type_not_supported(msg: str) -> A2AError:
    return A2AError(-32005, msg)


def invalid_params(msg: str) -> A2AError:
    return A2AError(-32602, msg)


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def task(tid: str, context_id: str, state: str, *, text: Optional[str] = None, artifacts: Optional[list] = None,
         timestamp: Optional[str] = None, metadata: Optional[dict] = None) -> dict:
    """Build a Task object. ``text`` becomes the status message (an agent message)."""
    status: dict = {"state": state, "timestamp": timestamp or now()}
    if text:
        status["message"] = {"role": "ROLE_AGENT", "messageId": f"{tid}-{state.lower()}", "parts": [{"text": text}]}
    out: dict = {"id": tid, "contextId": context_id, "status": status}
    if artifacts:
        out["artifacts"] = artifacts
    if metadata:
        out["metadata"] = metadata
    return out


class TaskHandler(Protocol):
    """``base`` is the server's own URL (for links in results)."""
    def send(self, message: dict, configuration: dict, base: str) -> dict: ...      # -> Task
    def get(self, task_id: str, base: str) -> dict: ...                             # -> Task
    def list(self, params: dict, base: str) -> list[dict]: ...                      # -> Tasks, newest first
    def cancel(self, task_id: str, base: str) -> dict: ...                          # -> Task


def mount_a2a(app: FastAPI, card: dict, handler: TaskHandler, path: str = "/a2a") -> None:
    """GET /.well-known/agent-card.json and POST ``path`` (JSON-RPC 2.0)."""

    def base_of(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    def agent_card(request: Request) -> dict:
        c = json.loads(json.dumps(card))
        c["supportedInterfaces"] = [{"url": base_of(request) + path, "protocolBinding": "JSONRPC",
                                     "protocolVersion": "1.0"}]
        c.setdefault("capabilities", {}).update({"streaming": False, "pushNotifications": False})
        return c

    def ok(rid: Any, result: Any) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})

    def err(rid: Any, code: int, message: str, data: Any = None) -> JSONResponse:
        e: dict = {"code": code, "message": message}
        if data is not None:
            e["data"] = data
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": e})

    @app.post(path, include_in_schema=False)
    async def rpc(request: Request) -> JSONResponse:
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return err(None, -32700, "Parse error")
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
            return err(body.get("id") if isinstance(body, dict) else None, -32600, "Invalid Request")
        rid, method, params = body.get("id"), body["method"], body.get("params") or {}
        if not isinstance(params, dict):
            return err(rid, -32602, "params must be an object")
        base = base_of(request)
        try:
            if method == "SendMessage":
                msg = params.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("parts"), list):
                    raise invalid_params("message.parts is required")
                result = {"task": await _thread(handler.send, msg, params.get("configuration") or {}, base)}
            elif method == "GetTask":
                result = handler.get(_id(params), base)
            elif method == "CancelTask":
                result = handler.cancel(_id(params), base)
            elif method == "ListTasks":
                tasks = handler.list(params, base)
                size = int(params.get("pageSize") or 50)
                result = {"tasks": tasks[:size], "nextPageToken": "", "pageSize": size, "totalSize": len(tasks)}
            elif method in ("SendStreamingMessage", "SubscribeToTask"):
                raise unsupported("streaming")
            elif "PushNotification" in method:
                raise A2AError(-32003, "Push notifications are not supported")
            else:
                return err(rid, -32601, f"Method not found: {method}")
        except A2AError as e:
            return err(rid, e.code, e.message, e.data)
        except Exception as e:  # handler bug: report it as an internal error, keep serving
            return err(rid, -32603, f"Internal error: {type(e).__name__}")
        return ok(rid, result)


def _id(params: dict) -> str:
    tid = params.get("id")
    if not isinstance(tid, str) or not tid:
        raise invalid_params("id is required")
    return tid


async def _thread(fn, *args):
    import anyio

    return await anyio.to_thread.run_sync(lambda: fn(*args))
