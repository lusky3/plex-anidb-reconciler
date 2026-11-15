"""End-to-end real-world scenarios for plex-anidb-reconciler, driven through runner.run_once with a
stateful respx-mocked Shoko + Plex (writes mutate the fake world, so consecutive runs see them)."""

import csv
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from plex_anidb_reconciler.config import Settings
from plex_anidb_reconciler.runner import run_once

SHOKO = "http://shoko:8111"
PLEX = "http://plex:32400"
NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[1]


def ts(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp())


class World:
    """Fake Shoko + Plex. Writes are recorded and applied to state."""

    def __init__(self):
        self.files: dict[int, dict] = {}  # Shoko file id -> raw v3 json (index)
        self.detail_override: dict[int, dict] = {}  # what GET /File/{id} returns (simulate upgrade)
        self.stats: dict[int, dict] = {}  # file id -> UserStats body
        self.items: dict[str, dict] = {}  # Plex ratingKey -> metadata
        self.history: list[dict] = []
        self.puts: list[tuple[int, dict]] = []
        self.scrobbles: list[str] = []
        self.other_writes: list[str] = []
        self.clock = NOW

    def add_file(self, fid_shoko, fid, eid, path, created="2025-01-01T00:00:00+00:00"):
        self.files[fid_shoko] = {
            "ID": fid_shoko,
            "AniDB": {"ID": fid, "ReleaseGroup": {"ShortName": f"G{fid}"}},
            "Created": created,
            "Locations": [{"AbsolutePath": path}],
            "SeriesIDs": [{"SeriesID": {"ID": 1, "AniDB": 700}, "EpisodeIDs": [{"AniDB": eid, "Source": "AniDB"}]}],
        }

    def add_item(self, rk, paths, views=0, last=None):
        m = {"ratingKey": rk, "viewCount": views, "Media": [{"Part": [{"file": p} for p in paths]}]}
        if last:
            m["lastViewedAt"] = ts(last)
        self.items[rk] = m

    def add_play(self, rk, when):
        self.history.append({"historyKey": f"h{len(self.history)}", "ratingKey": rk, "viewedAt": ts(when)})

    # --- handlers ---
    def install(self, router: respx.Router):
        router.get(f"{SHOKO}/api/v3/File").mock(
            side_effect=lambda r: httpx.Response(200, json={"List": list(self.files.values())})
        )
        router.get(url__regex=rf"{SHOKO}/api/v3/File/(?P<i>\d+)/UserStats$").mock(side_effect=self._get_stats)
        router.put(url__regex=rf"{SHOKO}/api/v3/File/(?P<i>\d+)/UserStats$").mock(side_effect=self._put_stats)
        router.get(url__regex=rf"{SHOKO}/api/v3/File/(?P<i>\d+)(\?.*)?$").mock(side_effect=self._get_file)
        router.get(url__regex=rf"{PLEX}/library/sections/4/all.*").mock(
            side_effect=lambda r: httpx.Response(200, json={"MediaContainer": {"Metadata": list(self.items.values())}})
        )
        router.get(url__regex=rf"{PLEX}/status/sessions/history/all.*").mock(
            side_effect=lambda r: httpx.Response(200, json={"MediaContainer": {"Metadata": list(self.history)}})
        )
        router.get(url__regex=rf"{PLEX}/:/scrobble.*").mock(side_effect=self._scrobble)
        # anything else that is not GET is an unexpected write
        router.route(method__in=["POST", "PUT", "DELETE", "PATCH"]).mock(side_effect=self._other)
        router.route().mock(side_effect=self._other)

    def _other(self, request):
        self.other_writes.append(f"{request.method} {request.url}")
        return httpx.Response(500)

    def _get_stats(self, request, i):
        i = int(i)
        if i in self.stats:
            return httpx.Response(200, json=self.stats[i])
        return httpx.Response(404, text="No FileUserStats entry for the given fileID for the current user")

    def _put_stats(self, request, i):
        body = json.loads(request.content)
        self.puts.append((int(i), body))
        self.stats[int(i)] = {"WatchedCount": 1, "LastWatchedAt": body["LastWatchedAt"], "ResumePosition": None}
        return httpx.Response(200)

    def _get_file(self, request, i):
        i = int(i)
        f = self.detail_override.get(i) or self.files.get(i)
        return httpx.Response(200, json=f) if f else httpx.Response(404)

    def _scrobble(self, request):
        rk = request.url.params["key"]
        self.scrobbles.append(rk)
        m = self.items[rk]
        m["viewCount"] = int(m.get("viewCount") or 0) + 1
        m["lastViewedAt"] = int(self.clock.timestamp())  # Plex: scrobble sets lastViewedAt, no history row
        return httpx.Response(200)


