import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


WAIT = 5  # upper bound for event waits; only a broken test ever reaches it


class ObservedService(Service):
    """Service that signals loop heartbeats and finished runs, so tests wait on events, not sleeps."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.beaten = threading.Event()
        self._finished = threading.Condition()
        self.runs_finished = 0

    def _beat(self):
        super()._beat()
        self.beaten.set()

    def run_pending_now(self, rating_keys=None):
        super().run_pending_now(rating_keys)
        with self._finished:
            self.runs_finished += 1
            self._finished.notify_all()

    def wait_runs(self, n, timeout=WAIT):
        with self._finished:
            return self._finished.wait_for(lambda: self.runs_finished >= n, timeout)


def make_service(run_fn, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)):
    settings = SimpleNamespace(
        poll_minutes=15,
        listen="127.0.0.1:0",
        shutdown_grace_s=25,
        run_stale_minutes=360,
        health_max_failed_runs=3,
        shoko_api_key="SHOKOSECRET",
        plex_token="PLEXSECRET",
    )
    return ObservedService(settings, run_fn=run_fn, clock=clock)


def start_http(svc):
    server = make_server(svc, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def post(url, body=b"x"):
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.status, json.loads(r.read())


def test_trigger_coalesces_concurrent_requests():
    calls = []
    gate = threading.Event()
    first_started = threading.Event()

    def run_fn(settings, **kw):
        calls.append(kw)
        first_started.set()
        gate.wait(30)  # released by the test; the bound only keeps a broken test from hanging forever
        return FakeResult(f"r{len(calls)}", {"moved": 1})

    svc = make_service(run_fn)
    server, base = start_http(svc)
    stop = threading.Event()
    t = threading.Thread(target=svc.loop, args=(stop,), daemon=True)
    try:
        svc.request_run()
        t.start()
        assert first_started.wait(WAIT)  # first run is in progress
        for _ in range(5):
            assert post(base + "/trigger")[0] == 202
        gate.set()
        assert svc.wait_runs(2)
        # nothing is pending after the second run, so no third run can start: 5 POSTs -> exactly 1 extra run
        assert not svc._event.is_set()
        assert len(calls) == 2
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
    assert svc.beaten.wait(WAIT)
    assert svc.health()["loop"] == "ok"
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
        with e:
            return e.code, json.loads(e.read())


def test_healthz_reports_last_run():
    svc = make_service(lambda s, **kw: FakeResult("abc", {"moved": 2}))
    server, base = start_http(svc)
    stop, t = start_loop(svc)
    try:
        assert get(base + "/healthz") == (
            200,
            {
                "last_run_id": None,
                "last_status": None,
                "last_finished": None,
                "snapshot": None,
                "failed_runs": 0,
                "loop": "ok",
            },
        )
        svc.run_pending_now()
        assert get(base + "/healthz") == (
            200,
            {
                "last_run_id": "abc",
                "last_status": "ok",
                "last_finished": "2026-01-01T00:00:00+00:00",
                "snapshot": "ok",
                "failed_runs": 0,
                "loop": "ok",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/nope", timeout=2)
        ei.value.close()
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
    inside = threading.Event()
    release = threading.Event()

    def run_fn(settings, **kw):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        inside.set()
        release.wait(WAIT)  # the first run holds the lock until the others are queued behind it
        with guard:
            active -= 1
        return FakeResult("x", {})

    svc = make_service(run_fn)
    threads = [threading.Thread(target=svc.run_pending_now) for _ in range(4)]
    threads[0].start()
    assert inside.wait(WAIT)
    for t in threads[1:]:
        t.start()
    release.set()
    for t in threads:
        t.join(WAIT)
    assert svc.runs_finished == 4
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
        with urllib.request.urlopen(base + "/healthz", timeout=2) as req:
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


def test_trigger_large_body_fully_drained_before_response(http_svc):
    _, base = http_svc
    body = b"x" * (200 * 1024)  # > the old 64 KiB partial read, < 1 MiB
    conn, (code, _, _) = raw_post(base, "/trigger", {"Content-Length": str(len(body))}, body)
    assert code == 202
    # the server is still sane after the large body (HTTP/1.0 handler: no keep-alive is claimed)
    conn.request("GET", "/healthz")
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


# --- heartbeat during a long run ---


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def test_long_run_keeps_healthz_ok_but_dead_loop_is_503():
    clock = FakeClock()
    gate = threading.Event()
    started = threading.Event()

    def run_fn(settings, **kw):
        started.set()
        gate.wait(30)
        return FakeResult("r1", {"moved": 1})

    svc = make_service(run_fn, clock=clock)
    stop, t = start_loop(svc)
    try:
        svc.request_run()
        assert started.wait(2)
        clock.advance(hours=3)  # far beyond 2 x POLL_MINUTES + 5 min, run still in progress
        assert svc.healthy() is True
        gate.set()
        assert svc.wait_runs(1)
        assert svc.health()["last_run_id"] == "r1"
        assert svc.healthy() is True  # finishing the run re-beats
        clock.advance(hours=3)  # idle and no beat for hours: stale
        assert svc.healthy() is False
    finally:
        gate.set()
        stop_loop(svc, stop, t)
    assert svc.healthy() is False  # loop thread dead


def test_dead_loop_thread_is_503_even_during_lock_hold():
    svc = make_service(lambda s, **kw: FakeResult("a", {}))
    stop, t = start_loop(svc)
    stop_loop(svc, stop, t)
    assert not t.is_alive()
    with svc._lock:
        assert svc.healthy() is False


# --- healthcheck probe target derived from LISTEN ---


@pytest.mark.parametrize(
    ("listen", "url"),
    [
        (None, "http://127.0.0.1:8787/healthz"),
        ("", "http://127.0.0.1:8787/healthz"),
        ("0.0.0.0:8787", "http://127.0.0.1:8787/healthz"),
        (":9000", "http://127.0.0.1:9000/healthz"),
        ("[::]:8080", "http://[::1]:8080/healthz"),
        ("[fd00::5]:8080", "http://[fd00::5]:8080/healthz"),
        ("10.1.2.3:81", "http://10.1.2.3:81/healthz"),
        ("  127.0.0.1:7000 ", "http://127.0.0.1:7000/healthz"),
        ("[0:0:0:0:0:0:0:0]:8080", "http://[::1]:8080/healthz"),
        ("[::ffff:0.0.0.0]:8080", "http://[::1]:8080/healthz"),
        ("[fe80::1%eth0]:8080", "http://[fe80::1%25eth0]:8080/healthz"),
        ("myhost:8080", "http://myhost:8080/healthz"),
    ],
)
def test_health_url_derived_from_listen(listen, url):
    from plex_anidb_reconciler.serve import health_url

    assert health_url(listen) == url


def test_healthcheck_exit_codes():
    from plex_anidb_reconciler.serve import healthcheck

    svc = make_service(lambda s, **kw: FakeResult("a", {}))
    server, base = start_http(svc)
    try:
        listen = base.removeprefix("http://")
        assert healthcheck({"LISTEN": listen}) == 1  # no loop yet: 503
        stop, t = start_loop(svc)
        try:
            assert healthcheck({"LISTEN": listen}) == 0
        finally:
            stop_loop(svc, stop, t)
    finally:
        server.shutdown()
        server.server_close()
    assert healthcheck({"LISTEN": "127.0.0.1:1"}) == 1  # nothing listening
    assert healthcheck({"LISTEN": "garbage"}) == 1  # invalid LISTEN is unhealthy, not a crash


def test_hung_run_goes_stale_after_run_stale_minutes():
    clock = FakeClock()
    gate = threading.Event()
    started = threading.Event()

    def run_fn(settings, **kw):
        started.set()
        gate.wait(30)
        return FakeResult("r1", {})

    svc = make_service(run_fn, clock=clock)
    stop, t = start_loop(svc)
    try:
        assert svc.health().get("run_active_since") is None
        svc.request_run()
        assert started.wait(2)
        since = clock().isoformat()
        clock.advance(minutes=359)
        assert svc.healthy() is True and svc.health()["loop"] == "ok"
        assert svc.health()["run_active_since"] == since
        clock.advance(minutes=2)  # 361 min > RUN_STALE_MINUTES
        h = svc.health()
        assert svc.healthy() is False and h["loop"] == "stale" and h["run_active_since"] == since
        server, base = start_http(svc)
        try:
            code, body = get(base + "/healthz")
            assert code == 503 and body["loop"] == "stale" and body["run_active_since"] == since
        finally:
            server.shutdown()
            server.server_close()
        gate.set()
        assert svc.wait_runs(1)
        assert svc.health()["last_run_id"] == "r1"
        assert svc.health().get("run_active_since") is None and svc.healthy() is True
    finally:
        gate.set()
        stop_loop(svc, stop, t)


def test_healthcheck_ignores_proxy_env(monkeypatch):
    from plex_anidb_reconciler.serve import healthcheck

    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:1")  # unreachable proxy
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(urllib.request, "_opener", None)  # urlopen caches its opener (and proxy env) globally
    svc = make_service(lambda s, **kw: FakeResult("a", {}))
    server, base = start_http(svc)
    try:
        stop, t = start_loop(svc)
        try:
            assert healthcheck({"LISTEN": base.removeprefix("http://")}) == 0
        finally:
            stop_loop(svc, stop, t)
    finally:
        server.shutdown()
        server.server_close()


def test_health_loop_and_run_active_since_are_one_snapshot():
    clock = FakeClock()
    gate = threading.Event()
    started = threading.Event()

    def run_fn(settings, **kw):
        started.set()
        gate.wait(30)
        return FakeResult("r1", {})

    svc = make_service(run_fn, clock=clock)
    stop, t = start_loop(svc)
    try:
        svc.request_run()
        assert started.wait(2)
        clock.advance(minutes=400)
        h = svc.health()
        assert h["loop"] == "stale" and h["run_active_since"]
    finally:
        gate.set()
        stop_loop(svc, stop, t)


# --- /trigger scoped to rating keys (Tautulli Webhook JSON data) ---


def scoped_run_setup():
    calls = []
    gate = threading.Event()
    first_started = threading.Event()

    def run_fn(settings, **kw):
        calls.append(kw)
        first_started.set()
        if len(calls) == 1:
            gate.wait(30)  # hold the first run so triggers pile up behind it
        return FakeResult(f"r{len(calls)}", {})

    svc = make_service(run_fn)
    server, base = start_http(svc)
    stop = threading.Event()
    t = threading.Thread(target=svc.loop, args=(stop,), daemon=True)
    svc.request_run()
    t.start()
    assert first_started.wait(WAIT)
    return svc, server, base, stop, t, calls, gate


def teardown(svc, server, stop, t, gate):
    gate.set()
    stop.set()
    svc.request_run()
    t.join(2)
    server.shutdown()
    server.server_close()


def test_trigger_rating_keys_coalesce_into_one_scoped_run():
    svc, server, base, stop, t, calls, gate = scoped_run_setup()
    try:
        assert post(base + "/trigger", b'{"rating_key": "123"}')[0] == 202
        assert post(base + "/trigger", b'{"rating_key": "456"}')[0] == 202
        assert post(base + "/trigger", b'{"rating_key": 789}')[0] == 202  # a number works too
        assert post(base + "/trigger", b'{"rating_key": "123"}')[0] == 202
        gate.set()
        assert svc.wait_runs(2)
        assert not svc._event.is_set()
        assert calls[0]["rating_keys"] is None  # startup run is full
        assert calls[1]["rating_keys"] == frozenset({"123", "456", "789"})
    finally:
        teardown(svc, server, stop, t, gate)


@pytest.mark.parametrize(
    "body",
    [b"", b"x", b"{not json", b"[]", b'{"rating_key": null}', b'{"rating_key": ""}', b'{"rating_key": true}', b"{}"],
    ids=["empty", "text", "bad_json", "list", "null", "blank", "bool", "no_key"],
)
def test_trigger_without_usable_key_means_full_run(body):
    svc, server, base, stop, t, calls, gate = scoped_run_setup()
    try:
        assert post(base + "/trigger", b'{"rating_key": "123"}')[0] == 202
        assert post(base + "/trigger", body)[0] == 202
        gate.set()
        assert svc.wait_runs(2)
        assert calls[1]["rating_keys"] is None  # one keyless trigger widens the pending run to a full one
    finally:
        teardown(svc, server, stop, t, gate)


def test_poll_tick_is_always_full():
    svc = make_service(lambda s, **kw: FakeResult("a", {}))
    svc.request_run("123")
    assert svc._take_scope(triggered=False) is None
    assert svc._take_scope(triggered=True) is None  # the tick consumed the pending key
    svc.request_run("5")
    assert svc._take_scope(triggered=True) == frozenset({"5"})


def test_run_pending_now_passes_scope():
    seen = []
    svc = make_service(lambda s, **kw: seen.append(kw["rating_keys"]) or FakeResult("a", {}))
    svc.run_pending_now()
    svc.run_pending_now(frozenset({"9"}))
    assert seen == [None, frozenset({"9"})]


# --- consecutive failed runs make /healthz unhealthy ---


def test_consecutive_failed_runs_make_healthz_503_until_a_run_succeeds():
    from plex_anidb_reconciler.shoko import ShokoError

    results = iter(
        [
            ShokoError("Shoko GET /api/v3/File returned HTTP 500"),
            FakeResult("b", {"failed": 1}),
            FakeResult("c", {"snapshot_unavailable": 1}, snapshot="unavailable"),
            FakeResult("d", {"applied": 1}),
        ]
    )

    def run_fn(settings, **kw):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    svc = make_service(run_fn)
    server, base = start_http(svc)
    stop, t = start_loop(svc)
    try:
        svc.run_pending_now()
        svc.run_pending_now()
        code, body = get(base + "/healthz")
        assert (code, body["failed_runs"]) == (200, 2) and "unhealthy" not in body
        svc.run_pending_now()  # the third in a row (HEALTH_MAX_FAILED_RUNS=3)
        code, body = get(base + "/healthz")
        assert (code, body["failed_runs"], body["loop"]) == (503, 3, "ok")
        assert body["last_status"] == "snapshot_unavailable"
        assert body["unhealthy"] == "3 consecutive failed runs (last: snapshot_unavailable)"
        svc.run_pending_now()  # a successful run resets the count
        code, body = get(base + "/healthz")
        assert (code, body["failed_runs"], body["last_status"]) == (200, 0, "ok") and "unhealthy" not in body
    finally:
        stop_loop(svc, stop, t)
        server.shutdown()
        server.server_close()


def test_health_max_failed_runs_threshold_is_configurable():
    svc = make_service(lambda s, **kw: FakeResult("a", {"failed": 1}))
    svc.settings.health_max_failed_runs = 1
    svc.run_pending_now()
    assert svc.health()["unhealthy"] == "1 consecutive failed runs (last: failed)"


# --- serve logs backend errors sanitised: the CLI's text, never a URL or secret ---


def test_serve_logs_backend_error_message(caplog):
    from plex_anidb_reconciler.plex import PlexError
    from plex_anidb_reconciler.shoko import ShokoError

    errors = iter(
        [ShokoError("Shoko GET /api/v3/File returned HTTP 500"), PlexError("Plex GET /:/scrobble failed: ReadTimeout")]
    )

    def run_fn(settings, **kw):
        raise next(errors)

    svc = make_service(run_fn)
    with caplog.at_level("ERROR"):
        svc.run_pending_now()
        svc.run_pending_now()
    assert "reconcile run failed: Shoko GET /api/v3/File returned HTTP 500" in caplog.text
    assert "reconcile run failed: Plex GET /:/scrobble failed: ReadTimeout" in caplog.text
    assert svc.health()["last_status"] == "error:PlexError"


def test_serve_logs_only_the_type_of_other_exceptions(caplog):
    def run_fn(settings, **kw):
        raise RuntimeError("http://plex:32400/?X-Plex-Token=PLEXSECRET apikey=SHOKOSECRET")

    svc = make_service(run_fn)
    with caplog.at_level("ERROR"):
        svc.run_pending_now()
    assert "reconcile run failed: RuntimeError" in caplog.text
    assert "PLEXSECRET" not in caplog.text and "SHOKOSECRET" not in caplog.text and "http://" not in caplog.text


def test_serve_real_client_errors_log_no_secrets(tmp_path, caplog):
    """End to end through run_once and the real HTTP clients: transport errors whose text carries the secrets."""
    import httpx
    import respx

    from plex_anidb_reconciler.config import Settings

    mylist = tmp_path / "mylist.json"
    mylist.write_text("[]")
    settings = Settings.from_env(
        {
            "SHOKO_URL": "http://shoko:8111",
            "SHOKO_API_KEY": "SHOKOSECRET",
            "PLEX_URL": "http://plex:32400",
            "PLEX_TOKEN": "PLEXSECRET",
            "PLEX_SECTIONS": "4",
            "MYLIST_PATH": str(mylist),
            "DATA_DIR": str(tmp_path / "data"),
        }
    )
    leak = "http://plex:32400/?X-Plex-Token=PLEXSECRET apikey=SHOKOSECRET"
    svc = Service(settings, clock=lambda: datetime.now(UTC))
    with respx.mock, caplog.at_level("DEBUG"):
        respx.get("http://shoko:8111/api/v3/File").mock(side_effect=httpx.ConnectError(leak))
        svc.run_pending_now()
        respx.get("http://shoko:8111/api/v3/File").mock(return_value=httpx.Response(200, json={"List": []}))
        respx.get(url__regex=r"http://plex:32400/library/sections/4/all.*").mock(
            return_value=httpx.Response(401, text=leak)
        )
        svc.run_pending_now()
    assert "reconcile run failed: GET /api/v3/File failed: ConnectError" in caplog.text
    assert "reconcile run failed: GET /library/sections/4/all returned HTTP 401" in caplog.text
    assert "PLEXSECRET" not in caplog.text and "SHOKOSECRET" not in caplog.text
    assert svc.health()["failed_runs"] == 2
