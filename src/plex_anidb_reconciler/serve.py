"""Daemon mode: periodic reconcile plus an on-demand /trigger endpoint (Tautulli).

/trigger has no authentication: bind it to the Docker network only. The daemon
should be the only process that applies; a concurrent CLI `pxar apply` in another
process is not coordinated with this lock.
"""

import ipaddress
import json
import logging
import signal
import socket
import threading
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from plex_anidb_reconciler.config import Settings, parse_listen
from plex_anidb_reconciler.runner import run_once

log = logging.getLogger(__name__)
MAX_BODY = 1024 * 1024  # larger bodies get 413 and the connection is closed
CHUNK = 64 * 1024
DEFAULT_LISTEN = "0.0.0.0:8787"


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
        self._state: dict = {"last_run_id": None, "last_status": None, "last_finished": None, "snapshot": None}
        self._state_lock = threading.Lock()
        self._heartbeat: datetime | None = None  # last loop iteration or finished run
        self._loop_thread: threading.Thread | None = None
        self._run_started: datetime | None = None  # set while a run is active
        self._stop: threading.Event | None = None  # shutdown signal, handed to every run

    def _beat(self) -> None:
        with self._state_lock:
            self._heartbeat = self._clock()

    def healthy(self) -> bool:
        """The loop thread is alive and either a run has been active for at most RUN_STALE_MINUTES (a long
        run is not unhealthy, a hung one is) or, when idle, it has beaten within 2 x POLL_MINUTES + 5 min."""
        with self._state_lock:
            return self._healthy_locked()

    def _healthy_locked(self) -> bool:
        """healthy() for a caller already holding _state_lock."""
        beat, thread, started = self._heartbeat, self._loop_thread, self._run_started
        if thread is None or not thread.is_alive() or beat is None:
            return False
        if started is not None:  # a reconcile is running; the loop beats again when it finishes
            return self._clock() - started <= timedelta(minutes=self.settings.run_stale_minutes)
        limit = timedelta(minutes=2 * self.settings.poll_minutes + 5)
        return self._clock() - beat <= limit

    def request_run(self) -> None:
        self._event.set()

    def health(self) -> dict:
        with self._state_lock:  # one acquisition: loop, run_active_since and the last-run fields agree
            out = {**self._state, "loop": "ok" if self._healthy_locked() else "stale"}
            if self._run_started is not None:
                out["run_active_since"] = self._run_started.astimezone(UTC).isoformat()
        return out

    def run_pending_now(self) -> None:
        """Run one reconcile (blocking if one is in flight). Never raises."""
        with self._lock:
            with self._state_lock:
                self._run_started = self._clock()
            run_id = None
            snapshot = None
            try:
                result = self._run_fn(self.settings, apply=True, limit=None, now=self._clock(), stop=self._stop)
                run_id = result.run_id
                snapshot = getattr(result, "snapshot", None)
                status = "failed" if result.counts.get("failed") else "ok"
            except Exception as exc:  # noqa: BLE001 - daemon must survive any run failure
                status = f"error:{type(exc).__name__}"  # type only: messages may contain URLs
                log.error("reconcile run failed: %s", type(exc).__name__)
            with self._state_lock:
                if run_id is not None:
                    self._state["last_run_id"] = run_id
                    self._state["snapshot"] = snapshot  # ok | stale | unavailable
                self._state["last_status"] = status
                self._state["last_finished"] = self._clock().isoformat()
                self._heartbeat = self._clock()
                self._run_started = None

    def loop(self, stop: threading.Event) -> None:
        interval = max(self.settings.poll_minutes, 1) * 60
        with self._state_lock:
            self._loop_thread = threading.current_thread()
            self._stop = stop
        while not stop.is_set():
            self._beat()
            self._event.wait(timeout=interval)  # a trigger wakes this immediately
            if stop.is_set():
                break
            self._event.clear()  # triggers arriving during the run set it again
            self.run_pending_now()


def make_server(service: Service, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        timeout = 10  # a stalled client cannot pin a handler thread

        def _json(self, code: int, body: dict, close: bool = False) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                body = service.health()
                self._json(200 if body["loop"] == "ok" else 503, body)
            else:
                self._json(404, {"error": "not found"})

        def _drain_body(self) -> bool:
            """Read and discard the whole request body (Tautulli payload). False: response already sent."""
            raw = self.headers.get("Content-Length")
            if raw is None:
                return True
            raw = raw.strip()
            if not raw.isdigit():  # also rejects negative and empty values
                self._json(400, {"error": "bad Content-Length"}, close=True)
                return False
            remaining = int(raw)
            if remaining > MAX_BODY:
                self._json(413, {"error": "body too large"}, close=True)
                return False
            while remaining > 0:
                chunk = self.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    self.close_connection = True
                    return False
                remaining -= len(chunk)
            return True

        def do_POST(self) -> None:
            if not self._drain_body():
                return
            if self.path == "/trigger":
                service.request_run()
                self._json(202, {"status": "accepted"})
            else:
                self._json(404, {"error": "not found"})

        def log_message(self, format: str, *args) -> None:
            pass

    server_cls = ThreadingHTTPServer
    if ":" in host:  # IPv6 literal (LISTEN=[addr]:port)

        class V6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server_cls = V6Server
    return server_cls((host, port), Handler)


def health_url(listen: str | None) -> str:
    """Where the container probe should connect, derived from LISTEN (default 0.0.0.0:8787)."""
    host, port = parse_listen(listen if listen and listen.strip() else DEFAULT_LISTEN)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:  # a hostname: connect to it as given
        pass
    else:
        mapped = getattr(ip, "ipv4_mapped", None)
        if ip.is_unspecified or (mapped is not None and mapped.is_unspecified):  # any form of 0.0.0.0 / ::
            host = "::1" if ip.version == 6 else "127.0.0.1"
        else:
            host = str(ip)
        if ip.version == 6:
            host = f"[{host.replace('%', '%25')}]"  # a zone id must be percent-encoded in a URL
    if host == "":
        host = "127.0.0.1"
    return f"http://{host}:{port}/healthz"


def healthcheck(env: Mapping[str, str]) -> int:
    """Docker HEALTHCHECK: 0 iff /healthz answers 200. Needs no backend settings; never raises."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # a loopback probe never uses a proxy
        with opener.open(health_url(env.get("LISTEN")), timeout=5) as resp:
            return 0 if resp.status == 200 else 1
    except Exception:  # noqa: BLE001 - any failure (bad LISTEN, refused, 503, timeout) means unhealthy
        return 1


def finish_shutdown(loop: threading.Thread, grace_s: float) -> bool:
    """Give an in-flight run up to grace_s to finish. False (and a warning) if it is still running."""
    loop.join(timeout=grace_s)
    if loop.is_alive():
        log.warning("reconcile run still running after %ss shutdown grace; exiting anyway", grace_s)
        return False
    return True


def serve(settings: Settings) -> None:
    host, port = parse_listen(settings.listen)
    service = Service(settings)
    server = make_server(service, host, port)
    stop = threading.Event()

    def shutdown(signum, frame) -> None:
        stop.set()  # an in-flight run also sees this and stops before its next write
        service.request_run()  # wake the loop; it exits after an in-flight run completes
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    service.request_run()  # initial run at startup
    loop = threading.Thread(target=service.loop, args=(stop,), daemon=True)
    loop.start()
    log.info("serving on %s", settings.listen)
    server.serve_forever()
    finish_shutdown(loop, settings.shutdown_grace_s)
    server.server_close()