@pytest.fixture
def ctx(tmp_path):
    world = World()
    mylist = tmp_path / "mylist.json"

    def write_mylist(entries, age=timedelta(0)):
        mylist.write_text(json.dumps(entries))
        t = (NOW - age).timestamp()
        os.utime(mylist, (t, t))

    env = {
        "SHOKO_URL": SHOKO,
        "SHOKO_API_KEY": "k",
        "PLEX_URL": PLEX,
        "PLEX_TOKEN": "t",
        "PLEX_SECTIONS": "4",
        "MYLIST_PATH": str(mylist),
        "DATA_DIR": str(tmp_path / "data"),
        "PATH_MAP": "/plex=/shoko",
        "SHOKO_INDEX_TTL_MIN": "0",
    }

    def run(apply=True, extra=None, at=NOW, **kw):
        settings = Settings.from_env({**env, **(extra or {})})
        world.clock = at
        with respx.mock(assert_all_called=False) as router:
            world.install(router)
            res = run_once(settings, apply=apply, limit=None, now=at, **kw)
        with open(res.report_path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        return res, rows

    run.env = env
    return world, write_mylist, run


def ml(fid, eid, viewed=None):
    return {"FileID": fid, "EpisodeID": eid, "AnimeID": 700, "State": 1, "ViewedAt": viewed}


def reasons(rows):
    return {(r["rule"], r["status"], r["reason"], r["detail"]) for r in rows}


# 1 -----------------------------------------------------------------------------------------------
def test_s1_new_watch(ctx):
    w, write_ml, run = ctx
    write_ml([ml(12345, 50)])
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv")
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=1, last="2026-02-10T20:30:00+00:00")
    w.add_play("rk1", "2026-02-10T20:00:00+00:00")
    _, rows = run()
    assert len(w.puts) == 1
    fid, body = w.puts[0]
    assert fid == 1 and datetime.fromisoformat(body["LastWatchedAt"]) == datetime(2026, 2, 10, 20, tzinfo=UTC)
    assert ("R2", "applied", "new_watch", "") in reasons(rows)
    _, rows2 = run(at=NOW + timedelta(minutes=15))
    assert len(w.puts) == 1 and not w.scrobbles and not w.other_writes
    # ledger pending watch makes fid 12345 count as watched -> nothing planned at all
    assert [r for r in rows2 if r["kind"] != "report"] == []


# 2 -----------------------------------------------------------------------------------------------
def test_s2_headline_anidb_watched_plex_wiped_manual_mark(ctx):
    w, write_ml, run = ctx
    write_ml([ml(12345, 50, "2025-05-01T21:00:00+00:00")])
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv")
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=1, last="2026-02-20T10:00:00+00:00")  # manual mark, no history
    for i in range(2):
        run(at=NOW + timedelta(minutes=15 * i))
    assert w.puts == [] and w.scrobbles == [] and w.other_writes == []


def test_s2b_headline_but_shoko_file_differs_and_plex_play_absent(ctx):
    """Variant: AniDB watched via the same episode but older fid; manual mark (no history) -> still nothing."""
    w, write_ml, run = ctx
    write_ml([ml(999, 50, "2025-05-01T21:00:00+00:00"), ml(12345, 50)])
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv", created="2025-12-01T00:00:00+00:00")
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=1, last="2026-02-20T10:00:00+00:00")
    run()
    assert w.puts == [] and w.scrobbles == []


