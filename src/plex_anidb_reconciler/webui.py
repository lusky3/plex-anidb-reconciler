"""Read-only web UI for ``serve``: status, runs, reported items and the ledger.

The UI answers GET and HEAD only. Its pages read the run reports and meta files under DATA_DIR/reports, the
ledger, and the service's in-process state. The ledger is opened read-only on a separate connection, never through
``Ledger``, because that class deletes rows on open. Nothing here calls Plex, Shoko or AniDB, and nothing here
writes. Every request needs UI_TOKEN, sent as the HTTP Basic password or as a Bearer token. Pages contain no
scripts, and their CSP blocks scripts as well.
"""

import base64
import csv
import hmac
import html
import json
import logging
import re
import socket
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from plex_anidb_reconciler.config import Settings
from plex_anidb_reconciler.mylist import SnapshotError, snapshot_age, snapshot_is_fresh
from plex_anidb_reconciler.report import COLUMNS

log = logging.getLogger(__name__)

RUN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")
PAGE_ROWS = 200
MAX_PARAM = 200  # longer query values are cut; every filter is an exact match on a short value
RECENT_RUNS = 10

# What a reason means and what to do about it (docs/how-it-works.md, "Reason codes").
REASONS: dict[str, tuple[str, str]] = {
    "no_shoko_match": ("No Shoko file at the rewritten path.", "A handful is normal; many mean PATH_MAP is wrong."),
    "ambiguous_path": ("Several Shoko files share the path or fallback key.", "Remove the duplicates in Shoko."),
    "fallback_match": ("Matched only by folder and filename.", "Fix PATH_MAP; this should be zero."),
    "unrecognised": ("Shoko has no AniDB file id for the file.", "Out of scope; Shoko has to recognise it first."),
    "manual_link": ("The file is linked to its episode by hand in Shoko.", "Out of scope; never written."),
    "no_episodes": ("Shoko links the file to no AniDB episode.", "Check the file in Shoko."),
    "eid_mismatch": ("AniDB and Shoko link the file to different episodes.", "Fix the link in Shoko or on AniDB."),
    "partial_episode": ("Only some of the episodes involved are watched in AniDB.", "Nothing; the tool won't guess."),
    "multi_version": (
        "A watched Plex item with several distinct files; Plex can't say which one was played.",
        "Split or remove versions in Plex if you want it written.",
    ),
    "not_in_snapshot": ("The file isn't in MyList.", "Check AniDb.MyList_AddFiles, then sync MyList."),
    "no_date": ("Watched in Plex with no play history and no lastViewedAt.", "Nothing usable."),
    "plex_marked_by_reconciler": ("The only date is this tool's own R1 scrobble.", "Nothing; that date isn't a watch."),
    "stale_snapshot": ("mylist.json is older than SNAPSHOT_MAX_AGE_H.", "Run Shoko's MyList sync."),
    "predates_file": (
        "No play history, and Plex's lastViewedAt is older than the file's Shoko import.",
        "Nothing; that date belongs to an earlier file or mark.",
    ),
    "unknown_file_age": (
        "Shoko has no import time for the file, and the date would need one.",
        "Nothing; the file can't be dated safely.",
    ),
    "ignore_policy": ("R2B_POLICY=ignore.", "Nothing."),
    "no_playback": ("No play after the AniDB date and the file's import.", "Nothing."),
    "multiple_prerequisites": ("Would need several R2s of this run to land first.", "Nothing; the next run decides."),
}

