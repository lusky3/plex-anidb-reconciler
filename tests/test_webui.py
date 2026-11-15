import base64
import csv
import json
import os
import socket
import struct
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from plex_anidb_reconciler import webui
from plex_anidb_reconciler.config import Settings
from plex_anidb_reconciler.ledger import Ledger
from plex_anidb_reconciler.models import Action, ActionKind, Rule
from plex_anidb_reconciler.report import COLUMNS, RunMeta
from plex_anidb_reconciler.serve import start_ui
from plex_anidb_reconciler.webui import (
    LedgerUnavailable,
    Page,
    Pages,
    ReasonCount,
    Reports,
    RunSummary,
    authorized,
    fmt_age,
    fmt_time,
    make_ui_server,
    query_params,
    reason_changes,
    state_diff,
)

TOKEN = "u" * 40
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
RUN_OLD = "20261113T120000Z-aaaaaa"  # plan, written before meta files existed
RUN_FULL = "20261114T100000Z-bbbbbb"  # apply, full
RUN_SCOPED = "20261114T110000Z-cccccc"  # apply, scoped (a trigger)
RUN_NOSNAP = "20261114T113000Z-dddddd"  # apply, snapshot unavailable


def row(**kw):
    base = dict.fromkeys(COLUMNS, "")
    base.update(kw)
    return base


def reported(reason, rating_key, aid="", group="", rule="R4"):
    return row(
        rule=rule, kind="report", status="reported", reason=reason, rating_key=rating_key, aid=aid, release_group=group
    )


