import socket
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from plex_anidb_reconciler import httpd
from plex_anidb_reconciler.httpd import BoundedServer, QuietHandler, bind

WAIT = 5


class Blocking(QuietHandler):
    entered = threading.Event()
    release = threading.Event()

    def do_GET(self):
        Blocking.entered.set()
        Blocking.release.wait(WAIT)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def stop(server):
    server.shutdown()
    server.server_close()


def recv_all(sock):
    chunks = []
    while chunk := sock.recv(4096):
        chunks.append(chunk)
    return b"".join(chunks)


def test_connections_beyond_the_cap_are_closed_until_a_slot_frees():
    Blocking.entered.clear()
    Blocking.release.clear()
    server = BoundedServer(("127.0.0.1", 0), Blocking, max_connections=1)
    port = serve(server)
    try:
        first = socket.create_connection(("127.0.0.1", port), timeout=WAIT)
        first.sendall(b"GET / HTTP/1.0\r\n\r\n")
        assert Blocking.entered.wait(WAIT)
        with socket.create_connection(("127.0.0.1", port), timeout=WAIT) as second:
            second.sendall(b"GET / HTTP/1.0\r\n\r\n")
            assert recv_all(second) == b""  # refused: closed without a reply
        Blocking.release.set()
        with first:
            assert recv_all(first).endswith(b"ok")
        with socket.create_connection(("127.0.0.1", port), timeout=WAIT) as third:  # the slot is free again
            third.sendall(b"GET / HTTP/1.0\r\n\r\n")
            assert recv_all(third).endswith(b"ok")
    finally:
        Blocking.release.set()
        stop(server)


def test_a_client_dribbling_headers_is_cut_off_at_the_deadline(monkeypatch):
    monkeypatch.setattr(httpd, "REQUEST_DEADLINE_S", 0.6)
    monkeypatch.setattr(httpd, "READ_TIMEOUT_S", 5)
    server = bind("127.0.0.1", 0, Blocking)
    port = serve(server)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=WAIT) as sock:
            sock.sendall(b"GET / HTTP/1.0\r\n")
            started = time.monotonic()
            closed = False
            for i in range(40):  # one header line every 0.1 s: each read is quick, the whole request isn't
                try:
                    sock.sendall(f"X-Pad-{i}: x\r\n".encode())
                except OSError:
                    closed = True
                    break
                sock.settimeout(0.1)
                try:
                    if sock.recv(1) == b"":
                        closed = True
                        break
                except TimeoutError:
                    pass
            assert closed and time.monotonic() - started < 3
    finally:
        stop(server)


def test_a_failed_dispatch_gives_the_slot_back(monkeypatch):
    server = BoundedServer(("127.0.0.1", 0), QuietHandler, max_connections=1)
    try:

        def boom(self, request, client_address):
            raise RuntimeError("no thread")

        monkeypatch.setattr(ThreadingHTTPServer, "process_request", boom)
        a, b = socket.socketpair()
        with a, b, pytest.raises(RuntimeError):
            server.process_request(a, ("x", 0))
        assert server._slots.acquire(blocking=False)
    finally:
        server.server_close()


def test_bind_ipv6_literal():
    try:
        server = bind("::1", 0, QuietHandler)
    except OSError:
        pytest.skip("no IPv6 loopback")
    try:
        assert server.address_family == socket.AF_INET6
    finally:
        server.server_close()


def test_the_deadline_reader_refuses_reads_after_the_deadline():
    a, b = socket.socketpair()
    with a, b:
        b.sendall(b"line\n")
        reader = httpd._DeadlineReader(a.makefile("rb"), a, time.monotonic() + 5)
        assert reader.readline() == b"line\n"
        assert a.gettimeout() <= httpd.READ_TIMEOUT_S
        late = httpd._DeadlineReader(a.makefile("rb"), a, time.monotonic() - 1)
        with pytest.raises(TimeoutError):
            late.read(1)
        reader.close()
        late.close()
