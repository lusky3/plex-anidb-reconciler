import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from plex_anidb_reconciler.serve import Service, make_server


@dataclass
class FakeResult:
    run_id: str
    counts: dict
    report_path: Path = Path("r.json")
    snapshot: str = "ok"


def make_service(run_fn, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)):
    settings = SimpleNamespace(
        poll_minutes=15,
        listen="127.0.0.1:0",
        shutdown_grace_s=25,
        shoko_api_key="SHOKOSECRET",
        plex_token="PLEXSECRET",
    )
    return Service(settings, run_fn=run_fn, clock=clock)


def start_http(svc):
    server = make_server(svc, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def post(url, body=b"x"):
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.status, json.loads(r.read())


def wait_for(cond, timeout=1.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_trigger_coalesces_concurrent_requests():
    calls = []
    gate = threading.Event()

    def run_fn(settings, **kw):
        calls.append(kw)
        gate.wait(30)  # released by the test; the bound only keeps a broken test from hanging forever
        return FakeResult(f"r{len(calls)}", {"moved": 1})

    svc = make_service(run_fn)
    server, base = start_http(svc)
    stop = threading.Event()
    t = threading.Thread(target=svc.loop, args=(stop,), daemon=True)
    try:
        svc.request_run()
        t.start()
        assert wait_for(lambda: len(calls) == 1)  # first run is in progress
        for _ in range(5):
            assert post(base + "/trigger")[0] == 202
        gate.set()
        assert wait_for(lambda: len(calls) == 2)
        time.sleep(0.2)
        assert len(calls) == 2  # 5 POSTs -> exactly 1 extra run
        assert calls[0]["apply"] is True
    finally:
        stop.set()
        svc.request_run()
        t.join(2)
        server.shutdown()
        server.server_close()


def start_loop(svc):
    stop = threading.Event()
    t = threading.Thread(target=svc.loop, args=(stop,), daemon=True)
    t.start()
    assert wait_for(lambda: svc.health()["loop"] == "ok")
    return stop, t


def stop_loop(svc, stop, t):
    stop.set()
    svc.request_run()
    t.join(2)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_healthz_reports_last_run():
    svc = make_service(lambda s, **kw: FakeResult("abc", {"moved": 2}))
    server, base = start_http(svc)
    stop, t = start_loop(svc)
    try:
        assert get(base + "/healthz") == (
            200,
            {"last_run_id": None, "last_status": None, "last_finished": None, "snapshot": None, "loop": "ok"},
        )
        svc.run_pending_now()
        assert get(base + "/healthz") == (
            200,
            {
                "last_run_id": "abc",
                "last_status": "ok",
                "last_finished": "2026-01-01T00:00:00+00:00",
                "snapshot": "ok",
                "loop": "ok",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/nope", timeout=2)
        assert ei.value.code == 404
    finally:
        stop_loop(svc, stop, t)
        server.shutdown()
        server.server_close()


def test_failed_counts_and_exception_survive():
    results = iter([FakeResult("a", {"failed": 1}), RuntimeError("http://secret")])

    def run_fn(settings, **kw):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    svc = make_service(run_fn)
    svc.run_pending_now()
    assert svc.health()["last_status"] == "failed"
    svc.run_pending_now()  # must not raise
    h = svc.health()
    assert h["last_status"] == "error:RuntimeError"
    assert "secret" not in json.dumps(h)
    assert h["last_run_id"] == "a"


def test_runs_never_overlap():
    active = 0
    peak = 0
    guard = threading.Lock()

    def run_fn(settings, **kw):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return FakeResult("x", {})

    svc = make_service(run_fn)
    threads = [threading.Thread(target=svc.run_pending_now) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(2)
    assert peak == 1


def test_healthz_surfaces_stale_and_unavailable_snapshot():
    results = iter(
        [FakeResult("a", {}, snapshot="stale"), FakeResult("b", {"snapshot_unavailable": 1}, snapshot="unavailable")]
    )
    svc = make_service(lambda s, **kw: next(results))
    svc.run_pending_now()
    assert svc.health()["snapshot"] == "stale"
    svc.run_pending_now()
    assert svc.health()["snapshot"] == "unavailable"


def test_handler_has_socket_timeout():
    server = make_server(make_service(lambda s, **kw: None), "127.0.0.1", 0)
    try:
        assert server.RequestHandlerClass.timeout == 10
    finally:
        server.server_close()


# --- loop heartbeat (/healthz 503 when the loop is dead or stuck) ---


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now


def test_healthz_503_before_loop_starts():
    svc = make_service(lambda s, **kw: FakeResult("a", {}))
    server, base = start_http(svc)
    try:
        code, body = get(base + "/healthz")
        assert (code, body["loop"]) == (503, "stale")
    finally:
        server.shutdown()
        server.server_close()


def test_healthz_503_when_heartbeat_stale():
    from datetime import timedelta

    clock = Clock()
    svc = make_service(lambda s, **kw: FakeResult("a", {}), clock=clock)
    server, base = start_http(svc)
    stop, t = start_loop(svc)
    try:
        clock.now += timedelta(minutes=2 * 15 + 5)  # exactly at the limit: still ok
        assert get(base + "/healthz")[0] == 200
        clock.now += timedelta(seconds=1)
        code, body = get(base + "/healthz")
        assert (code, body["loop"]) == (503, "stale")
        svc.run_pending_now()  # a finished run refreshes the heartbeat
        assert get(base + "/healthz")[0] == 200
    finally:
        stop_loop(svc, stop, t)
        server.shutdown()
        server.server_close()


def test_healthz_503_when_loop_thread_died():
    svc = make_service(lambda s, **kw: FakeResult("a", {}))
    stop, t = start_loop(svc)
    stop_loop(svc, stop, t)
    assert not t.is_alive()
    assert svc.health()["loop"] == "stale"
    assert svc.healthy() is False


def test_healthz_never_contains_secrets():
    def boom(s, **kw):
        raise RuntimeError("http://plex?X-Plex-Token=PLEXSECRET apikey=SHOKOSECRET")

    svc = make_service(boom)
    server, base = start_http(svc)
    stop, t = start_loop(svc)
    try:
        svc.run_pending_now()
        req = urllib.request.urlopen(base + "/healthz", timeout=2)
        raw = req.read().decode()
        assert "PLEXSECRET" not in raw and "SHOKOSECRET" not in raw
        assert "error:RuntimeError" in raw
    finally:
        stop_loop(svc, stop, t)
        server.shutdown()
        server.server_close()


# --- shutdown waits for the in-flight run ---


def test_shutdown_waits_for_inflight_run_within_grace(caplog):
    from plex_anidb_reconciler.serve import finish_shutdown

    started, gate = threading.Event(), threading.Event()

    def run_fn(settings, **kw):
        started.set()
        gate.wait(5)
        return FakeResult("a", {})

    svc = make_service(run_fn)
    stop = threading.Event()
    t = threading.Thread(target=svc.loop, args=(stop,), daemon=True)
    svc.request_run()
    t.start()
    assert started.wait(2)
    stop.set()
    svc.request_run()
    with caplog.at_level("WARNING"):
        assert finish_shutdown(t, 0.2) is False  # still running after the grace
    assert "still running" in caplog.text
    threading.Timer(0.1, gate.set).start()
    caplog.clear()
    assert finish_shutdown(t, 2) is True  # run completes inside the grace: clean exit
    assert "still running" not in caplog.text
    assert svc.health()["last_run_id"] == "a"


# --- /trigger body handling ---


def raw_post(base, path, headers, body=b""):
    import http.client
    from urllib.parse import urlsplit

    u = urlsplit(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    conn.putrequest("POST", path, skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    if body:
        conn.send(body)
    resp = conn.getresponse()
    out = (resp.status, resp.getheader("Connection"), resp.read())
    return conn, out


@pytest.fixture
def http_svc():
    svc = make_service(lambda s, **kw: FakeResult("a", {}))
    server, base = start_http(svc)
    yield svc, base
    server.shutdown()
    server.server_close()


def test_trigger_without_content_length(http_svc):
    svc, base = http_svc
    conn, (code, _, _) = raw_post(base, "/trigger", {})
    conn.close()
    assert code == 202 and svc._event.is_set()


@pytest.mark.parametrize("value", ["-1", "abc", "1e3", ""])
def test_trigger_bad_content_length_400(http_svc, value):
    svc, base = http_svc
    conn, (code, connection, _) = raw_post(base, "/trigger", {"Content-Length": value})
    conn.close()
    assert (code, connection) == (400, "close")
    assert not svc._event.is_set()


def test_trigger_huge_content_length_413(http_svc):
    svc, base = http_svc
    conn, (code, connection, _) = raw_post(base, "/trigger", {"Content-Length": str(10 * 1024 * 1024)})
    conn.close()
    assert (code, connection) == (413, "close")
    assert not svc._event.is_set()


def test_trigger_large_body_fully_drained_keeps_connection_usable(http_svc):
    _, base = http_svc
    body = b"x" * (200 * 1024)  # > the old 64 KiB partial read, < 1 MiB
    conn, (code, _, _) = raw_post(base, "/trigger", {"Content-Length": str(len(body))}, body)
    assert code == 202
    conn.request("GET", "/healthz")  # same keep-alive connection: the body must not leak into it
    resp = conn.getresponse()
    assert resp.status in (200, 503) and "loop" in json.loads(resp.read())
    conn.close()


def test_post_unknown_path_404(http_svc):
    svc, base = http_svc
    conn, (code, _, _) = raw_post(base, "/nope", {"Content-Length": "3"}, b"abc")
    conn.close()
    assert code == 404 and not svc._event.is_set()


# --- IPv6 listen ---


def test_make_server_binds_ipv6_literal():
    import socket

    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    svc = make_service(lambda s, **kw: FakeResult("a", {}))
    try:
        server = make_server(svc, "::1", 0)
    except OSError:
        pytest.skip("::1 not available")
    try:
        assert server.address_family == socket.AF_INET6
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        assert get(f"http://[::1]:{port}/healthz")[0] == 503
    finally:
        server.shutdown()
        server.server_close()