def write_run(reports: Path, run_id: str, rows: list[dict], meta: dict | None = None) -> None:
    reports.mkdir(parents=True, exist_ok=True)
    with (reports / f"{run_id}.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    if meta is not None:
        full = {"run_id": run_id, "mode": "apply", "full": True, "snapshot": "ok", "rules": None, "file_id": None}
        (reports / f"{run_id}.json").write_text(json.dumps({**full, **meta}), encoding="utf-8")


def act(kind, rule, rating_key, file_id=None, fid=None):
    return Action(
        kind=kind,
        rule=rule,
        rating_key=rating_key,
        file_id=file_id,
        fid=fid,
        eids=(50,),
        when=datetime(2026, 11, 1, tzinfo=UTC),
        reason="r",
    )


@pytest.fixture
def data(tmp_path):
    reports = tmp_path / "reports"
    write_run(
        reports,
        RUN_OLD,
        [
            row(rule="R2", kind="set_file_watched", status="dry_run", reason="new_watch", rating_key="1", file_id="7"),
            reported("no_shoko_match", "2"),
            reported("no_shoko_match", "3"),
            reported("partial_episode", "4"),
            reported("fallback_match", "6"),
            reported("fallback_match", "8"),
        ],
    )
    write_run(
        reports,
        RUN_FULL,
        [
            row(
                rule="R2",
                kind="set_file_watched",
                status="applied",
                reason="new_watch",
                rating_key="1",
                file_id="7",
                fid="12345",
                aid="99",
                when="2026-11-01T00:00:00+00:00",
                detail="<script>alert(1)</script>",
            ),
            row(rule="R1", kind="mark_plex_played", status="failed", reason="anidb_episode_watched", rating_key="5"),
            reported("no_shoko_match", "2"),
            reported("partial_episode", "40", aid="300", group="GrpB"),
            reported("partial_episode", "41", aid="200", group="'-ZR-"),  # as _csv_safe writes "-ZR-"
            reported("partial_episode", "42"),
            reported("ignore_policy", "43", rule="R2b"),
            reported("brand_new_reason", "44"),
        ],
        {},
    )
    write_run(
        reports,
        RUN_SCOPED,
        [row(rule="R1", kind="mark_plex_played", status="applied", reason="anidb_episode_watched", rating_key="9")],
        {"full": False, "rules": ["R1", "R2"], "file_id": 7, "snapshot": "stale"},
    )
    write_run(reports, RUN_NOSNAP, [], {"snapshot": "unavailable"})

    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.record(
        RUN_FULL,
        act(ActionKind.SET_FILE_WATCHED, Rule.R2, "1", file_id=7, fid=12345),
        "applied",
        before={"shoko_last_watched_at": None, "plex_view_count": 1},
        after={"shoko_last_watched_at": "2026-11-01T00:00:00+00:00"},
    )
    ledger.record(RUN_FULL, act(ActionKind.MARK_PLEX_PLAYED, Rule.R1, "5"), "failed", "Plex returned HTTP 500")
    ledger.start(RUN_SCOPED, act(ActionKind.MARK_PLEX_PLAYED, Rule.R1, "9"))
    ledger.close()

    mylist = tmp_path / "mylist.json"
    mylist.write_text("[]", encoding="utf-8")
    two_hours_ago = (NOW - timedelta(hours=2)).timestamp()
    os.utime(mylist, (two_hours_ago, two_hours_ago))
    return tmp_path


def settings_for(data_dir: Path, **extra) -> Settings:
    env = {
        "SHOKO_URL": "http://shoko:8111",
        "SHOKO_API_KEY": "SHOKOSECRET",
        "PLEX_URL": "http://plex:32400",
        "PLEX_TOKEN": "PLEXSECRET",
        "PLEX_SECTIONS": "3,7",
        "DATA_DIR": str(data_dir),
        "MYLIST_PATH": str(data_dir / "mylist.json"),
        "PATH_MAP": "/plex/anime=/shoko/anime",
        "UI_LISTEN": "127.0.0.1:8788",
        "UI_TOKEN": TOKEN,
        **extra,
    }
    return Settings.from_env(env)


class FakeService:
    def __init__(self, health=None, next_poll=None):
        self._health = health or {
            "last_run_id": RUN_NOSNAP,
            "last_status": "snapshot_unavailable",
            "last_finished": "2026-11-14T11:30:05+00:00",
            "snapshot": "unavailable",
            "failed_runs": 1,
            "loop": "ok",
        }
        self._next = next_poll

    def health(self):
        return dict(self._health)

    def next_poll(self):
        return self._next


@pytest.fixture
def pages(data):
    return Pages(FakeService(next_poll=NOW + timedelta(minutes=12)), settings_for(data), clock=lambda: NOW)


@pytest.fixture
def server(pages):
    srv = make_ui_server(pages, TOKEN, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def fetch(url, *, auth=f"Bearer {TOKEN}", method="GET"):
    req = urllib.request.Request(url, method=method)
    if auth:
        req.add_header("Authorization", auth)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as err:
        with err:
            return err.code, dict(err.headers), err.read().decode()


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def summaries(pages):
    return {s.run_id: s for s in pages.reports.summaries()}


# --- auth and headers ---


@pytest.mark.parametrize(
    ("header", "ok"),
    [
        (None, False),
        ("", False),
        (f"Bearer {TOKEN}", True),
        (f"bearer   {TOKEN}  ", True),
        (basic("anyone", TOKEN), True),
        (basic("", TOKEN), True),
        (basic(TOKEN, "nope"), False),
        (f"Bearer {TOKEN[:-1]}", False),
        ("Basic !!!notbase64", False),
        ("Basic " + base64.b64encode(b"\xff\xfe:x").decode(), False),
        (f"Token {TOKEN}", False),
    ],
)
def test_authorized(header, ok):
    assert authorized(header, TOKEN) is ok


def test_authorized_refuses_an_empty_token():
    assert not authorized("Bearer ", "")


def test_requests_without_the_token_get_401_and_no_content(server):
    for auth in (None, "Bearer wrong", basic("x", "wrong")):
        code, headers, body = fetch(server + "/", auth=auth)
        assert code == 401
        assert headers["WWW-Authenticate"] == 'Basic realm="pxar", charset="UTF-8"'
        assert RUN_FULL not in body
    assert fetch(server + "/style.css", auth=None)[0] == 401


@pytest.mark.parametrize(("path", "auth"), [("/", "token"), ("/style.css", "token"), ("/nope", "token"), ("/", None)])
def test_every_response_carries_security_headers(server, path, auth):
    _, headers, _ = fetch(server + path, auth=f"Bearer {TOKEN}" if auth else None)
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert "script-src" not in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Server"] == "pxar"


def test_pages_have_no_scripts_and_never_show_secrets(server):
    for path in ("/", "/runs", f"/runs/{RUN_FULL}", "/reported?reason=partial_episode", "/ledger"):
        code, _, body = fetch(server + path, auth=basic("me", TOKEN))
        assert code == 200, path
        assert "<script" not in body
        for secret in ("SHOKOSECRET", "PLEXSECRET", TOKEN):
            assert secret not in body


def test_only_get_and_head_are_served(server):
    code, headers, body = fetch(server + "/", method="HEAD")
    assert code == 200 and body == "" and int(headers["Content-Length"]) > 0
    assert fetch(server + "/", method="POST")[0] == 501
    assert fetch(server + "/", method="DELETE")[0] == 501


def test_style_is_served_as_css(server):
    code, headers, body = fetch(server + "/style.css")
    assert code == 200 and headers["Content-Type"].startswith("text/css") and "prefers-color-scheme" in body


def test_unknown_paths_and_traversal_get_404(server):
    for path in ("/nope", "/runs/../ledger.sqlite", "/runs/20261114T100000Z-BBBBBB", "/runs/x.csv", "/ledger.sqlite"):
        assert fetch(server + path)[0] == 404, path


def test_a_trailing_slash_is_ignored(server):
    assert fetch(server + "/runs/")[0] == 200


def test_a_failing_page_returns_500_without_details(pages, server, caplog, monkeypatch):
    def boom(params):
        raise RuntimeError("secret detail http://plex:32400?X-Plex-Token=PLEXSECRET")

    monkeypatch.setattr(pages, "runs", boom)
    code, _, body = fetch(server + "/runs")
    assert code == 500 and "Something went wrong" in body and "PLEXSECRET" not in body
    assert "RuntimeError" in caplog.text and "PLEXSECRET" not in caplog.text


def test_a_client_that_hangs_up_mid_reply_leaves_no_traceback(pages, server, monkeypatch, capfd):
    monkeypatch.setattr(pages, "status", lambda params: (200, "x" * 8_000_000))
    port = int(server.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port)) as sock:
        sock.sendall(f"GET / HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode())
        sock.recv(1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close with a reset
    assert fetch(server + "/runs")[0] == 200  # the server is still serving
    assert "Traceback" not in capfd.readouterr().err


# --- status ---


def test_status_page(pages):
    code, body = pages.status({})
    assert code == 200
    assert f'<a href="/runs/{RUN_NOSNAP}">{RUN_NOSNAP}</a>' in body
    assert '<span class="bad">snapshot_unavailable</span>' in body
    assert '<span class="bad">1</span> of 3 allowed' in body
    assert "2026-11-14 12:12 UTC (in 12 min)" in body
    assert '<span class="ok">fresh</span>' in body and "2 h 0 min old" in body
    assert '<a href="/ledger?status=pending">1</a>' in body
    assert "pending R1 rows" in body
    assert "/plex/anime → /shoko/anime" in body
    assert '<a href="/" aria-current="page">Status</a>' in body


def test_status_page_while_a_run_is_active_and_unhealthy(data):
    health = {
        "last_run_id": None,
        "last_status": None,
        "last_finished": None,
        "snapshot": None,
        "failed_runs": 3,
        "loop": "stale",
        "run_active_since": "2026-11-14T11:00:00+00:00",
        "unhealthy": "3 consecutive failed runs (last: error:ShokoError)",
    }
    settings = settings_for(data, PATH_MAP="", MYLIST_PATH=str(data / "missing.json"))
    code, body = Pages(FakeService(health), settings, clock=lambda: NOW).status({})
    assert code == 200
    assert '<span class="bad">stale</span>' in body
    assert "2026-11-14 11:00 UTC" in body and "next poll" not in body
    assert "3 consecutive failed runs" in body
    assert '<span class="bad">unavailable</span>' in body
    assert '<span class="muted">none</span>' in body


def test_status_page_with_a_future_snapshot_and_no_ledger_or_runs(tmp_path):
    (tmp_path / "mylist.json").write_text("[]", encoding="utf-8")
    future = (NOW + timedelta(hours=1)).timestamp()
    os.utime(tmp_path / "mylist.json", (future, future))
    code, body = Pages(FakeService(), settings_for(tmp_path), clock=lambda: NOW).status({})
    assert code == 200
    assert '<span class="bad">stale</span>' in body and "1 h 0 min in the future (check the clocks)" in body
    assert "no ledger yet" in body
    assert "No runs yet." in body


# --- runs ---


def test_run_summaries_know_mode_scope_and_snapshot(pages):
    runs = summaries(pages)
    assert list(runs) == [RUN_NOSNAP, RUN_SCOPED, RUN_FULL, RUN_OLD]  # newest first
    assert (runs[RUN_OLD].mode, runs[RUN_OLD].scope, runs[RUN_OLD].snapshot) == ("plan", "?", "")  # no meta file
    assert (runs[RUN_FULL].mode, runs[RUN_FULL].scope, runs[RUN_FULL].snapshot) == ("apply", "full", "ok")
    assert runs[RUN_SCOPED].scope_label == "scoped · rules R1,R2 · file 7"
    assert runs[RUN_NOSNAP].snapshot == "unavailable"


def test_runs_page(pages):
    code, body = pages.runs({})
    assert code == 200
    assert '<td data-label="scope"><span>scoped</span></td>' in body  # stacked labels for phones
    assert '<a href="/runs" aria-current="page">Runs</a>' in body
    assert "The newest 200 runs" in body


def test_runs_page_filters_by_mode(pages):
    _, body = pages.runs({"mode": "plan"})
    assert RUN_OLD in body and RUN_FULL not in body
    _, body = pages.runs({"mode": "nope"})
    assert "No rows match these filters." in body


def test_runs_page_paginates_and_clamps(pages, monkeypatch):
    monkeypatch.setattr(webui, "PAGE_ROWS", 2)
    _, first = pages.runs({})
    assert RUN_NOSNAP in first and RUN_FULL not in first and "page 1 of 2" in first and "?page=2" in first
    _, second = pages.runs({"page": "2"})
    assert RUN_FULL in second and RUN_NOSNAP not in second and "?page=1" in second
    _, beyond = pages.runs({"page": "9"})
    assert "page 2 of 2" in beyond and RUN_FULL in beyond


def test_run_detail_escapes_links_and_filters(pages):
    code, body = pages.run_detail(RUN_FULL, {})
    assert code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body and "<script>" not in body
    assert '<a href="https://anidb.net/file/12345" rel="noreferrer noopener">12345</a>' in body
    assert '<a href="https://anidb.net/anime/99" rel="noreferrer noopener">99</a>' in body
    assert "8 of 8 rows match" in body
    assert f'href="/runs/{RUN_FULL}.csv"' in body
    assert "<td>-ZR-</td>" in body and "'-ZR-" not in body  # the CSV formula guard isn't data
    for params, n in (
        ({"status": "reported", "reason": "partial_episode"}, 3),
        ({"item": "12345"}, 1),
        ({"aid": "300", "rule": "R4"}, 1),
    ):
        assert f"{n} of 8 rows match" in pages.run_detail(RUN_FULL, params)[1]
    _, body = pages.run_detail(RUN_FULL, {"item": "nothing"})
    assert "No rows match these filters." in body


def test_run_detail_shows_the_scope(pages):
    _, body = pages.run_detail(RUN_SCOPED, {})
    assert "scoped · rules R1,R2 · file 7" in body
    assert '<span class="bad">stale</span>' in body


def test_run_detail_of_an_old_run_has_unknown_snapshot(pages):
    _, body = pages.run_detail(RUN_OLD, {})
    assert '<dt>snapshot</dt><dd><span class="muted">?</span>' in body


def test_run_detail_of_a_missing_or_unreadable_run(pages, data):
    assert pages.run_detail("20200101T000000Z-000000", {})[0] == 404
    (data / "reports" / f"{RUN_OLD}.csv").write_bytes(b"\xff\xfe\x00bad")
    assert pages.run_detail(RUN_OLD, {})[0] == 404
    assert summaries(pages)[RUN_OLD].unreadable


def test_csv_download(server, data):
    code, headers, body = fetch(f"{server}/runs/{RUN_FULL}.csv")
    assert code == 200
    assert headers["Content-Type"] == "text/csv; charset=utf-8"
    assert headers["Content-Disposition"] == f"attachment; filename=pxar-{RUN_FULL}.csv"
    assert body == (data / "reports" / f"{RUN_FULL}.csv").read_bytes().decode()
    assert fetch(f"{server}/runs/20200101T000000Z-000000.csv")[0] == 404


# --- reported items ---


def test_reason_changes_against_the_previous_full_run(pages):
    runs = summaries(pages)
    changes = reason_changes(runs[RUN_FULL], runs[RUN_OLD])
    assert changes == [
        ReasonCount("R4", "partial_episode", 3, 2),
        ReasonCount("R2b", "ignore_policy", 1, 1),
        ReasonCount("R4", "brand_new_reason", 1, 1),
        ReasonCount("R4", "no_shoko_match", 1, -1),
        ReasonCount("R4", "fallback_match", 0, -2),  # fully fixed, still listed
    ]
    assert all(c.change is None for c in reason_changes(runs[RUN_FULL], None))


def test_reported_compares_the_latest_two_full_runs(pages):
    code, body = pages.reported({})
    assert code == 200
    assert f'latest full run, <a href="/runs/{RUN_FULL}">' in body  # not the scoped or snapshot-less run
    assert f'(<a href="/runs/{RUN_OLD}">' in body
    assert "Only some of the episodes involved are watched in AniDB." in body
    assert '<td class="num" data-label="change"><span><span class="ok">-2</span></span></td>' in body
    assert 'class="sel"' not in body


def test_reported_lists_one_reason_sorted_by_anime(pages):
    _, body = pages.reported({"reason": "partial_episode"})
    assert "<h2>partial_episode</h2>" in body and "3 items" in body
    assert '<tr class="sel"><td data-label="rule"><span>R4</span></td>' in body
    items = body[body.index("<h2>partial_episode</h2>") :]
    assert items.index("-ZR-") < items.index("GrpB") < items.index("<td>42</td>")  # aid 200, 300, none
    assert "<th>reason</th>" not in items  # the same on every row: not repeated
    assert f'href="/runs/{RUN_FULL}?status=reported&amp;reason=partial_episode"' in items


def test_reported_without_a_previous_full_run(pages, data):
    (data / "reports" / f"{RUN_OLD}.csv").unlink()
    _, body = pages.reported({})
    assert "previous full run." in body


def test_reported_with_no_full_run(tmp_path):
    write_run(tmp_path / "reports", RUN_SCOPED, [], {"full": False})
    write_run(tmp_path / "reports", RUN_OLD, [])  # no meta and no rows: can't tell what it was
    code, body = Pages(FakeService(), settings_for(tmp_path), clock=lambda: NOW).reported({})
    assert code == 200 and "No complete run report yet." in body


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (RunSummary("r", RunMeta("r", "apply", True, "ok")), True),
        (RunSummary("r", RunMeta("r", "apply", False, "ok")), False),
        (RunSummary("r", RunMeta("r", "apply", True, "unavailable")), False),
        (RunSummary("r", RunMeta("r", "apply", True, "ok"), unreadable=True), False),
        (RunSummary("r", None, rows=3, statuses={"dry_run": 1}), True),
        (RunSummary("r", None, rows=3, statuses={"reported": 3}), False),
        (RunSummary("r", None), False),
    ],
)
def test_which_runs_list_everything(summary, expected):
    assert summary.lists_everything is expected


# --- ledger ---


def test_ledger_page_lists_writes_with_their_state_change(pages):
    code, body = pages.ledger({})
    assert code == 200
    assert "3 rows match" in body
    assert "plex_view_count: 1" in body
    assert "shoko_last_watched_at: None → 2026-11-01T00:00:00+00:00" in body
    assert "Plex returned HTTP 500" in body
    assert '<span class="bad">pending</span>' in body
    assert '<label>Plex item<input name="rating_key"' in body


@pytest.mark.parametrize(
    ("filters", "count"),
    [
        ({}, 3),
        ({"status": "pending"}, 1),
        ({"rule": "R1"}, 2),
        ({"run_id": RUN_FULL}, 2),
        ({"rating_key": "5"}, 1),
        ({"file_id": "7"}, 1),
        ({"file_id": "x7"}, 0),
        ({"file_id": "9" * 19}, 0),
        ({"status": "applied", "rule": "R1"}, 0),
    ],
)
def test_ledger_filters(pages, filters, count):
    assert pages.ledger_view.count(filters) == count
    assert len(pages.ledger_view.rows(filters, 0)) == count


def test_ledger_rows_are_newest_first(pages):
    assert [r["id"] for r in pages.ledger_view.rows({}, 0)] == [3, 2, 1]


def test_ledger_page_with_no_match_and_pages(pages, monkeypatch):
    assert "No rows match these filters." in pages.ledger({"rule": "R2b"})[1]
    monkeypatch.setattr(webui, "PAGE_ROWS", 2)
    _, body = pages.ledger({"page": "2"})
    assert "page 2 of 2" in body and "Previous page" in body


def test_ledger_missing_or_corrupt(tmp_path):
    p = Pages(FakeService(), settings_for(tmp_path), clock=lambda: NOW)
    assert "no ledger yet" in p.ledger({})[1]
    (tmp_path / "ledger.sqlite").write_bytes(b"this is not a database at all, not even close" * 40)
    assert "ledger unreadable (DatabaseError)" in p.ledger({})[1]
    assert "ledger unreadable" in p.status({})[1]


def test_ledger_connection_is_closed_when_setup_fails(pages, monkeypatch):
    closed = []

    class Conn:
        def execute(self, *a):
            raise webui.sqlite3.OperationalError("nope")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(webui.sqlite3, "connect", lambda *a, **kw: Conn())
    with pytest.raises(LedgerUnavailable, match="OperationalError"):
        pages.ledger_view.status_counts()
    assert closed == [True]


def test_ledger_connect_failure_is_reported(pages, monkeypatch):
    def refuse(*a, **kw):
        raise webui.sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(webui.sqlite3, "connect", refuse)
    assert "ledger unreadable (OperationalError)" in pages.ledger({})[1]


def test_ledger_is_opened_read_only(pages, data):
    before = (data / "ledger.sqlite").read_bytes()
    pages.ledger({})
    pages.status({})
    assert (data / "ledger.sqlite").read_bytes() == before
    with pytest.raises(LedgerUnavailable), pages.ledger_view._connection() as conn:
        conn.execute("DELETE FROM actions")


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (None, None, ""),
        ('{"a": 1}', None, "a: 1"),
        (None, '{"a": 2}', "a: → 2"),
        ("not json", None, "?: not json"),
        ("[1]", None, "?: [1]"),
    ],
)
def test_state_diff(before, after, expected):
    assert state_diff(before, after) == (f"<pre>{expected}</pre>" if expected else "")


