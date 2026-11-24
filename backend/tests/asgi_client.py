"""轻量 ASGI 客户端：不依赖 httpx，直接驱动 FastAPI 应用，便于验证下载区间等 HTTP 行为。"""
from __future__ import annotations

import asyncio
import json
from typing import Any


class AsgiResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def request(
    app: Any,
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
    query: str = "",
) -> AsgiResponse:
    payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    raw_headers = [(b"content-type", b"application/json; charset=utf-8")]
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": raw_headers,
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }
    state = {"sent": False}
    collected = {"status": 0, "headers": {}, "body": b""}

    async def receive() -> dict[str, Any]:
        if state["sent"]:
            return {"type": "http.disconnect"}
        state["sent"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            collected["status"] = message["status"]
            collected["headers"] = {
                key.decode("latin-1"): value.decode("latin-1")
                for key, value in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            collected["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return AsgiResponse(collected["status"], collected["headers"], collected["body"])
