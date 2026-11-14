"""Daemon mode: periodic reconcile plus an on-demand /trigger endpoint (Tautulli).

/trigger has no authentication: bind it to the Docker network only. The daemon
should be the only process that applies; a concurrent CLI `pxar apply` in another
process is not coordinated with this lock.
"""

import json
import logging
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from plex_anidb_reconciler.config import Settings
from plex_anidb_reconciler.runner import run_once

log = logging.getLogger(__name__)
MAX_BODY = 64 * 1024


class Service:
    def __init__(
        self,
        settings: Settings,
        run_fn: Callable = run_once,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self._run_fn = run_fn
        self._clock = clock
        self._lock = threading.Lock()  # the only guard around run_fn
        self._event = threading.Event()  # pending-run flag; coalesces triggers
        self._state: dict = {"last_run_id": None, "last_status": None, "last_finished": None}
        self._state_lock = threading.Lock()

    def request_run(self) -> None:
        self._event.set()

    def health(self) -> dict:
        with self._state_lock:
            return dict(self._state)

    def run_pending_now(self) -> None:
        """Run one reconcile (blocking if one is in flight). Never raises."""
        with self._lock:
            run_id = None
            try:
                result = self._run_fn(self.settings, apply=True, limit=None, now=self._clock())
                run_id = result.run_id
                status = "failed" if result.counts.get("failed") else "ok"
            except Exception as exc:  # noqa: BLE001 - daemon must survive any run failure
                status = f"error:{type(exc).__name__}"  # type only: messages may contain URLs
                log.error("reconcile run failed: %s", type(exc).__name__)
            with self._state_lock:
                if run_id is not None:
                    self._state["last_run_id"] = run_id
                self._state["last_status"] = status
                self._state["last_finished"] = self._clock().isoformat()

    def loop(self, stop: threading.Event) -> None:
        interval = max(self.settings.poll_minutes, 1) * 60
        while not stop.is_set():
            self._event.wait(timeout=interval)  # a trigger wakes this immediately
            if stop.is_set():
                break
            self._event.clear()  # triggers arriving during the run set it again
            self.run_pending_now()


def make_server(service: Service, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._json(200, service.health())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            try:
                length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
            except ValueError:
                length = 0
            if length > 0:
                self.rfile.read(length)  # discard Tautulli payload
            if self.path == "/trigger":
                service.request_run()
                self._json(202, {"status": "accepted"})
            else:
                self._json(404, {"error": "not found"})

        def log_message(self, format: str, *args) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve(settings: Settings) -> None:
    host, _, port = settings.listen.rpartition(":")
    service = Service(settings)
    server = make_server(service, host or "0.0.0.0", int(port))
    stop = threading.Event()

    def shutdown(signum, frame) -> None:
        stop.set()
        service.request_run()  # wake the loop
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    service.request_run()  # initial run at startup
    loop = threading.Thread(target=service.loop, args=(stop,), daemon=True)
    loop.start()
    log.info("serving on %s", settings.listen)
    server.serve_forever()
    loop.join(timeout=10)
    server.server_close()