# --- the report cache ---


def test_reports_cache_follows_file_changes_and_pruning(data):
    reports = Reports(data / "reports")
    reports.summaries()
    first = reports.summary(RUN_FULL)
    assert reports.summary(RUN_FULL) is first
    write_run(data / "reports", RUN_FULL, [reported("no_date", "1")], {"mode": "plan"})
    second = reports.summary(RUN_FULL)
    assert second is not first and second.mode == "plan" and second.reasons == {("R4", "no_date"): 1}
    (data / "reports" / f"{RUN_OLD}.csv").unlink()
    reports.summaries()
    assert RUN_OLD not in reports._cache
    assert reports.summary(RUN_OLD) is None


def test_an_unreadable_report_is_not_cached(data, monkeypatch):
    reports = Reports(data / "reports")
    real_rows = reports.rows
    monkeypatch.setattr(reports, "rows", lambda run_id: None)  # e.g. EMFILE
    assert reports.summary(RUN_FULL).unreadable
    monkeypatch.setattr(reports, "rows", real_rows)
    assert not reports.summary(RUN_FULL).unreadable


def test_a_cold_cache_builds_each_summary_once(data, monkeypatch):
    reports = Reports(data / "reports")
    builds, entered, release = [], threading.Event(), threading.Event()
    real = reports._summarize

    def slow(run_id):
        builds.append(run_id)
        entered.set()
        release.wait(5)
        return real(run_id)

    monkeypatch.setattr(reports, "_summarize", slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(reports.summary(RUN_FULL))) for _ in range(4)]
    for t in threads:
        t.start()
    assert entered.wait(5)
    release.set()
    for t in threads:
        t.join(5)
    assert builds == [RUN_FULL] and len(results) == 4 and len({id(r) for r in results}) == 1