# 3 -----------------------------------------------------------------------------------------------
def test_s3_old_release_watched_new_release_r1_then_no_write(ctx):
    w, write_ml, run = ctx
    write_ml([ml(999, 50, "2023-04-01T21:00:00+00:00"), ml(12345, 50)])
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv", created="2025-01-01T00:00:00+00:00")
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=0)
    _, rows = run()
    assert w.scrobbles == ["rk1"] and w.puts == []
    assert ("R1", "applied", "anidb_episode_watched", "") in reasons(rows)
    # next runs: Plex now watched, lastViewedAt = scrobble time, no history row
    for i in range(1, 3):
        _, rows = run(at=NOW + timedelta(minutes=15 * i))
    assert w.puts == [] and w.scrobbles == ["rk1"] and not w.other_writes, rows


def test_s3b_ledger_lost_after_r1(ctx):
    """Even if the ledger is lost (no r1_marked memory), a scrobbled item with no history must not be dated."""
    w, write_ml, run = ctx
    write_ml([ml(999, 50, "2023-04-01T21:00:00+00:00"), ml(12345, 50)])
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv", created="2025-01-01T00:00:00+00:00")
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=0)
    run()
    assert w.scrobbles == ["rk1"]
    (Path(run.env["DATA_DIR"]) / "ledger.sqlite").unlink()
    run(at=NOW + timedelta(minutes=15))
    assert w.puts == []


# 4 -----------------------------------------------------------------------------------------------
def _remaster(w, write_ml, play):
    write_ml([ml(999, 50, "2023-04-01T21:00:00+00:00"), ml(12345, 50)])
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv", created="2026-01-15T00:00:00+00:00")
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=1, last=play)
    w.add_play("rk1", play)


def test_s4_remaster_rewatch_record(ctx):
    w, write_ml, run = ctx
    _remaster(w, write_ml, "2026-02-05T20:00:00+00:00")
    _, rows = run()
    assert len(w.puts) == 1 and w.puts[0][0] == 1
    assert datetime.fromisoformat(w.puts[0][1]["LastWatchedAt"]) == datetime(2026, 2, 5, 20, tzinfo=UTC)
    assert ("R2b", "applied", "rewatch_new_release", "") in reasons(rows)
    run(at=NOW + timedelta(minutes=15))
    assert len(w.puts) == 1


def test_s4_remaster_rewatch_ignore(ctx):
    w, write_ml, run = ctx
    _remaster(w, write_ml, "2026-02-05T20:00:00+00:00")
    _, rows = run(extra={"R2B_POLICY": "ignore"})
    assert w.puts == []
    assert ("R2b", "reported", "ignore_policy", "") in reasons(rows)


def test_s4_remaster_play_before_created(ctx):
    w, write_ml, run = ctx
    _remaster(w, write_ml, "2026-01-10T20:00:00+00:00")  # before Created 2026-01-15
    _, rows = run()
    assert w.puts == []
    assert ("R2b", "reported", "no_playback", "") in reasons(rows)


# 5 -----------------------------------------------------------------------------------------------
def test_s5_shoko_already_watched(ctx):
    w, write_ml, run = ctx
    write_ml([ml(12345, 50)])  # snapshot says unwatched (stale view of AniDB)
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv")
    w.stats[1] = {"WatchedCount": 1, "LastWatchedAt": "2025-06-01T10:00:00+00:00", "ResumePosition": None}
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=1, last="2026-02-10T20:30:00+00:00")
    w.add_play("rk1", "2026-02-10T20:00:00+00:00")
    _, rows = run()
    assert w.puts == []
    assert ("R2", "skipped", "new_watch", "shoko_already_watched") in reasons(rows)


