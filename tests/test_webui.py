import base64
import csv
import json
import os
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
from plex_anidb_reconciler.report import COLUMNS
from plex_anidb_reconciler.serve import start_ui
from plex_anidb_reconciler.webui import (
    Pages,
    Reports,
    authorized,
    fmt_delta,
    fmt_time,
    make_ui_server,
    page_number,
    query_params,
)

TOKEN = "u" * 40
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
RUN_OLD = "20261113T120000Z-aaaaaa"  # plan, full, written before meta files existed
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
        (reports / f"{run_id}.json").write_text(json.dumps({"run_id": run_id, **meta}), encoding="utf-8")


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
            reported("partial_episode", "41", aid="200", group="GrpA"),
            reported("partial_episode", "42"),
            reported("ignore_policy", "43", rule="R2b"),
            reported("brand_new_reason", "44"),
        ],
        {"mode": "apply", "full": True, "rules": None, "file_id": None, "snapshot": "ok"},
    )
    write_run(
        reports,
        RUN_SCOPED,
        [row(rule="R1", kind="mark_plex_played", status="applied", reason="anidb_episode_watched", rating_key="9")],
        {"mode": "apply", "full": False, "rules": ["R1", "R2"], "file_id": 7, "snapshot": "stale"},
    )
    write_run(reports, RUN_NOSNAP, [], {"mode": "apply", "full": True, "snapshot": "unavailable"})

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
        assert "pxar run" not in body and RUN_FULL not in body
    assert fetch(server + "/style.css", auth=None)[0] == 401


def test_every_response_carries_security_headers(server):
    for path in ("/", "/style.css", "/nope"):
        _, headers, _ = fetch(server + path)
        assert headers["Content-Security-Policy"].startswith("default-src 'none'")
        assert "script-src" not in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["Cache-Control"] == "no-store"
    _, headers, _ = fetch(server + "/", auth=None)
    assert headers["Cache-Control"] == "no-store"


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


def test_a_failing_page_returns_500_without_details(pages, server, caplog, monkeypatch):
    def boom(params):
        raise RuntimeError("secret detail http://plex:32400?X-Plex-Token=PLEXSECRET")

    monkeypatch.setattr(pages, "runs", boom)
    code, _, body = fetch(server + "/runs")
    assert code == 500 and "Something went wrong" in body and "PLEXSECRET" not in body
    assert "RuntimeError" in caplog.text and "PLEXSECRET" not in caplog.text


# --- status ---


def test_status_page(server):
    code, _, body = fetch(server + "/")
    assert code == 200
    assert f'<a href="/runs/{RUN_NOSNAP}">{RUN_NOSNAP}</a>' in body
    assert "snapshot_unavailable" in body
    assert "1 of 3 allowed" in body
    assert "2026-11-14 12:12 UTC (in 12 min)" in body
    assert '<span class="ok">fresh</span>' in body and "2 h 0 min" in body
    assert '<a href="/ledger?status=pending">1</a>' in body
    assert "pending R1 rows" in body
    assert "/plex/anime → /shoko/anime" in body
    assert body.index(RUN_NOSNAP) < body.index(RUN_SCOPED) < body.index(RUN_FULL) < body.index(RUN_OLD)


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


def test_status_page_with_a_future_snapshot_and_no_ledger(tmp_path):
    (tmp_path / "mylist.json").write_text("[]", encoding="utf-8")
    future = (NOW + timedelta(hours=1)).timestamp()
    os.utime(tmp_path / "mylist.json", (future, future))
    code, body = Pages(FakeService(), settings_for(tmp_path), clock=lambda: NOW).status({})
    assert code == 200
    assert '<span class="bad">stale</span>' in body and "(in the future)" in body
    assert "no ledger yet" in body
    assert "Nothing to show." in body


# --- runs ---


def test_runs_page_lists_mode_scope_and_snapshot(pages):
    code, body = pages.runs({})
    assert code == 200
    assert body.index(RUN_NOSNAP) < body.index(RUN_SCOPED) < body.index(RUN_FULL) < body.index(RUN_OLD)
    old_row = body[body.index(f">{RUN_OLD}<") :]
    assert "<td>plan</td><td>?</td>" in old_row[:400]  # no meta file: mode from the dry_run rows, scope unknown
    assert "<td>apply</td><td>scoped</td>" in body
    assert '<span class="bad">unavailable</span>' in body


def test_runs_page_filters_by_mode(pages):
    _, body = pages.runs({"mode": "plan"})
    assert RUN_OLD in body and RUN_FULL not in body


def test_runs_page_paginates(pages, monkeypatch):
    monkeypatch.setattr(webui, "PAGE_ROWS", 2)
    _, first = pages.runs({})
    assert RUN_NOSNAP in first and RUN_FULL not in first and "page 1 of 2" in first and "?page=2" in first
    _, second = pages.runs({"page": "2"})
    assert RUN_FULL in second and RUN_NOSNAP not in second and "?page=1" in second