def test_reports_ignore_foreign_files_and_bad_meta(data):
    reports_dir = data / "reports"
    (reports_dir / "notes.csv").write_text("x", encoding="utf-8")
    (reports_dir / f"{RUN_FULL}.json").write_text("[1, 2]", encoding="utf-8")
    reports = Reports(reports_dir)
    assert "notes" not in reports.run_ids()
    summary = reports.summary(RUN_FULL)
    assert summary.meta is None and summary.mode == "apply" and summary.scope == "?"  # inferred from rows


def test_reports_in_a_missing_or_unlistable_directory(tmp_path, monkeypatch):
    assert Reports(tmp_path / "nope").summaries() == []

    def deny(self, pattern):
        raise PermissionError

    monkeypatch.setattr(Path, "glob", deny)
    assert Reports(tmp_path).run_ids() == []


def test_mode_cannot_be_inferred_without_telling_rows(tmp_path):
    write_run(tmp_path, RUN_OLD, [reported("no_date", "1")])
    assert Reports(tmp_path).summary(RUN_OLD).mode == "?"
    write_run(tmp_path, RUN_FULL, [row(status="capped")])
    assert Reports(tmp_path).summary(RUN_FULL).mode == "apply"


# --- helpers ---


@pytest.mark.parametrize(
    ("age", "text"),
    [
        (timedelta(minutes=5), "5 min"),
        (timedelta(hours=3, minutes=7), "3 h 7 min"),
        (timedelta(days=2, hours=4), "2 d 4 h"),
        (timedelta(minutes=-12), "in 12 min"),
    ],
)
def test_fmt_age(age, text):
    assert fmt_age(age) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        ("", ""),
        ("garbage", "garbage"),
        (7, "7"),
        ("2026-11-14T07:00:00-05:00", "2026-11-14 12:00 UTC"),
        (NOW, "2026-11-14 12:00 UTC"),
    ],
)
def test_fmt_time(value, text):
    assert fmt_time(value) == text


