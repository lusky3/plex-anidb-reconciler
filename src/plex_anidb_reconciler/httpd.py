"""HTTP plumbing shared by the /trigger API and the web UI: a quiet handler with a whole-request deadline, and a
threading server that caps concurrent connections.

http.server alone gives each connection its own thread with no limit, and its socket timeout applies to each read,
so a client sending one header line every few seconds would keep its thread forever, before any auth check runs.
"""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_CONNECTIONS = 32  # further connections are closed at once until a slot frees up
READ_TIMEOUT_S = 10  # longest wait for any single read
REQUEST_DEADLINE_S = 30  # longest time to receive one whole request (request line, headers, body)


class _DeadlineReader:
    """Wraps a handler's rfile so that all reads together end by ``deadline``: each read gets the socket timeout
    that remains, and none starts once the deadline has passed."""

    def __init__(self, raw, conn: socket.socket, deadline: float) -> None:
        self._raw = raw
        self._conn = conn
        self._deadline = deadline

    def _arm(self) -> None:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request deadline passed")
        self._conn.settimeout(min(READ_TIMEOUT_S, remaining))

    def readline(self, *args):
        self._arm()
        return self._raw.readline(*args)

    def read(self, *args):
        self._arm()
        return self._raw.read(*args)

    def close(self) -> None:
        self._raw.close()


class QuietHandler(BaseHTTPRequestHandler):
    timeout = READ_TIMEOUT_S

    def version_string(self) -> str:
        return "pxar"  # no Python version in the Server header

    def setup(self) -> None:
        super().setup()
        self.rfile = _DeadlineReader(self.rfile, self.connection, time.monotonic() + REQUEST_DEADLINE_S)

    def parse_request(self) -> bool:
        # Headers are in: the response gets the normal per-operation timeout, not what's left of the deadline.
        # (A body read still goes through the deadline reader; a missed deadline raises TimeoutError, which
        # BaseHTTPRequestHandler.handle_one_request turns into a quiet close.)
        ok = super().parse_request()
        self.connection.settimeout(READ_TIMEOUT_S)
        return ok

    def log_message(self, format: str, *args) -> None:
        # Intentionally empty: no per-request access log (one line per /healthz probe, and query strings).
        pass


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, max_connections: int = MAX_CONNECTIONS) -> None:
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class BoundedServerV6(BoundedServer):
    address_family = socket.AF_INET6


def bind(host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> BoundedServer:
    """A BoundedServer on host:port; an IPv6 literal (``LISTEN=[addr]:port``) gets an IPv6 socket."""
    server_cls = BoundedServerV6 if ":" in host else BoundedServer
    return server_cls((host, port), handler)