def test_run_detail_escapes_and_filters(pages):
    code, body = pages.run_detail(RUN_FULL, {})
    assert code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body and "<script>" not in body
    assert '<a href="https://anidb.net/file/12345" rel="noreferrer noopener">12345</a>' in body
    assert '<a href="https://anidb.net/anime/99" rel="noreferrer noopener">99</a>' in body
    assert "8 of 8 rows match" in body
    assert f'href="/runs/{RUN_FULL}.csv"' in body
    _, body = pages.run_detail(RUN_FULL, {"status": "reported", "reason": "partial_episode"})
    assert "3 of 8 rows match" in body
    _, body = pages.run_detail(RUN_FULL, {"item": "12345"})
    assert "1 of 8 rows match" in body
    _, body = pages.run_detail(RUN_FULL, {"aid": "300", "rule": "R4"})
    assert "1 of 8 rows match" in body


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
    _, body = pages.runs({})
    assert "unreadable" in body


def test_csv_download(server, data):
    code, headers, body = fetch(f"{server}/runs/{RUN_FULL}.csv")
    assert code == 200
    assert headers["Content-Type"] == "text/csv; charset=utf-8"
    assert headers["Content-Disposition"] == f"attachment; filename=pxar-{RUN_FULL}.csv"
    assert body == (data / "reports" / f"{RUN_FULL}.csv").read_bytes().decode()
    assert fetch(f"{server}/runs/20200101T000000Z-000000.csv")[0] == 404


# --- reported items ---


def test_reported_uses_the_latest_full_run_and_compares_with_the_previous(pages):
    code, body = pages.reported({})
    assert code == 200
    assert f'latest full run, <a href="/runs/{RUN_FULL}">' in body  # not the scoped or snapshot-less run
    assert f'(<a href="/runs/{RUN_OLD}">' in body
    assert body.index("partial_episode") < body.index("no_shoko_match")  # most items first
    assert '<span class="warn">+2</span>' in body  # partial_episode: 1 -> 3
    assert '<span class="ok">-1</span>' in body  # no_shoko_match: 2 -> 1
    assert '<span class="warn">+1</span>' in body  # new reasons
    assert "Only some of the episodes involved are watched in AniDB." in body
    assert "brand_new_reason" in body
    assert '<span class="ok">-2</span>' in body  # fallback_match: 2 -> 0, still listed as fixed
    assert body.index("no_shoko_match") < body.index("fallback_match")  # zero counts last
    assert "<h2>" not in body


def test_reported_lists_one_reason_sorted_by_anime(pages):
    _, body = pages.reported({"reason": "partial_episode"})
    assert "<h2>partial_episode</h2>" in body and "3 items" in body
    assert body.index("GrpA") < body.index("GrpB") < body.index("<td>42</td>")


def test_reported_without_a_previous_full_run(pages, data):
    (data / "reports" / f"{RUN_OLD}.csv").unlink()
    _, body = pages.reported({})
    assert "previous full run." in body and '<span class="warn">' not in body


def test_reported_with_no_full_run(tmp_path):
    write_run(tmp_path / "reports", RUN_SCOPED, [], {"mode": "apply", "full": False, "snapshot": "ok"})
    code, body = Pages(FakeService(), settings_for(tmp_path), clock=lambda: NOW).reported({})
    assert code == 200 and "No complete run report yet." in body


def test_reported_change_marks_unchanged_counts(pages, data):
    write_run(
        data / "reports",
        "20261114T115000Z-eeeeee",
        [reported("no_shoko_match", "2")],
        {"mode": "plan", "full": True, "snapshot": "ok"},
    )
    _, body = pages.reported({})
    assert '<span class="muted">0</span>' in body


# --- ledger ---


def test_ledger_page_lists_writes_with_their_state_change(pages):
    code, body = pages.ledger_page({})
    assert code == 200
    assert "3 rows match" in body
    assert "plex_view_count: 1" in body
    assert "shoko_last_watched_at: None → 2026-11-01T00:00:00+00:00" in body
    assert "Plex returned HTTP 500" in body
    assert '<span class="bad">pending</span>' in body
    assert body.index("<td>3</td>") < body.index("<td>1</td>")  # newest first


@pytest.mark.parametrize(
    ("params", "count"),
    [
        ({"status": "pending"}, 1),
        ({"rule": "R1"}, 2),
        ({"run_id": RUN_FULL}, 2),
        ({"rating_key": "5"}, 1),
        ({"file_id": "7"}, 1),
        ({"file_id": "x7"}, 0),
        ({"status": "applied", "rule": "R1"}, 0),
    ],
)
def test_ledger_filters(pages, params, count):
    _, body = pages.ledger_page(params)
    assert f"{count} rows match" in body