def test_query_params():
    assert query_params("a=1&a=2&b=+x+&c=&d=" + "z" * 500) == {"a": "1", "b": "x", "d": "z" * 200}
    assert query_params("&".join(f"k{i}=1" for i in range(30))) == {}


@pytest.mark.parametrize(
    ("raw", "total", "number", "pages"),
    [
        ("3", 1000, 3, 5),
        ("0", 1000, 1, 5),
        ("-1", 1000, 1, 5),
        ("x", 1000, 1, 5),
        ("٣", 1000, 1, 5),
        ("9999999", 1000, 1, 5),
        ("9", 1000, 5, 5),
        ("1", 0, 1, 1),
    ],
)
def test_page_of(raw, total, number, pages):
    page = Page.of({"page": raw}, total)
    assert (page.number, page.pages) == (number, pages)
    assert page.offset == (number - 1) * webui.PAGE_ROWS


def test_anidb_link_only_for_numeric_ids():
    assert webui.anidb_link("anime", "") == ""
    assert webui.anidb_link("anime", None) == ""
    assert webui.anidb_link("anime", "<b>") == "&lt;b&gt;"


def test_run_link_escapes_odd_values():
    assert webui.run_link("<x>") == "&lt;x&gt;"
    assert webui.run_link(None) == ""


def test_badge_classes():
    assert webui.badge("error:ShokoError") == '<span class="bad">error:ShokoError</span>'
    assert webui.badge("snapshot_unavailable") == '<span class="bad">snapshot_unavailable</span>'
    assert webui.badge("skipped") == '<span class="warn">skipped</span>'
    assert webui.badge("reported") == "reported"
    assert webui.badge(None) == ""


def test_change_html():
    assert webui.change_html(None) == ""
    assert webui.change_html(0) == '<span class="muted">0</span>'


# --- serve wiring ---


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_start_ui_serves_the_real_pages(data):
    ui = start_ui(FakeService(), settings_for(data, UI_LISTEN=f"127.0.0.1:{free_port()}"))
    try:
        url = f"http://127.0.0.1:{ui.server_address[1]}"
        assert fetch(url + "/runs")[0] == 200
        assert fetch(url + "/runs", auth=None)[0] == 401
    finally:
        ui.shutdown()
        ui.server_close()