_STYLE = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#deded8;--head:#f0f0ec;--accent:#2f5d8a;
--ok:#2e7d4f;--warn:#a66a00;--bad:#b3261e;--chip:#ecece6}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#e8e8e3;--muted:#9a9a93;--line:#34342f;
--head:#1f1f1d;--accent:#8db4dc;--ok:#6cc08f;--warn:#e0a84a;--bad:#f08a80;--chip:#2a2a27}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;flex-wrap:wrap;gap:.25rem 1.25rem;align-items:baseline;padding:.8rem 16px;
border-bottom:1px solid var(--line)}
header strong{font-size:1.05rem;letter-spacing:.02em}
nav a{margin-right:1rem}
main{padding:0 16px 2rem;max-width:1400px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:1.3rem;margin:1.2rem 0 .4rem}h2{font-size:1.05rem;margin:1.6rem 0 .4rem}
p.note,.muted{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(16rem,1fr));gap:.8rem}
.card{border:1px solid var(--line);border-radius:6px;padding:.7rem .9rem}
.card h2{margin:0 0 .4rem;font-size:.95rem}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.15rem .9rem;margin:0}
dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;font-size:.85rem;width:100%}
th,td{padding:.3rem .55rem;text-align:left;white-space:nowrap;border-bottom:1px solid var(--line);
vertical-align:top}
th{background:var(--head);position:sticky;top:0}
td.wide{white-space:normal;min-width:16rem}
tr:last-child td{border-bottom:0}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.chip{display:inline-block;background:var(--chip);border-radius:3px;padding:0 .35rem;margin:0 .2rem .2rem 0}
form.filters{display:flex;flex-wrap:wrap;gap:.5rem;align-items:end;margin:.6rem 0}
form.filters label{display:flex;flex-direction:column;font-size:.8rem;color:var(--muted)}
input,select,button{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);
border-radius:4px;padding:.25rem .4rem}
button{cursor:pointer}
.pager{margin:.6rem 0;display:flex;gap:1rem}
pre{margin:0;font-size:.8rem;white-space:pre-wrap}
"""

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def run_started(run_id: str) -> datetime:
    return datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def fmt_time(value: datetime | str | None) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def fmt_delta(delta: timedelta) -> str:
    seconds = int(abs(delta).total_seconds())
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        text = f"{days} d {hours} h"
    elif hours:
        text = f"{hours} h {minutes} min"
    else:
        text = f"{minutes} min"
    return text if delta >= timedelta(0) else f"in {text}"


# --- run reports -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    mode: str  # apply | plan | ? (an old report without a meta file and no telling rows)
    full: bool | None  # True: the report lists every item; False: a scoped /trigger run; None: unknown
    snapshot: str  # ok | stale | unavailable | "" (unknown)
    statuses: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)  # (rule, reason) of reported rows
    rows: int = 0
    unreadable: bool = False

    @property
    def started(self) -> datetime:
        return run_started(self.run_id)

    @property
    def scope(self) -> str:
        return {True: "full", False: "scoped", None: "?"}[self.full]


def _infer_mode(statuses: Counter) -> str:
    if statuses.get("dry_run"):
        return "plan"
    if statuses.get("applied") or statuses.get("failed") or statuses.get("capped"):
        return "apply"
    return "?"


class Reports:
    """The run reports under one directory. Summaries are cached per file version (size and mtime)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._cache: dict[str, tuple[tuple, RunSummary]] = {}
        self._lock = threading.Lock()

    def run_ids(self) -> list[str]:
        """Run ids with a CSV report, newest first."""
        try:
            names = [p.stem for p in self.directory.glob("*.csv") if RUN_ID.fullmatch(p.stem)]
        except OSError:
            return []
        return sorted(names, reverse=True)

    def csv_path(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.csv"

    def meta(self, run_id: str) -> dict:
        try:
            data = json.loads((self.directory / f"{run_id}.json").read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def rows(self, run_id: str) -> list[dict[str, str]] | None:
        """The CSV rows of a run, or None if the report can't be read."""
        try:
            with self.csv_path(run_id).open(newline="", encoding="utf-8") as fh:
                return [{c: (row.get(c) or "") for c in COLUMNS} for row in csv.DictReader(fh)]
        except OSError, csv.Error, UnicodeDecodeError:
            return None

    def _version(self, run_id: str) -> tuple | None:
        try:
            st = self.csv_path(run_id).stat()
        except OSError:
            return None
        try:
            meta_mtime = (self.directory / f"{run_id}.json").stat().st_mtime_ns
        except OSError:
            meta_mtime = None
        return (st.st_size, st.st_mtime_ns, meta_mtime)

    def summary(self, run_id: str) -> RunSummary | None:
        version = self._version(run_id)
        if version is None:
            return None
        with self._lock:
            cached = self._cache.get(run_id)
        if cached and cached[0] == version:
            return cached[1]
        summary = self._summarize(run_id)
        with self._lock:
            self._cache[run_id] = (version, summary)
        return summary

    def _summarize(self, run_id: str) -> RunSummary:
        meta = self.meta(run_id)
        rows = self.rows(run_id)
        statuses: Counter = Counter(r["status"] for r in rows or ())
        reasons: Counter = Counter((r["rule"], r["reason"]) for r in rows or () if r["status"] == "reported")
        full = meta.get("full")
        return RunSummary(
            run_id=run_id,
            mode=meta.get("mode") if meta.get("mode") in ("apply", "plan") else _infer_mode(statuses),
            full=full if isinstance(full, bool) else None,
            snapshot=str(meta.get("snapshot") or ""),
            statuses=statuses,
            reasons=reasons,
            rows=len(rows or ()),
            unreadable=rows is None,
        )

    def summaries(self, limit: int | None = None) -> list[RunSummary]:
        all_ids = self.run_ids()
        with self._lock:
            for gone in set(self._cache) - set(all_ids):  # pruned reports
                del self._cache[gone]
        ids = all_ids[:limit] if limit else all_ids
        return [s for s in (self.summary(i) for i in ids) if s is not None]

    def full_runs(self) -> list[RunSummary]:
        """Readable runs that list every item and had a snapshot, newest first. Runs from before meta files
        existed count as full, since nothing says otherwise."""
        return [s for s in self.summaries() if s.full is not False and s.snapshot != "unavailable" and not s.unreadable]


# --- ledger ------------------------------------------------------------------------------------------------------

LEDGER_COLUMNS = (
    "id",
    "run_id",
    "rule",
    "kind",
    "rating_key",
    "file_id",
    "fid",
    "eids",
    "when_ts",
    "status",
    "detail",
    "created_at",
    "before",
    "after",
)
# Static SQL only: each filter is a bound parameter that is NULL when unused.
_LEDGER_WHERE = (
    " WHERE (?1 IS NULL OR run_id = ?1) AND (?2 IS NULL OR rule = ?2) AND (?3 IS NULL OR status = ?3)"
    " AND (?4 IS NULL OR rating_key = ?4) AND (?5 IS NULL OR file_id = ?5)"
)
_LEDGER_SELECT = "SELECT " + ", ".join(LEDGER_COLUMNS) + " FROM actions" + _LEDGER_WHERE + " ORDER BY id DESC"
_LEDGER_PAGE = _LEDGER_SELECT + " LIMIT ?6 OFFSET ?7"
_LEDGER_COUNT = "SELECT COUNT(*) FROM actions" + _LEDGER_WHERE
_LEDGER_STATUS_COUNTS = "SELECT status, COUNT(*) FROM actions GROUP BY status"
LEDGER_FILTERS = ("run_id", "rule", "status", "rating_key", "file_id")


class LedgerUnavailable(Exception):
    """The ledger file is missing or can't be read."""


class LedgerView:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise LedgerUnavailable("no ledger yet: it appears after the first applied write")
        try:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error as exc:
            raise LedgerUnavailable(f"ledger unreadable ({type(exc).__name__})") from None
        return conn

    def _run(self, fn: Callable[[sqlite3.Connection], object]) -> object:
        conn = self._connect()
        try:
            return fn(conn)
        except sqlite3.Error as exc:
            raise LedgerUnavailable(f"ledger unreadable ({type(exc).__name__})") from None
        finally:
            conn.close()

    def status_counts(self) -> dict[str, int]:
        return dict(self._run(lambda c: c.execute(_LEDGER_STATUS_COUNTS).fetchall()))  # type: ignore[arg-type]

    def query(self, filters: Mapping[str, object], offset: int, limit: int = PAGE_ROWS) -> tuple[list[dict], int]:
        params = tuple(filters.get(name) for name in LEDGER_FILTERS)

        def fetch(conn: sqlite3.Connection) -> tuple[list[dict], int]:
            rows = conn.execute(_LEDGER_PAGE, (*params, limit, offset)).fetchall()
            total = conn.execute(_LEDGER_COUNT, params).fetchone()[0]
            return [dict(zip(LEDGER_COLUMNS, row, strict=True)) for row in rows], total

        return self._run(fetch)  # type: ignore[return-value]


# --- request helpers -------------------------------------------------------------------------------------------


def authorized(header: str | None, token: str) -> bool:
    """True iff the Authorization header carries ``token`` as a Bearer token or as the Basic password."""
    if not header or not token:
        return False
    scheme, _, value = header.strip().partition(" ")
    scheme, value = scheme.lower(), value.strip()
    if scheme == "bearer":
        supplied = value
    elif scheme == "basic":
        try:
            supplied = base64.b64decode(value, validate=True).decode("utf-8").partition(":")[2]
        except ValueError:  # bad base64 (binascii.Error) or bad UTF-8 (UnicodeDecodeError)
            return False
    else:
        return False
    return hmac.compare_digest(supplied.encode(), token.encode())


def query_params(raw_query: str) -> dict[str, str]:
    """The first value of each parameter, trimmed and cut to MAX_PARAM characters."""
    try:
        parsed = parse_qs(raw_query, max_num_fields=20)
    except ValueError:
        return {}
    return {k: v[0].strip()[:MAX_PARAM] for k, v in parsed.items() if v and v[0].strip()}


def page_number(params: Mapping[str, str]) -> int:
    raw = params.get("page", "1")
    return int(raw) if raw.isascii() and raw.isdigit() and 0 < int(raw) < 100_000 else 1


def link(path: str, params: Mapping[str, object] | None = None) -> str:
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    return esc(path + ("?" + urlencode(clean) if clean else ""))


def anidb_link(kind: str, value: str) -> str:
    if not (value.isascii() and value.isdigit()):
        return esc(value)
    return f'<a href="https://anidb.net/{kind}/{value}" rel="noreferrer noopener">{esc(value)}</a>'


def status_class(status: str) -> str:
    if status in ("applied", "ok", "fresh"):
        return "ok"
    if status in ("failed", "stale", "unavailable", "pending") or status.startswith("error"):
        return "bad"
    if status in ("capped", "skipped", "skipped_ledger"):
        return "warn"
    return ""


def badge(text: object) -> str:
    text = "" if text is None else str(text)
    cls = status_class(text)
    return f'<span class="{cls}">{esc(text)}</span>' if cls else esc(text)


def counts_html(counts: Mapping[str, int]) -> str:
    if not counts:
        return '<span class="muted">no actions</span>'
    return "".join(f'<span class="chip">{badge(k)} {v}</span>' for k, v in sorted(counts.items()))


def table(headers: Iterable[str], rows: Iterable[Iterable[str]], wide: Iterable[int] = ()) -> str:
    """An HTML table; cells are already-escaped HTML. Columns listed in ``wide`` may wrap."""
    wide = set(wide)
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>"
        + "".join(f'<td class="wide">{c}</td>' if i in wide else f"<td>{c}</td>" for i, c in enumerate(r))
        + "</tr>"
        for r in rows
    )
    if not body:
        return '<p class="note">Nothing to show.</p>'
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def select(name: str, options: Iterable[str], current: str) -> str:
    opts = ['<option value="">any</option>'] + [
        f'<option value="{esc(o)}"{" selected" if o == current else ""}>{esc(o)}</option>' for o in options
    ]
    return f'<label>{esc(name)}<select name="{esc(name)}">{"".join(opts)}</select></label>'


def text_input(name: str, current: str) -> str:
    return f'<label>{esc(name)}<input name="{esc(name)}" value="{esc(current)}" size="12"></label>'


def filter_form(action: str, fields: Iterable[str]) -> str:
    return (
        f'<form class="filters" method="get" action="{esc(action)}">{"".join(fields)}'
        f'<button type="submit">Filter</button> <a href="{esc(action)}">clear</a></form>'
    )


def pager(path: str, params: Mapping[str, str], page: int, total: int) -> str:
    pages = max((total + PAGE_ROWS - 1) // PAGE_ROWS, 1)
    if pages == 1:
        return ""
    parts = []
    if page > 1:
        parts.append(f'<a href="{link(path, {**params, "page": page - 1})}">previous</a>')
    parts.append(f'<span class="muted">page {page} of {pages}</span>')
    if page < pages:
        parts.append(f'<a href="{link(path, {**params, "page": page + 1})}">next</a>')
    return f'<div class="pager">{"".join(parts)}</div>'


# --- pages --------------------------------------------------------------------------------------------------------


class ServiceView(Protocol):
    def health(self) -> dict: ...

    def next_poll(self) -> datetime | None: ...


class Pages:
    def __init__(
        self,
        service: ServiceView,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.service = service
        self.settings = settings
        self.clock = clock
        self.reports = Reports(Path(settings.data_dir) / "reports")
        self.ledger = LedgerView(Path(settings.data_dir) / "ledger.sqlite")

    @staticmethod
    def layout(title: str, body: str) -> str:
        nav = "".join(
            f'<a href="{href}">{label}</a>'
            for href, label in (("/", "Status"), ("/runs", "Runs"), ("/reported", "Reported"), ("/ledger", "Ledger"))
        )
        return (
            '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{esc(title)} · pxar</title><link rel="stylesheet" href="/style.css"></head><body>'
            f"<header><strong>pxar</strong><nav>{nav}</nav>"
            '<span class="muted">read-only</span></header>'
            f"<main>{body}</main></body></html>\n"
        )

    # Status

    def status(self, params: Mapping[str, str]) -> tuple[int, str]:
        now = self.clock()
        health = self.service.health()
        loop = health.get("loop")
        service_rows = [
            ("loop", badge(loop)),
            ("last run", self._run_link(health.get("last_run_id"))),
            ("last status", badge(health.get("last_status"))),
            ("last finished", esc(fmt_time(health.get("last_finished")))),
            (
                "failed runs in a row",
                (
                    f"{badge('failed' if health.get('failed_runs') else 'ok')} {esc(health.get('failed_runs', 0))}"
                    f" of {self.settings.health_max_failed_runs} allowed"
                ),
            ),
        ]
        if health.get("run_active_since"):
            service_rows.append(("run active since", esc(fmt_time(health["run_active_since"]))))
        next_poll = self.service.next_poll()
        if next_poll is not None:
            service_rows.append(("next poll", f"{esc(fmt_time(next_poll))} ({esc(fmt_delta(now - next_poll))})"))
        if health.get("unhealthy"):
            service_rows.append(("unhealthy", f'<span class="bad">{esc(health["unhealthy"])}</span>'))

        cards = [
            self._card("Service", service_rows),
            self._card("MyList snapshot", self._snapshot_rows(now)),
            self._card("Ledger", self._ledger_rows()),
            self._card("Settings", self._settings_rows()),
        ]
        recent = self.reports.summaries(RECENT_RUNS)
        body = (
            "<h1>Status</h1>"
            f'<div class="grid">{"".join(cards)}</div>'
            "<h2>Recent runs</h2>"
            f"{self._runs_table(recent, now)}"
            f'<p class="note"><a href="/runs">All runs</a></p>'
        )
        return 200, self.layout("Status", body)

    @staticmethod
    def _card(title: str, rows: list[tuple[str, str]]) -> str:
        items = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in rows)
        return f'<section class="card"><h2>{esc(title)}</h2><dl>{items}</dl></section>'

    def _run_link(self, run_id: object) -> str:
        if isinstance(run_id, str) and RUN_ID.fullmatch(run_id):
            return f'<a href="/runs/{run_id}">{run_id}</a>'
        return esc(run_id)

    def _snapshot_rows(self, now: datetime) -> list[tuple[str, str]]:
        max_age = timedelta(hours=self.settings.snapshot_max_age_h)
        try:
            age = snapshot_age(Path(self.settings.mylist_path), now)
        except SnapshotError:
            return [("state", badge("unavailable")), ("detail", "the snapshot file can't be read")]
        state = "fresh" if snapshot_is_fresh(age, max_age) else "stale"
        return [
            ("state", badge(state)),
            ("age", esc(fmt_delta(age)) + ("" if age >= timedelta(0) else " (in the future)")),
            ("max age", f"{self.settings.snapshot_max_age_h} h"),
        ]

    def _ledger_rows(self) -> list[tuple[str, str]]:
        try:
            counts = self.ledger.status_counts()
        except LedgerUnavailable as exc:
            return [("state", esc(exc))]
        rows = [
            (status, f'<a href="{link("/ledger", {"status": status})}">{counts.get(status, 0)}</a>')
            for status in ("applied", "failed", "pending")
        ]
        if counts.get("pending"):
            rows.append(("attention", '<span class="bad">pending R1 rows: Plex may or may not be marked</span>'))
        return rows

    def _settings_rows(self) -> list[tuple[str, str]]:
        s = self.settings
        path_map = "<br>".join(f"{esc(p)} → {esc(q)}" for p, q in s.path_map) or '<span class="muted">none</span>'
        return [
            ("Plex sections", esc(", ".join(str(x) for x in s.plex_sections))),
            ("R2B_POLICY", esc(s.r2b_policy)),
            ("MAX_WRITES", esc(s.max_writes)),
            ("POLL_MINUTES", esc(s.poll_minutes)),
            ("SHOKO_TZ", esc(s.shoko_tz.key)),
            ("PATH_MAP", path_map),
        ]

    def _runs_table(self, runs: list[RunSummary], now: datetime) -> str:
        rows = [
            (
                self._run_link(r.run_id),
                f'{esc(fmt_time(r.started))} <span class="muted">({esc(fmt_delta(now - r.started))} ago)</span>',
                esc(r.mode),
                esc(r.scope),
                badge(r.snapshot) if r.snapshot else '<span class="muted">?</span>',
                '<span class="bad">unreadable</span>' if r.unreadable else counts_html(r.statuses),
            )
            for r in runs
        ]
        return table(("run", "started", "mode", "scope", "snapshot", "rows by status"), rows, wide=(5,))

    # Runs

    def runs(self, params: Mapping[str, str]) -> tuple[int, str]:
        mode = params.get("mode", "")
        runs = [r for r in self.reports.summaries() if not mode or r.mode == mode]
        page = page_number(params)
        shown = runs[(page - 1) * PAGE_ROWS : page * PAGE_ROWS]
        body = (
            "<h1>Runs</h1>"
            '<p class="note">The newest 200 runs; older reports are pruned. A scoped run comes from a Tautulli '
            "trigger and lists only that Plex item.</p>"
            + filter_form("/runs", [select("mode", ("apply", "plan"), mode)])
            + self._runs_table(shown, self.clock())
            + pager("/runs", params, page, len(runs))
        )
        return 200, self.layout("Runs", body)

    def run_detail(self, run_id: str, params: Mapping[str, str]) -> tuple[int, str]:
        summary = self.reports.summary(run_id)
        rows = self.reports.rows(run_id) if summary else None
        if summary is None or rows is None:
            return 404, self.layout(
                "Run not found",
                "<h1>Run not found</h1><p>That report doesn't exist or "
                "can't be read; reports older than the newest 200 are pruned.</p>",
            )
        filters = {k: params.get(k, "") for k in ("rule", "status", "reason", "aid", "item")}
        shown = [r for r in rows if self._row_matches(r, filters)]
        page = page_number(params)
        meta = self.reports.meta(run_id)
        scope_bits = [summary.scope]
        if meta.get("rules"):
            scope_bits.append("rules " + ",".join(str(x) for x in meta["rules"]))
        if meta.get("file_id") is not None:
            scope_bits.append(f"file {meta['file_id']}")
        info = [
            ("started", esc(fmt_time(summary.started))),
            ("mode", esc(summary.mode)),
            ("scope", esc(" · ".join(scope_bits))),
            ("snapshot", badge(summary.snapshot) if summary.snapshot else '<span class="muted">?</span>'),
            ("rows", counts_html(summary.statuses)),
            ("download", f'<a href="/runs/{run_id}.csv" download>CSV</a>'),
        ]
        form = filter_form(
            f"/runs/{run_id}",
            [
                select("rule", sorted({r["rule"] for r in rows}), filters["rule"]),
                select("status", sorted({r["status"] for r in rows}), filters["status"]),
                select("reason", sorted({r["reason"] for r in rows}), filters["reason"]),
                text_input("aid", filters["aid"]),
                text_input("item", filters["item"]),
            ],
        )
        body = (
            f"<h1>Run {esc(run_id)}</h1>"
            + self._card("Run", info)
            + "<h2>Rows</h2>"
            + '<p class="note"><code>item</code> matches a Plex rating key, Shoko file id or AniDB fid. '
            f"{len(shown)} of {len(rows)} rows match.</p>"
            + form
            + self._report_table(shown[(page - 1) * PAGE_ROWS : page * PAGE_ROWS])
            + pager(f"/runs/{run_id}", params, page, len(shown))
        )
        return 200, self.layout(f"Run {run_id}", body)

    @staticmethod
    def _row_matches(row: Mapping[str, str], filters: Mapping[str, str]) -> bool:
        for key in ("rule", "status", "reason", "aid"):
            if filters[key] and row[key] != filters[key]:
                return False
        item = filters["item"]
        return not item or item in (row["rating_key"], row["file_id"], row["fid"])

    @staticmethod
    def _report_table(rows: list[dict[str, str]]) -> str:
        cells = [
            (
                esc(r["rule"]),
                badge(r["status"]),
                esc(r["reason"]),
                esc(r["rating_key"]),
                esc(r["file_id"]),
                anidb_link("file", r["fid"]),
                esc(r["eids"]),
                anidb_link("anime", r["aid"]),
                esc(r["release_group"]),
                esc(fmt_time(r["when"])),
                esc(r["anidb_state"]),
                esc(fmt_time(r["anidb_viewdate"])),
                esc(r["detail"]),
            )
            for r in rows
        ]
        headers = (
            "rule",
            "status",
            "reason",
            "plex item",
            "shoko file",
            "fid",
            "eids",
            "aid",
            "group",
            "when",
            "anidb state",
            "anidb viewed",
            "detail",
        )
        return table(headers, cells, wide=(12,))

    # Reported items

    def reported(self, params: Mapping[str, str]) -> tuple[int, str]:
        full = self.reports.full_runs()
        if not full:
            return 200, self.layout("Reported", "<h1>Reported items</h1><p>No complete run report yet.</p>")
        latest, previous = full[0], (full[1] if len(full) > 1 else None)
        reason = params.get("reason", "")
        intro = (
            "<h1>Reported items</h1>"
            f'<p class="note">From the latest full run, {self._run_link(latest.run_id)} '
            f"({esc(fmt_time(latest.started))}). These are never written; each needs a fix outside pxar or none "
            "at all. The change column compares with the previous full run"
            + (f" ({self._run_link(previous.run_id)})" if previous else "")
            + ".</p>"
        )
        summary_rows = []
        for (rule, why), n in sorted(latest.reasons.items(), key=lambda kv: (-kv[1], kv[0])):
            meaning, todo = REASONS.get(why, ("", ""))
            change = "" if previous is None else self._change(n - previous.reasons.get((rule, why), 0))
            summary_rows.append(
                (
                    esc(rule),
                    f'<a href="{link("/reported", {"reason": why})}">{esc(why)}</a>',
                    str(n),
                    change,
                    esc(meaning),
                    esc(todo),
                )
            )
        body = intro + table(("rule", "reason", "items", "change", "meaning", "what to do"), summary_rows, (4, 5))
        if reason:
            rows = self.reports.rows(latest.run_id) or []
            picked = [r for r in rows if r["status"] == "reported" and r["reason"] == reason]
            picked.sort(key=lambda r: (r["aid"] == "", r["aid"].zfill(10), r["release_group"], r["rating_key"]))
            page = page_number(params)
            body += (
                f"<h2>{esc(reason)}</h2>"
                f'<p class="note">{len(picked)} items, sorted by anime and release group.</p>'
                + self._report_table(picked[(page - 1) * PAGE_ROWS : page * PAGE_ROWS])
                + pager("/reported", params, page, len(picked))
            )
        return 200, self.layout("Reported", body)

    @staticmethod
    def _change(delta: int) -> str:
        if delta > 0:
            return f'<span class="warn">+{delta}</span>'
        if delta < 0:
            return f'<span class="ok">{delta}</span>'
        return '<span class="muted">0</span>'

    # Ledger

    def ledger_page(self, params: Mapping[str, str]) -> tuple[int, str]:
        filters: dict[str, object] = {k: params[k] for k in LEDGER_FILTERS if params.get(k)}
        if "file_id" in filters:
            raw = str(filters["file_id"])
            filters["file_id"] = int(raw) if raw.isascii() and raw.isdigit() and len(raw) < 19 else -1
        page = page_number(params)
        form = filter_form(
            "/ledger",
            [
                select("status", ("applied", "failed", "pending"), params.get("status", "")),
                select("rule", ("R1", "R2", "R2b"), params.get("rule", "")),
                text_input("run_id", params.get("run_id", "")),
                text_input("rating_key", params.get("rating_key", "")),
                text_input("file_id", params.get("file_id", "")),
            ],
        )
        note = (
            '<p class="note">Every write pxar attempted, newest first. A <span class="bad">pending</span> R1 row '
            "means the service stopped after recording the R1 and before recording how the scrobble went, so Plex may or "
            "may not have it. "
            "Failed writes are retried on the next run.</p>"
        )
        try:
            rows, total = self.ledger.query(filters, (page - 1) * PAGE_ROWS)
        except LedgerUnavailable as exc:
            return 200, self.layout("Ledger", f"<h1>Ledger</h1>{note}<p>{esc(exc)}</p>")
        cells = [
            (
                esc(r["id"]),
                esc(fmt_time(r["created_at"])),
                self._run_link(r["run_id"]),
                esc(r["rule"]),
                badge(r["status"]),
                esc(r["rating_key"]),
                esc(r["file_id"]),
                anidb_link("file", "" if r["fid"] is None else str(r["fid"])),
                esc(fmt_time(r["when_ts"])),
                self._state_diff(r["before"], r["after"]),
                esc(r["detail"]),
            )
            for r in rows
        ]
        headers = (
            "id",
            "recorded",
            "run",
            "rule",
            "status",
            "plex item",
            "shoko file",
            "fid",
            "date written",
            "before → after",
            "detail",
        )
        body = (
            f"<h1>Ledger</h1>{note}{form}"
            f'<p class="note">{total} rows match.</p>'
            + table(headers, cells, wide=(9, 10))
            + pager("/ledger", params, page, total)
        )
        return 200, self.layout("Ledger", body)

    @staticmethod
    def _state_diff(before: str | None, after: str | None) -> str:
        def load(raw: str | None) -> dict:
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                return {"?": raw}
            return data if isinstance(data, dict) else {"?": data}

        b, a = load(before), load(after)
        lines = []
        for key in sorted(set(b) | set(a)):
            if key in a and key in b:
                lines.append(f"{key}: {b[key]} → {a[key]}")
            elif key in a:
                lines.append(f"{key}: → {a[key]}")
            else:
                lines.append(f"{key}: {b[key]}")
        return f"<pre>{esc(chr(10).join(lines))}</pre>" if lines else ""


# --- HTTP -------------------------------------------------------------------------------------------------------

_RUN_PATH = re.compile(r"/runs/(" + RUN_ID.pattern + r")")
_RUN_CSV_PATH = re.compile(r"/runs/(" + RUN_ID.pattern + r")\.csv")


def make_ui_server(pages: Pages, token: str, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        timeout = 10
        server_version = "pxar"
        sys_version = ""

        def _send(self, code: int, body: bytes, content_type: str, extra: Mapping[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in {**_SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _page(self, code: int, page: str) -> None:
            self._send(code, page.encode(), "text/html; charset=utf-8")

        def _route(self) -> None:
            if not authorized(self.headers.get("Authorization"), token):
                self._send(
                    401,
                    b"authentication required\n",
                    "text/plain; charset=utf-8",
                    {"WWW-Authenticate": 'Basic realm="pxar", charset="UTF-8"'},
                )
                return
            url = urlsplit(self.path)
            params = query_params(url.query)
            path = url.path.rstrip("/") or "/"
            try:
                if path == "/style.css":
                    self._send(200, _STYLE.encode(), "text/css; charset=utf-8")
                elif path == "/":
                    self._page(*pages.status(params))
                elif path == "/runs":
                    self._page(*pages.runs(params))
                elif path == "/reported":
                    self._page(*pages.reported(params))
                elif path == "/ledger":
                    self._page(*pages.ledger_page(params))
                elif m := _RUN_CSV_PATH.fullmatch(path):
                    self._csv(m.group(1))
                elif m := _RUN_PATH.fullmatch(path):
                    self._page(*pages.run_detail(m.group(1), params))
                else:
                    self._page(404, pages.layout("Not found", "<h1>Not found</h1>"))
            except Exception as exc:  # noqa: BLE001 - a broken page must not kill the handler or leak details
                log.error("web UI request failed: %s", type(exc).__name__)
                self._page(500, pages.layout("Error", "<h1>Something went wrong</h1><p>See the service log.</p>"))

        def _csv(self, run_id: str) -> None:
            try:
                data = pages.reports.csv_path(run_id).read_bytes()
            except OSError:
                self._page(404, pages.layout("Not found", "<h1>Not found</h1>"))
                return
            name = quote(f"pxar-{run_id}.csv")
            self._send(200, data, "text/csv; charset=utf-8", {"Content-Disposition": f"attachment; filename={name}"})

        def do_GET(self) -> None:
            self._route()

        def do_HEAD(self) -> None:
            self._route()

        def log_message(self, format: str, *args) -> None:
            # Intentionally empty: no per-request access log (it would include query strings).
            pass

    server_cls = ThreadingHTTPServer
    if ":" in host:

        class V6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server_cls = V6Server
    return server_cls((host, port), Handler)
