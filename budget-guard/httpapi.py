"""Stdlib HTTP server: /metrics /healthz /readyz /status."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

logger = logging.getLogger("budget-guard")


@dataclass
class RuntimeStatus:
    """Mutable snapshot served at /status and used by readiness."""

    ready: bool = False
    leader: bool = False
    day_key: str = ""
    last_poll_ok: bool = True
    last_error: str | None = None
    watermark_ms: int | None = None
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set_ready(self, ready: bool) -> None:
        with self.lock:
            self.ready = ready

    def set_leader(self, leader: bool) -> None:
        with self.lock:
            self.leader = leader

    def update_poll(
        self,
        *,
        day_key: str,
        last_poll_ok: bool,
        last_error: str | None,
        watermark_ms: int,
        projects: dict[str, dict[str, Any]],
    ) -> None:
        with self.lock:
            self.day_key = day_key
            self.last_poll_ok = last_poll_ok
            self.last_error = last_error
            self.watermark_ms = watermark_ms
            self.projects = projects

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            lag = None
            if self.watermark_ms is not None:
                lag = max(0.0, time.time() - self.watermark_ms / 1000.0)
            return {
                "leader": self.leader,
                "ready": self.ready,
                "day_key": self.day_key,
                "last_poll_ok": self.last_poll_ok,
                "last_error": self.last_error,
                "watermark_ms": self.watermark_ms,
                "watermark_lag_seconds": lag,
                "projects": dict(self.projects),
            }


def project_status_map(
    projects_cfg: dict[str, Any],
    spend: dict[str, float],
    blocked_projects: set[str],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(projects_cfg.keys()):
        pcfg = projects_cfg.get(name) or {}
        if not isinstance(pcfg, dict):
            pcfg = {}
        budget = float(pcfg.get("budget_usd") or 0)
        used = float(spend.get(name, 0.0))
        ratio = (used / budget) if budget > 0 else 0.0
        out[name] = {
            "spend_usd": round(used, 6),
            "budget_usd": budget,
            "ratio": ratio,
            "blocked": name in blocked_projects,
            "enforce": bool(pcfg.get("enforce", True)),
        }
    return out


def make_handler(status: RuntimeStatus):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("http " + fmt, *args)

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/metrics":
                payload = generate_latest()
                self._send(200, payload, CONTENT_TYPE_LATEST)
                return
            if path == "/healthz":
                self._send(200, b"ok\n", "text/plain; charset=utf-8")
                return
            if path == "/readyz":
                ready = status.snapshot()["ready"]
                if ready:
                    self._send(200, b"ready\n", "text/plain; charset=utf-8")
                else:
                    self._send(503, b"not ready\n", "text/plain; charset=utf-8")
                return
            if path == "/status":
                body = json.dumps(status.snapshot(), indent=2).encode("utf-8")
                self._send(200, body, "application/json")
                return
            self._send(404, b"not found\n", "text/plain; charset=utf-8")

    return Handler


def start_http_server(port: int, status: RuntimeStatus) -> ThreadingHTTPServer:
    """Bind 0.0.0.0:port in a daemon thread. Port 0 picks an ephemeral port."""
    handler = make_handler(status)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="http")
    thread.start()
    bound = httpd.server_address[1]
    logger.info("HTTP server listening on 0.0.0.0:%s", bound)
    return httpd