def test_ledger_paginates(pages, monkeypatch):
    monkeypatch.setattr(webui, "PAGE_ROWS", 2)
    _, body = pages.ledger_page({"page": "2"})
    assert "page 2 of 2" in body and "previous" in body


def test_ledger_missing_or_corrupt(tmp_path):
    p = Pages(FakeService(), settings_for(tmp_path), clock=lambda: NOW)
    assert "no ledger yet" in p.ledger_page({})[1]
    (tmp_path / "ledger.sqlite").write_bytes(b"this is not a database at all, not even close" * 40)
    assert "ledger unreadable (DatabaseError)" in p.ledger_page({})[1]
    assert "ledger unreadable" in p.status({})[1]


def test_ledger_connect_failure_is_reported(pages, monkeypatch):
    def refuse(*a, **kw):
        raise webui.sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(webui.sqlite3, "connect", refuse)
    assert "ledger unreadable (OperationalError)" in pages.ledger_page({})[1]


def test_ledger_is_opened_read_only(pages, data):
    before = (data / "ledger.sqlite").read_bytes()
    pages.ledger_page({})
    pages.status({})
    assert (data / "ledger.sqlite").read_bytes() == before
    with pytest.raises(webui.LedgerUnavailable):
        pages.ledger._run(lambda conn: conn.execute("DELETE FROM actions"))


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
    assert Pages._state_diff(before, after) == (f"<pre>{expected}</pre>" if expected else "")


# --- the report cache ---


def test_reports_cache_follows_file_changes_and_pruning(data):
    reports = Reports(data / "reports")
    reports.summaries()
    first = reports.summary(RUN_FULL)
    assert reports.summary(RUN_FULL) is first
    write_run(data / "reports", RUN_FULL, [reported("no_date", "1")], {"mode": "plan", "full": True})
    second = reports.summary(RUN_FULL)
    assert second is not first and second.mode == "plan" and second.reasons == {("R4", "no_date"): 1}
    (data / "reports" / f"{RUN_OLD}.csv").unlink()
    reports.summaries()
    assert RUN_OLD not in reports._cache
    assert reports.summary(RUN_OLD) is None


def test_reports_ignore_foreign_files_and_bad_meta(data):
    reports_dir = data / "reports"
    (reports_dir / "notes.csv").write_text("x", encoding="utf-8")
    (reports_dir / f"{RUN_FULL}.json").write_text("[1, 2]", encoding="utf-8")
    reports = Reports(reports_dir)
    assert "notes" not in reports.run_ids()
    summary = reports.summary(RUN_FULL)
    assert summary.mode == "apply" and summary.full is None and summary.snapshot == ""  # inferred from rows
    (reports_dir / f"{RUN_FULL}.json").write_text("{not json", encoding="utf-8")
    assert reports.meta(RUN_FULL) == {}


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
    ("delta", "text"),
    [
        (timedelta(minutes=5), "5 min"),
        (timedelta(hours=3, minutes=7), "3 h 7 min"),
        (timedelta(days=2, hours=4), "2 d 4 h"),
        (timedelta(minutes=-12), "in 12 min"),
    ],
)
def test_fmt_delta(delta, text):
    assert fmt_delta(delta) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        ("", ""),
        ("garbage", "garbage"),
        ("2026-11-14T07:00:00-05:00", "2026-11-14 12:00 UTC"),
        (NOW, "2026-11-14 12:00 UTC"),
    ],
)
def test_fmt_time(value, text):
    assert fmt_time(value) == text


def test_query_params_and_page_number():
    assert query_params("a=1&a=2&b=+x+&c=&d=" + "z" * 500) == {"a": "1", "b": "x", "d": "z" * 200}
    assert query_params("&".join(f"k{i}=1" for i in range(30))) == {}
    for raw, page in (("3", 3), ("0", 1), ("-1", 1), ("x", 1), ("٣", 1), ("100000", 1)):
        assert page_number({"page": raw}) == page
    assert page_number({}) == 1


def test_anidb_link_only_for_numeric_ids():
    assert webui.anidb_link("anime", "") == ""
    assert webui.anidb_link("anime", "<b>") == "&lt;b&gt;"


def test_run_link_escapes_odd_values(pages):
    assert pages._run_link("<x>") == "&lt;x&gt;"
    assert pages._run_link(None) == ""


def test_badge_classes():
    assert webui.badge("error:ShokoError") == '<span class="bad">error:ShokoError</span>'
    assert webui.badge("skipped") == '<span class="warn">skipped</span>'
    assert webui.badge("reported") == "reported"
    assert webui.badge(None) == ""


# --- serve wiring ---


def free_port():
    with webui.socket.socket() as sock:
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


def test_ui_server_binds_ipv6(pages):
    try:
        srv = make_ui_server(pages, TOKEN, "::1", 0)
    except OSError:
        pytest.skip("no IPv6 loopback")
    try:
        assert srv.address_family == webui.socket.AF_INET6
    finally:
        srv.server_close()
