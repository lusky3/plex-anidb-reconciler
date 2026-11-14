import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from plex_anidb_reconciler.serve import Service, make_server


@dataclass
class FakeResult:
    run_id: str
    counts: dict
    report_path: Path = Path("r.json")


def make_service(run_fn):
    settings = SimpleNamespace(poll_minutes=15, listen="127.0.0.1:0")
    return Service(settings, run_fn=run_fn, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))


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
        gate.wait(1)
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


def test_healthz_reports_last_run():
    svc = make_service(lambda s, **kw: FakeResult("abc", {"moved": 2}))
    server, base = start_http(svc)
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
            assert json.loads(r.read()) == {"last_run_id": None, "last_status": None, "last_finished": None}
        svc.run_pending_now()
        with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
            body = json.loads(r.read())
        assert body == {
            "last_run_id": "abc",
            "last_status": "ok",
            "last_finished": "2026-01-01T00:00:00+00:00",
        }
        try:
            urllib.request.urlopen(base + "/nope", timeout=2)
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
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