# 6 -----------------------------------------------------------------------------------------------
def test_s6_stale_snapshot(ctx):
    w, write_ml, run = ctx
    write_ml(
        [
            ml(999, 50, "2023-04-01T21:00:00+00:00"),
            ml(12345, 51),
            ml(888, 52, "2023-04-01T21:00:00+00:00"),
            ml(777, 52),
        ],
        age=timedelta(hours=30),
    )
    w.add_file(1, 999, 50, "/shoko/a/e1.mkv")  # R1 candidate
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=0)
    w.add_file(2, 12345, 51, "/shoko/a/e2.mkv")  # R2 candidate
    w.add_item("rk2", ["/plex/a/e2.mkv"], views=1, last="2026-02-10T20:30:00+00:00")
    w.add_play("rk2", "2026-02-10T20:00:00+00:00")
    w.add_file(3, 777, 52, "/shoko/a/e3.mkv", created="2026-01-01T00:00:00+00:00")  # R2b candidate
    w.add_item("rk3", ["/plex/a/e3.mkv"], views=1, last="2026-02-10T20:30:00+00:00")
    w.add_play("rk3", "2026-02-10T20:00:00+00:00")
    res, rows = run()
    assert w.scrobbles == ["rk1"] and w.puts == []
    assert res.snapshot == "stale"
    assert ("R4", "reported", "stale_snapshot", "") in reasons(rows)
    assert ("R2b", "reported", "stale_snapshot", "") in reasons(rows)


# 7 -----------------------------------------------------------------------------------------------
def test_s7_fallback_match_only(ctx):
    w, write_ml, run = ctx
    write_ml([ml(999, 50, "2023-04-01T21:00:00+00:00"), ml(12345, 51)])
    w.add_file(1, 999, 50, "/elsewhere/Show/e1.mkv")
    w.add_file(2, 12345, 51, "/elsewhere/Show/e2.mkv")
    w.add_item("rk1", ["/media/Show/e1.mkv"], views=0)
    w.add_item("rk2", ["/media/Show/e2.mkv"], views=1, last="2026-02-10T20:30:00+00:00")
    w.add_play("rk2", "2026-02-10T20:00:00+00:00")
    _, rows = run(extra={"PATH_MAP": ""})
    assert w.puts == [] and w.scrobbles == []
    fb = [r for r in rows if r["reason"] == "fallback_match"]
    assert {r["rating_key"] for r in fb} == {"rk1", "rk2"}


# 8 -----------------------------------------------------------------------------------------------
def test_s8_multi_version(ctx):
    w, write_ml, run = ctx
    write_ml([ml(111, 50), ml(222, 50)])
    w.add_file(1, 111, 50, "/shoko/a/e1-v1.mkv")
    w.add_file(2, 222, 50, "/shoko/a/e1-v2.mkv")
    w.add_item("rk1", ["/plex/a/e1-v1.mkv", "/plex/a/e1-v2.mkv"], views=1, last="2026-02-10T20:30:00+00:00")
    w.add_play("rk1", "2026-02-10T20:00:00+00:00")
    _, rows = run()
    assert w.puts == []
    assert {r["reason"] for r in rows} == {"multi_version"}


# 9 -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("change", ["fid", "path"])
def test_s9_upgrade_between_plan_and_write(ctx, change):
    w, write_ml, run = ctx
    write_ml([ml(12345, 50)])
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv")
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=1, last="2026-02-10T20:30:00+00:00")
    w.add_play("rk1", "2026-02-10T20:00:00+00:00")
    upgraded = json.loads(json.dumps(w.files[1]))
    if change == "fid":
        upgraded["AniDB"]["ID"] = 54321
    else:
        upgraded["Locations"] = [{"AbsolutePath": "/shoko/a/e1-v2.mkv"}]
    w.detail_override[1] = upgraded
    _, rows = run()
    assert w.puts == []
    assert ("R2", "failed", "new_watch", "shoko_changed") in reasons(rows)


# 10 ----------------------------------------------------------------------------------------------
def _two_items(w, write_ml):
    write_ml([ml(999, 50, "2023-04-01T21:00:00+00:00"), ml(1001, 50), ml(12345, 51), ml(23456, 52)])
    w.add_file(1, 1001, 50, "/shoko/a/e1.mkv")
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=0)  # R1
    w.add_file(2, 12345, 51, "/shoko/a/e2.mkv")
    w.add_item("rk2", ["/plex/a/e2.mkv"], views=1, last="2026-02-10T20:30:00+00:00")
    w.add_play("rk2", "2026-02-10T20:00:00+00:00")  # R2
    w.add_file(3, 23456, 52, "/shoko/a/e3.mkv")
    w.add_item("rk3", ["/plex/a/e3.mkv"], views=1, last="2026-02-11T20:30:00+00:00")
    w.add_play("rk3", "2026-02-11T20:00:00+00:00")  # R2


def _decision(rows):
    return sorted((r["rule"], r["kind"], r["rating_key"], r["file_id"], r["when"], r["reason"]) for r in rows)


def test_s10_triggered_matches_full(ctx):
    w, write_ml, run = ctx
    _two_items(w, write_ml)
    _, plan_rows = run(apply=False)
    full_rk2 = [r for r in plan_rows if r["rating_key"] == "rk2"]
    _, rows = run(rating_keys=frozenset({"rk2"}))
    assert [p[0] for p in w.puts] == [2] and w.scrobbles == []
    assert {r["rating_key"] for r in rows} == {"rk2"}
    assert _decision(rows) == _decision(full_rk2)


# 11 ----------------------------------------------------------------------------------------------
def test_s11_dry_run(ctx):
    w, write_ml, run = ctx
    _two_items(w, write_ml)
    for i in range(2):
        _, rows = run(apply=False, at=NOW + timedelta(minutes=i))
    assert w.puts == [] and w.scrobbles == [] and w.other_writes == []
    assert {r["status"] for r in rows if r["kind"] != "report"} == {"dry_run"}


# 12 ----------------------------------------------------------------------------------------------
def test_s12_cap(ctx):
    w, write_ml, run = ctx
    write_ml([ml(100 + i, 60 + i) for i in range(3)])
    for i in range(3):
        w.add_file(i + 1, 100 + i, 60 + i, f"/shoko/a/e{i}.mkv")
        w.add_item(f"rk{i}", [f"/plex/a/e{i}.mkv"], views=1, last="2026-02-10T20:30:00+00:00")
        w.add_play(f"rk{i}", f"2026-02-1{i}T20:00:00+00:00")
    _, rows = run(extra={"MAX_WRITES": "1"})
    assert len(w.puts) == 1
    assert sorted(r["status"] for r in rows) == ["applied", "capped", "capped"]
    run(extra={"MAX_WRITES": "1"}, at=NOW + timedelta(minutes=15))
    assert len(w.puts) == 2
    run(extra={"MAX_WRITES": "1"}, at=NOW + timedelta(minutes=30))
    assert len(w.puts) == 3
    _, rows = run(extra={"MAX_WRITES": "1"}, at=NOW + timedelta(minutes=45))
    assert len(w.puts) == 3 and {p[0] for p in w.puts} == {1, 2, 3}


# 13 ----------------------------------------------------------------------------------------------
def test_s13_never_unwatch_runtime(ctx):
    """AniDB unwatched but Plex watched with no date / Shoko watched but Plex unwatched / AniDB watched
    but Shoko unwatched and Plex unwatched-after-play: no unscrobble, no DELETE, no PUT with 0/None."""
    w, write_ml, run = ctx
    write_ml([ml(12345, 50), ml(222, 51, "2024-01-01T00:00:00+00:00"), ml(333, 52)])
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv")
    w.stats[1] = {"WatchedCount": 1, "LastWatchedAt": "2025-06-01T10:00:00+00:00", "ResumePosition": None}
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=0)  # Plex unwatched, Shoko watched, AniDB unwatched
    w.add_file(2, 222, 51, "/shoko/a/e2.mkv")
    w.add_item("rk2", ["/plex/a/e2.mkv"], views=1, last="2026-02-01T00:00:00+00:00")
    w.add_file(3, 333, 52, "/shoko/a/e3.mkv")
    w.add_item("rk3", ["/plex/a/e3.mkv"], views=0)  # unwatched everywhere
    for i in range(2):
        run(at=NOW + timedelta(minutes=15 * i))
    assert w.other_writes == []
    assert all(b["WatchedCount"] >= 1 and b["LastWatchedAt"] for _, b in w.puts)
    assert w.scrobbles == []


def _src_lines():
    for path in sorted((REPO / "src").rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            yield f"{path.name}:{n}: {line}", line


UNWATCH = re.compile(
    r"unscrobble|viewed=0|\"viewed\"|/Watched|watched/false|watched=false|\.delete\(|\"DELETE\"|WatchedCount\": 0",
    re.IGNORECASE,
)
WRITE = re.compile(r"\"PUT\"|\"POST\"|scrobble")


def test_s13_never_unwatch_static():
    assert [where for where, line in _src_lines() if UNWATCH.search(line)] == []
    # only writes: Shoko PUT UserStats and Plex /:/scrobble (comments and docstrings aside)
    writes = [where for where, line in _src_lines() if WRITE.search(line) and not re.search(r"#|\"\"\"", line)]
    assert all("UserStats" in w or "scrobble" in w for w in writes), writes


def test_s14_r1_marked_item_never_dates_anidb_with_the_scrobble(ctx):
    """R1 marks the item played; later the old release's watched entry leaves MyList, so the episode reads as
    unwatched again. Plex's only date is the reconciler's own scrobble (lastViewedAt) and there is no history
    row: the r1_marked guard reports plex_marked_by_reconciler and nothing reaches AniDB."""
    w, write_ml, run = ctx
    write_ml([ml(999, 50, "2023-04-01T21:00:00+00:00"), ml(12345, 50)])
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv")
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=0)
    run()
    assert w.scrobbles == ["rk1"]
    assert w.items["rk1"]["lastViewedAt"] == int(NOW.timestamp()) and w.history == []
    write_ml([ml(12345, 50)])  # episode 50 no longer watched in the snapshot
    _, rows = run(at=NOW + timedelta(minutes=15))
    assert w.puts == [] and w.scrobbles == ["rk1"] and not w.other_writes
    assert reasons(rows) == {("R4", "reported", "plex_marked_by_reconciler", "")}


def test_s15_ledger_lost_and_file_reimported_after_r1_scrobble(ctx):
    """R1 marks the item; the ledger is lost; the old release's watched entry leaves MyList; Shoko re-imports the
    file after the scrobble. With no history row the only date is the scrobble, which predates the file's import:
    report predates_file, never a SET."""
    w, write_ml, run = ctx
    write_ml([ml(999, 50, "2023-04-01T21:00:00+00:00"), ml(12345, 50)])
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv")
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=0)
    run()
    assert w.scrobbles == ["rk1"]
    (Path(run.env["DATA_DIR"]) / "ledger.sqlite").unlink()
    write_ml([ml(12345, 50)])
    del w.files[1]
    w.add_file(2, 12345, 50, "/shoko/a/e1.mkv", created=(NOW + timedelta(minutes=5)).isoformat())
    _, rows = run(at=NOW + timedelta(minutes=15))
    assert w.puts == [] and w.scrobbles == ["rk1"] and not w.other_writes
    assert reasons(rows) == {("R4", "reported", "predates_file", "")}


def test_s2c_same_file_already_watched_real_replay_2026(ctx):
    """Same fid watched on AniDB 2025, Plex wiped, user really re-plays it in 2026 (history row) -> no write."""
    w, write_ml, run = ctx
    write_ml([ml(12345, 50, "2025-05-01T21:00:00+00:00")])
    w.add_file(1, 12345, 50, "/shoko/a/e1.mkv")
    w.add_item("rk1", ["/plex/a/e1.mkv"], views=1, last="2026-02-20T10:30:00+00:00")
    w.add_play("rk1", "2026-02-20T10:00:00+00:00")
    _, rows = run()
    assert w.puts == [] and w.scrobbles == [] and rows == []
