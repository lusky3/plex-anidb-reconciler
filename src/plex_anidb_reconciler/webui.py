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
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from plex_anidb_reconciler.config import Settings
from plex_anidb_reconciler.httpd import BoundedServer, QuietHandler, bind
from plex_anidb_reconciler.models import SnapshotState, Status
from plex_anidb_reconciler.mylist import SnapshotError, snapshot_age, snapshot_is_fresh
from plex_anidb_reconciler.report import COLUMNS, RunMeta, csv_cell_value
from plex_anidb_reconciler.runner import EXECUTABLE_RULES, KEEP_REPORTS

log = logging.getLogger(__name__)

RUN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")
PAGE_ROWS = 200
MAX_PARAM = 200  # longer query values are cut; every filter is an exact match on a short value
RECENT_RUNS = 10
LEDGER_STATUSES = (Status.APPLIED, Status.FAILED, Status.PENDING)

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
:root{color-scheme:light dark;--bg:#fbfbfa;--fg:#1d1d1b;--muted:#5f5f5a;--line:#deded8;--field:#8a8a83;
--head:#f0f0ec;--sel:#e6ecf2;--accent:#2f5d8a;--ok:#25683f;--warn:#8a5700;--bad:#b3261e;--chip:#ecece6}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#e8e8e3;--muted:#a3a39c;--line:#34342f;--field:#6e6e67;
--head:#1f1f1d;--sel:#1e2a36;--accent:#8db4dc;--ok:#6cc08f;--warn:#e0a84a;--bad:#f08a80;--chip:#2a2a27}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
font-variant-numeric:tabular-nums}
header{display:flex;flex-wrap:wrap;gap:.25rem 1.25rem;align-items:baseline;padding:.8rem 16px;
border-bottom:1px solid var(--line)}
header strong{font-size:1.05rem;letter-spacing:.02em}
header nav{display:flex;flex-wrap:wrap;gap:0 1rem}
header nav a{padding:.2rem 0}
nav a[aria-current]{color:var(--fg);font-weight:600}
main{padding:0 16px 2rem;max-width:1400px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:1.35rem;margin:1.2rem 0 .4rem;text-wrap:balance}h2{font-size:1.05rem;margin:1.6rem 0 .4rem}
p.note{max-width:75ch;text-wrap:pretty}
p.note,.muted{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(19rem,1fr));gap:.8rem}
.card{border:1px solid var(--line);border-radius:6px;padding:.7rem .9rem}
.card h2{margin:0 0 .4rem;font-size:.95rem}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.15rem .9rem;margin:0}
dt{color:var(--muted)}dd{margin:0;overflow-wrap:break-word;min-width:0}
dd a{display:inline-block;min-width:1.5rem}
.wrap{overflow:auto;max-height:calc(100vh - 7rem);border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;font-size:.85rem;width:100%}
th,td{padding:.3rem .55rem;text-align:left;white-space:nowrap;border-bottom:1px solid var(--line);
vertical-align:top}
th{background:var(--head);position:sticky;top:0;z-index:1}
td.wide{white-space:normal;min-width:16rem}
td.num,th.num{text-align:right}
tr.sel td{background:var(--sel)}
tr:last-child td{border-bottom:0}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.chip{display:inline-block;background:var(--chip);border-radius:3px;padding:0 .35rem;margin:0 .2rem .2rem 0}
form.filters{display:flex;flex-wrap:wrap;gap:.5rem .75rem;align-items:end;margin:.6rem 0}
form.filters label{display:flex;flex-direction:column;font-size:.8rem;color:var(--muted)}
input,select,button{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--field);
border-radius:4px;padding:.25rem .4rem}
button{cursor:pointer}
@media (pointer:coarse){input,select,button{min-height:2.75rem}}
.pager{margin:.6rem 0;display:flex;gap:1rem}
pre{margin:0;font:.8rem/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media (max-width:40rem){
.wrap.stack{max-height:none;overflow:visible;border:0}
table.stack thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
table.stack,table.stack tbody,table.stack tr,table.stack td{display:block}
table.stack tr{border:1px solid var(--line);border-radius:6px;margin-bottom:.5rem;padding:.3rem 0}
table.stack td{display:flex;gap:.6rem;border:0;white-space:normal;padding:.1rem .6rem;text-align:left}
table.stack td::before{content:attr(data-label);color:var(--muted);min-width:7rem;flex:none}
}
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


# --- formatting ------------------------------------------------------------------------------------------------


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def run_started(run_id: str) -> datetime:
    return datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def fmt_time(value: object) -> str:
    """A datetime or ISO string as ``YYYY-MM-DD HH:MM UTC``; other text is returned as is."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return str(value)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def fmt_age(age: timedelta) -> str:
    """How long ago (positive ``age``) or how far ahead (negative, prefixed "in ") something is."""
    seconds = int(abs(age).total_seconds())
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        text = f"{days} d {hours} h"
    elif hours:
        text = f"{hours} h {minutes} min"
    else:
        text = f"{minutes} min"
    return text if age >= timedelta(0) else f"in {text}"


# --- run reports -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    meta: RunMeta | None  # None: a report from before meta files existed
    statuses: Counter[str] = field(default_factory=Counter)
    reasons: Counter[tuple[str, str]] = field(default_factory=Counter)  # (rule, reason) of reported rows
    rows: int = 0
    unreadable: bool = False

    @property
    def started(self) -> datetime:
        return run_started(self.run_id)

    @property
    def mode(self) -> str:
        """apply | plan | ? (no meta file and no row that tells)."""
        if self.meta is not None:
            return self.meta.mode
        if self.statuses.get(Status.DRY_RUN):
            return "plan"
        if any(self.statuses.get(s) for s in (Status.APPLIED, Status.FAILED, Status.CAPPED)):
            return "apply"
        return "?"

    @property
    def snapshot(self) -> str:
        return self.meta.snapshot if self.meta is not None else ""

    @property
    def scope(self) -> str:
        if self.meta is None:
            return "?"
        return "full" if self.meta.full else "scoped"

    @property
    def scope_label(self) -> str:
        """The scope plus any write limits, e.g. ``scoped · rules R1,R2 · file 7``."""
        parts = [self.scope]
        if self.meta is not None and self.meta.rules:
            parts.append("rules " + ",".join(self.meta.rules))
        if self.meta is not None and self.meta.file_id is not None:
            parts.append(f"file {self.meta.file_id}")
        return " · ".join(parts)

    @property
    def lists_everything(self) -> bool:
        """Whether the report lists every Plex item, so its reported rows compare with another run's. A report
        without a meta file counts only if it has rows and tells its mode; it may still be a scoped run, which
        nothing in it records."""
        if self.unreadable:
            return False
        if self.meta is not None:
            return self.meta.full and self.meta.snapshot != SnapshotState.UNAVAILABLE
        return self.rows > 0 and self.mode != "?"


class Reports:
    """The run reports under one directory. Summaries are cached per version of the CSV and its meta file (size
    and mtime); each is built once even when several requests ask for it at the same time."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._cache: dict[str, tuple[tuple, RunSummary]] = {}
        self._lock = threading.Lock()  # guards _cache
        self._build_lock = threading.Lock()  # one summary build at a time, so a cold cache doesn't stampede

    def run_ids(self) -> list[str]:
        """Run ids with a CSV report, newest first."""
        try:
            names = [p.stem for p in self.directory.glob("*.csv") if RUN_ID.fullmatch(p.stem)]
        except OSError:
            return []
        return sorted(names, reverse=True)

    def csv_path(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.csv"

    def _meta_path(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json"

    def rows(self, run_id: str) -> list[dict[str, str]] | None:
        """The CSV rows of a run with their original values, or None if the report can't be read."""
        try:
            with self.csv_path(run_id).open(newline="", encoding="utf-8") as fh:
                return [{c: csv_cell_value(row.get(c) or "") for c in COLUMNS} for row in csv.DictReader(fh)]
        except OSError, csv.Error, UnicodeDecodeError:
            return None

    def _version(self, run_id: str) -> tuple | None:
        try:
            st = self.csv_path(run_id).stat()
        except OSError:
            return None
        try:
            meta_mtime = self._meta_path(run_id).stat().st_mtime_ns
        except OSError:
            meta_mtime = None
        return (st.st_size, st.st_mtime_ns, meta_mtime)

    def _cached(self, run_id: str, version: tuple) -> RunSummary | None:
        with self._lock:
            hit = self._cache.get(run_id)
        return hit[1] if hit and hit[0] == version else None

    def summary(self, run_id: str) -> RunSummary | None:
        version = self._version(run_id)
        if version is None:
            return None
        if (hit := self._cached(run_id, version)) is not None:
            return hit
        with self._build_lock:
            if (hit := self._cached(run_id, version)) is not None:  # built while this request waited
                return hit
            summary = self._summarize(run_id)
            if not summary.unreadable:  # a read can fail transiently (e.g. EMFILE): try again next time
                with self._lock:
                    self._cache[run_id] = (version, summary)
        return summary

    def _summarize(self, run_id: str) -> RunSummary:
        rows = self.rows(run_id)
        return RunSummary(
            run_id=run_id,
            meta=RunMeta.load(self._meta_path(run_id)),
            statuses=Counter(r["status"] for r in rows or ()),
            reasons=Counter((r["rule"], r["reason"]) for r in rows or () if r["status"] == Status.REPORTED),
            rows=len(rows or ()),
            unreadable=rows is None,
        )

    def summaries(self, limit: int | None = None) -> list[RunSummary]:
        all_ids = self.run_ids()
        self._forget_except(all_ids)
        ids = all_ids[:limit] if limit else all_ids
        return [s for s in (self.summary(i) for i in ids) if s is not None]

    def _forget_except(self, run_ids: Iterable[str]) -> None:
        """Drop cached summaries of reports that have been pruned."""
        keep = set(run_ids)
        with self._lock:
            for gone in set(self._cache) - keep:
                del self._cache[gone]

    def full_runs(self) -> list[RunSummary]:
        """Runs whose reports list every item, newest first."""
        return [s for s in self.summaries() if s.lists_everything]


@dataclass(frozen=True)
class ReasonCount:
    rule: str
    reason: str
    count: int
    change: int | None  # against the previous full run; None without one


def reason_changes(latest: RunSummary, previous: RunSummary | None) -> list[ReasonCount]:
    """Reported rows per (rule, reason) of ``latest`` with the change since ``previous``, most items first. A
    reason of ``previous`` that dropped to zero stays in the list, so a fully fixed problem shows up."""
    before = previous.reasons if previous is not None else Counter()
    out = []
    for key in set(latest.reasons) | set(before):
        count = latest.reasons.get(key, 0)
        out.append(ReasonCount(key[0], key[1], count, None if previous is None else count - before.get(key, 0)))
    return sorted(out, key=lambda r: (-r.count, r.rule, r.reason))


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
_LEDGER_PAGE = (
    "SELECT " + ", ".join(LEDGER_COLUMNS) + " FROM actions" + _LEDGER_WHERE + " ORDER BY id DESC LIMIT ?6 OFFSET ?7"
)
_LEDGER_COUNT = "SELECT COUNT(*) FROM actions" + _LEDGER_WHERE
_LEDGER_STATUS_COUNTS = "SELECT status, COUNT(*) FROM actions GROUP BY status"
LEDGER_FILTERS = ("run_id", "rule", "status", "rating_key", "file_id")
_INT64_DIGITS = 18  # longer digit strings may not fit SQLite's 64-bit integer


class LedgerUnavailable(Exception):
    """The ledger file is missing or can't be read."""


def _as_file_id(raw: str) -> int:
    """A file_id filter as an integer; text that isn't a plain number of at most 18 digits matches nothing (-1)."""
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= _INT64_DIGITS else -1


class LedgerView:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise LedgerUnavailable("no ledger yet: it appears after the first applied write")
        conn = None
        try:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
            conn.execute("PRAGMA query_only = ON")
            yield conn
        except sqlite3.Error as exc:
            raise LedgerUnavailable(f"ledger unreadable ({type(exc).__name__})") from None
        finally:
            if conn is not None:
                conn.close()

    def status_counts(self) -> dict[str, int]:
        with self._connection() as conn:
            return dict(conn.execute(_LEDGER_STATUS_COUNTS).fetchall())

    def count(self, filters: Mapping[str, str]) -> int:
        with self._connection() as conn:
            return conn.execute(_LEDGER_COUNT, self._params(filters)).fetchone()[0]

    def rows(self, filters: Mapping[str, str], offset: int, limit: int = PAGE_ROWS) -> list[dict[str, object]]:
        """One page of rows matching ``filters`` (exact matches; absent keys don't filter), newest first."""
        with self._connection() as conn:
            rows = conn.execute(_LEDGER_PAGE, (*self._params(filters), limit, offset)).fetchall()
        return [dict(zip(LEDGER_COLUMNS, row, strict=True)) for row in rows]

    @staticmethod
    def _params(filters: Mapping[str, str]) -> tuple[object, ...]:
        values: list[object] = [filters.get(name) or None for name in LEDGER_FILTERS]
        if filters.get("file_id"):
            values[LEDGER_FILTERS.index("file_id")] = _as_file_id(filters["file_id"])
        return tuple(values)


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


@dataclass(frozen=True)
class Page:
    number: int  # 1-based, clamped to the last page
    pages: int

    @classmethod
    def of(cls, params: Mapping[str, str], total: int) -> Page:
        pages = max((total + PAGE_ROWS - 1) // PAGE_ROWS, 1)
        raw = params.get("page", "1")
        number = int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 6 else 1
        return cls(min(max(number, 1), pages), pages)

    @property
    def offset(self) -> int:
        return (self.number - 1) * PAGE_ROWS

    def take[T](self, items: Sequence[T]) -> Sequence[T]:
        return items[self.offset : self.offset + PAGE_ROWS]


def link(path: str, params: Mapping[str, object] | None = None) -> str:
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    return esc(path + ("?" + urlencode(clean) if clean else ""))


def anidb_link(kind: str, value: object) -> str:
    text = "" if value is None else str(value)
    if not (text.isascii() and text.isdigit()):
        return esc(text)
    return f'<a href="https://anidb.net/{kind}/{text}" rel="noreferrer noopener">{esc(text)}</a>'


def status_class(status: str) -> str:
    if status in (Status.APPLIED, SnapshotState.OK, "fresh"):
        return "ok"
    bad = (Status.FAILED, Status.PENDING, SnapshotState.STALE, SnapshotState.UNAVAILABLE)
    if status in bad or status.startswith(("error", "snapshot_")):
        return "bad"
    if status in (Status.CAPPED, Status.SKIPPED, Status.SKIPPED_LEDGER):
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


@dataclass(frozen=True)
class Table:
    """An HTML table whose cells are already-escaped HTML."""

    caption: str
    headers: Sequence[str]
    wide: frozenset[int] = frozenset()  # columns that may wrap
    num: frozenset[int] = frozenset()  # right-aligned numeric columns
    stack: bool = False  # on a phone, show each row as a labelled block

    def render(self, rows: Iterable[Sequence[str]], *, empty: str = "Nothing to show.", selected: int = -1) -> str:
        body = "".join(self._row(cells, i == selected) for i, cells in enumerate(rows))
        if not body:
            return f'<p class="note">{empty}</p>'
        head = "".join(f"<th{self._class(i)}>{esc(h)}</th>" for i, h in enumerate(self.headers))
        stack = " stack" if self.stack else ""
        return (
            f'<div class="wrap{stack}"><table class="report{stack}">'
            f'<caption class="vh">{esc(self.caption)}</caption><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>"
        )

    def _class(self, i: int) -> str:
        names = [name for name, cols in (("wide", self.wide), ("num", self.num)) if i in cols]
        return f' class="{" ".join(names)}"' if names else ""

    def _row(self, cells: Sequence[str], selected: bool) -> str:
        if self.stack:  # the label (::before) and one span: two flex items however many parts the cell has
            tds = "".join(
                f'<td{self._class(i)} data-label="{esc(self.headers[i])}"><span>{c}</span></td>'
                for i, c in enumerate(cells)
            )
        else:
            tds = "".join(f"<td{self._class(i)}>{c}</td>" for i, c in enumerate(cells))
        return f'<tr class="sel">{tds}</tr>' if selected else f"<tr>{tds}</tr>"


def select(name: str, label: str, options: Iterable[str], current: str) -> str:
    opts = ['<option value="">any</option>'] + [
        f'<option value="{esc(o)}"{" selected" if o == current else ""}>{esc(o)}</option>' for o in options
    ]
    return f'<label>{esc(label)}<select name="{esc(name)}">{"".join(opts)}</select></label>'


def text_input(name: str, label: str, current: str) -> str:
    return f'<label>{esc(label)}<input name="{esc(name)}" value="{esc(current)}" size="12"></label>'


def filter_form(action: str, fields: Iterable[str]) -> str:
    return (
        f'<form class="filters" method="get" action="{esc(action)}">{"".join(fields)}'
        f'<button type="submit">Filter</button> <a href="{esc(action)}">Clear filters</a></form>'
    )


def no_match(path: str) -> str:
    return f'No rows match these filters. <a href="{esc(path)}">Clear filters</a>'


def pager(path: str, params: Mapping[str, str], page: Page) -> str:
    if page.pages == 1:
        return ""
    parts = []
    if page.number > 1:
        parts.append(f'<a href="{link(path, {**params, "page": page.number - 1})}">Previous page</a>')
    parts.append(f'<span class="muted">page {page.number} of {page.pages}</span>')
    if page.number < page.pages:
        parts.append(f'<a href="{link(path, {**params, "page": page.number + 1})}">Next page</a>')
    return f'<nav class="pager" aria-label="Pages">{"".join(parts)}</nav>'


def card(title: str, rows: Sequence[tuple[str, str]]) -> str:
    items = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in rows)
    return f'<section class="card"><h2>{esc(title)}</h2><dl>{items}</dl></section>'


def run_link(run_id: object) -> str:
    if isinstance(run_id, str) and RUN_ID.fullmatch(run_id):
        return f'<a href="/runs/{run_id}">{run_id}</a>'
    return esc(run_id)


def state_diff(before: object, after: object) -> str:
    """A ledger row's before → after state (JSON text), one key per line, as a <pre>."""

    def load(raw: object) -> dict:
        try:
            data = json.loads(raw) if isinstance(raw, str) and raw else {}
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


def change_html(delta: int | None) -> str:
    if delta is None:
        return ""
    if delta > 0:
        return f'<span class="warn">+{delta}</span>'
    if delta < 0:
        return f'<span class="ok">{delta}</span>'
    return '<span class="muted">0</span>'


# --- table rows -------------------------------------------------------------------------------------------------

REPORT_HEADERS = ("rule", "status", "reason", "plex item", "shoko file", "fid", "eids", "aid", "group", "when",
                  "anidb state", "anidb viewed", "detail")  # fmt: skip
ITEM_COLUMNS = 3  # report columns from "plex item" on: what a list of one reason's items needs
RUNS_TABLE = Table(
    "Runs, newest first",
    ("run", "started", "mode", "scope", "snapshot", "rows by status"),
    wide=frozenset({5}),
    stack=True,
)
REPORT_TABLE = Table("Report rows", REPORT_HEADERS, wide=frozenset({12}))
REASON_ITEMS_TABLE = Table("Items with this reason", REPORT_HEADERS[ITEM_COLUMNS:], wide=frozenset({9}))
REASONS_TABLE = Table(
    "Reported items by reason",
    ("rule", "reason", "items", "change", "meaning", "what to do"),
    wide=frozenset({4, 5}),
    num=frozenset({2, 3}),
    stack=True,
)
LEDGER_TABLE = Table(
    "Ledger rows, newest first",
    (
        "status",
        "rule",
        "plex item",
        "shoko file",
        "fid",
        "date written",
        "before → after",
        "detail",
        "run",
        "recorded",
        "id",
    ),
    wide=frozenset({6, 7}),
    num=frozenset({10}),
)


def run_cells(r: RunSummary, now: datetime) -> tuple[str, ...]:
    return (
        run_link(r.run_id),
        f'{esc(fmt_time(r.started))} <span class="muted">({esc(fmt_age(now - r.started))} ago)</span>',
        esc(r.mode),
        esc(r.scope),
        badge(r.snapshot) if r.snapshot else '<span class="muted">?</span>',
        '<span class="bad">unreadable</span>' if r.unreadable else counts_html(r.statuses),
    )


def report_cells(r: Mapping[str, str]) -> tuple[str, ...]:
    return (
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


def reason_cells(c: ReasonCount) -> tuple[str, ...]:
    meaning, todo = REASONS.get(c.reason, ("", ""))
    return (
        esc(c.rule),
        f'<a href="{link("/reported", {"reason": c.reason})}">{esc(c.reason)}</a>',
        str(c.count),
        change_html(c.change),
        esc(meaning),
        esc(todo),
    )


def ledger_cells(r: Mapping[str, object]) -> tuple[str, ...]:
    return (
        badge(r["status"]),
        esc(r["rule"]),
        esc(r["rating_key"]),
        esc(r["file_id"]),
        anidb_link("file", r["fid"]),
        esc(fmt_time(r["when_ts"])),
        state_diff(r["before"], r["after"]),
        esc(r["detail"]),
        run_link(r["run_id"]),
        esc(fmt_time(r["created_at"])),
        esc(r["id"]),
    )


def row_matches(row: Mapping[str, str], filters: Mapping[str, str]) -> bool:
    """Exact matches on rule/status/reason/aid; ``item`` matches the Plex rating key, Shoko file id or AniDB fid."""
    for key in ("rule", "status", "reason", "aid"):
        if filters[key] and row[key] != filters[key]:
            return False
    item = filters["item"]
    return not item or item in (row["rating_key"], row["file_id"], row["fid"])


# --- pages --------------------------------------------------------------------------------------------------------

NAV = (("/", "Status"), ("/runs", "Runs"), ("/reported", "Reported"), ("/ledger", "Ledger"))


class ServiceView(Protocol):
    def health(self) -> dict: ...

    def next_poll(self) -> datetime | None: ...


Response = tuple[int, str]


class Pages:
    """The UI's pages. Each page method takes the query parameters and returns (HTTP status, HTML)."""

    def __init__(
        self,
        service: ServiceView,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.service = service
        self.settings = settings
        self.clock = clock
        self.reports = Reports(settings.reports_dir)
        self.ledger_view = LedgerView(settings.ledger_path)

    @staticmethod
    def layout(title: str, body: str, current: str = "") -> str:
        nav = "".join(
            f'<a href="{href}"' + (' aria-current="page"' if href == current else "") + f">{label}</a>"
            for href, label in NAV
        )
        return (
            '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{esc(title)} · pxar</title><link rel="stylesheet" href="/style.css"></head><body>'
            f'<header><strong>pxar</strong><nav aria-label="Main">{nav}</nav>'
            '<span class="muted">read-only</span></header>'
            f"<main>{body}</main></body></html>\n"
        )

    def not_found(self, what: str = "Page") -> Response:
        return 404, self.layout("Not found", f"<h1>{esc(what)} not found</h1>")

    def run_csv(self, run_id: str) -> bytes | None:
        try:
            return self.reports.csv_path(run_id).read_bytes()
        except OSError:
            return None

    # Status

    def status(self, params: Mapping[str, str]) -> Response:
        now = self.clock()
        cards = [
            card("Service", self._service_rows(now)),
            card("MyList snapshot", self._snapshot_rows(now)),
            card("Ledger", self._ledger_rows()),
            card("Settings", self._settings_rows()),
        ]
        recent = [run_cells(r, now) for r in self.reports.summaries(RECENT_RUNS)]
        body = (
            "<h1>Status</h1>"
            f'<div class="grid">{"".join(cards)}</div>'
            "<h2>Recent runs</h2>"
            f"{RUNS_TABLE.render(recent, empty='No runs yet.')}"
            '<p class="note"><a href="/runs">All runs</a></p>'
        )
        return 200, self.layout("Status", body, "/")

    def _service_rows(self, now: datetime) -> list[tuple[str, str]]:
        health = self.service.health()
        failed = health.get("failed_runs") or 0
        rows = [
            ("loop", badge(health.get("loop"))),
            ("last run", run_link(health.get("last_run_id"))),
            ("last status", badge(health.get("last_status"))),
            ("last finished", esc(fmt_time(health.get("last_finished")))),
            (
                "failures in a row",
                (
                    f'<span class="{"bad" if failed else "ok"}">{esc(failed)}</span>'
                    f" of {self.settings.health_max_failed_runs} allowed"
                ),
            ),
        ]
        if health.get("run_active_since"):
            rows.append(("run active since", esc(fmt_time(health["run_active_since"]))))
        next_poll = self.service.next_poll()
        if next_poll is not None:
            rows.append(("next poll", f"{esc(fmt_time(next_poll))} ({esc(fmt_age(now - next_poll))})"))
        if health.get("unhealthy"):
            rows.append(("unhealthy", f'<span class="bad">{esc(health["unhealthy"])}</span>'))
        return rows

    def _snapshot_rows(self, now: datetime) -> list[tuple[str, str]]:
        max_age = timedelta(hours=self.settings.snapshot_max_age_h)
        try:
            age = snapshot_age(Path(self.settings.mylist_path), now)
        except SnapshotError:
            return [("state", badge(SnapshotState.UNAVAILABLE)), ("detail", "the snapshot file can't be read")]
        if age < timedelta(0):
            age_text = f"{fmt_age(-age)} in the future (check the clocks)"
        else:
            age_text = f"{fmt_age(age)} old"
        return [
            ("state", badge("fresh" if snapshot_is_fresh(age, max_age) else SnapshotState.STALE)),
            ("age", esc(age_text)),
            ("max age", f"{self.settings.snapshot_max_age_h} h"),
        ]

    def _ledger_rows(self) -> list[tuple[str, str]]:
        try:
            counts = self.ledger_view.status_counts()
        except LedgerUnavailable as exc:
            return [("state", esc(exc))]
        rows = [
            (status, f'<a href="{link("/ledger", {"status": status})}">{counts.get(status, 0)}</a>')
            for status in LEDGER_STATUSES
        ]
        if counts.get(Status.PENDING):
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

    # Runs

    def runs(self, params: Mapping[str, str]) -> Response:
        mode = params.get("mode", "")
        runs = [r for r in self.reports.summaries() if not mode or r.mode == mode]
        page = Page.of(params, len(runs))
        now = self.clock()
        body = (
            "<h1>Runs</h1>"
            f'<p class="note">The newest {KEEP_REPORTS} runs; older reports are pruned. A scoped run comes from a '
            "Tautulli trigger and lists only that Plex item.</p>"
            + filter_form("/runs", [select("mode", "Mode", ("apply", "plan"), mode)])
            + RUNS_TABLE.render([run_cells(r, now) for r in page.take(runs)], empty=no_match("/runs"))
            + pager("/runs", params, page)
        )
        return 200, self.layout("Runs", body, "/runs")

    def run_detail(self, run_id: str, params: Mapping[str, str]) -> Response:
        summary = self.reports.summary(run_id)
        rows = self.reports.rows(run_id) if summary else None
        if summary is None or rows is None:
            return 404, self.layout(
                "Run not found",
                "<h1>Run not found</h1><p>That report doesn't exist or can't be read; only the newest "
                f"{KEEP_REPORTS} are kept.</p>",
                "/runs",
            )
        filters = {k: params.get(k, "") for k in ("rule", "status", "reason", "aid", "item")}
        shown = [r for r in rows if row_matches(r, filters)]
        page = Page.of(params, len(shown))
        path = f"/runs/{run_id}"
        info = [
            ("started", esc(fmt_time(summary.started))),
            ("mode", esc(summary.mode)),
            ("scope", esc(summary.scope_label)),
            ("snapshot", badge(summary.snapshot) if summary.snapshot else '<span class="muted">?</span>'),
            ("rows", counts_html(summary.statuses)),
            ("download", f'<a href="{path}.csv" download>CSV report</a>'),
        ]
        form = filter_form(
            path,
            [
                select("rule", "Rule", sorted({r["rule"] for r in rows}), filters["rule"]),
                select("status", "Status", sorted({r["status"] for r in rows}), filters["status"]),
                select("reason", "Reason", sorted({r["reason"] for r in rows}), filters["reason"]),
                text_input("aid", "Anime (aid)", filters["aid"]),
                text_input("item", "Any id", filters["item"]),
            ],
        )
        body = (
            f"<h1>Run {esc(run_id)}</h1>"
            + card("Run", info)
            + "<h2>Rows</h2>"
            + '<p class="note">"Any id" matches a Plex rating key, a Shoko file id or an AniDB fid. '
            f"{len(shown)} of {len(rows)} rows match.</p>"
            + form
            + REPORT_TABLE.render([report_cells(r) for r in page.take(shown)], empty=no_match(path))
            + pager(path, params, page)
        )
        return 200, self.layout(f"Run {run_id}", body, "/runs")

    # Reported items

    def reported(self, params: Mapping[str, str]) -> Response:
        full = self.reports.full_runs()
        if not full:
            body = "<h1>Reported items</h1><p>No complete run report yet.</p>"
            return 200, self.layout("Reported", body, "/reported")
        latest, previous = full[0], (full[1] if len(full) > 1 else None)
        chosen = params.get("reason", "")
        intro = (
            "<h1>Reported items</h1>"
            f'<p class="note">From the latest full run, {run_link(latest.run_id)} '
            f"({esc(fmt_time(latest.started))}). pxar never writes these; each needs a fix outside pxar or none "
            "at all. The change column compares with the previous full run"
            + (f" ({run_link(previous.run_id)})" if previous else "")
            + ".</p>"
        )
        counts = reason_changes(latest, previous)
        selected = next((i for i, c in enumerate(counts) if c.reason == chosen), -1)
        body = intro + REASONS_TABLE.render([reason_cells(c) for c in counts], selected=selected)
        if chosen:
            body += self._reason_items(latest.run_id, chosen, params)
        return 200, self.layout("Reported", body, "/reported")

    def _reason_items(self, run_id: str, reason: str, params: Mapping[str, str]) -> str:
        rows = self.reports.rows(run_id) or []
        picked = [r for r in rows if r["status"] == Status.REPORTED and r["reason"] == reason]
        picked.sort(key=lambda r: (r["aid"] == "", r["aid"].zfill(10), r["release_group"], r["rating_key"]))
        page = Page.of(params, len(picked))
        in_run = link(f"/runs/{run_id}", {"status": Status.REPORTED, "reason": reason})
        items = [report_cells(r)[ITEM_COLUMNS:] for r in page.take(picked)]
        return (
            f"<h2>{esc(reason)}</h2>"
            f'<p class="note">{len(picked)} items, sorted by anime and release group. '
            f'<a href="{in_run}">Filter these in the run view</a></p>'
            + REASON_ITEMS_TABLE.render(items)
            + pager("/reported", params, page)
        )

    # Ledger

    def ledger(self, params: Mapping[str, str]) -> Response:
        filters = {k: params[k] for k in LEDGER_FILTERS if params.get(k)}
        form = filter_form(
            "/ledger",
            [
                select("status", "Status", LEDGER_STATUSES, params.get("status", "")),
                select("rule", "Rule", EXECUTABLE_RULES, params.get("rule", "")),
                text_input("run_id", "Run", params.get("run_id", "")),
                text_input("rating_key", "Plex item", params.get("rating_key", "")),
                text_input("file_id", "Shoko file", params.get("file_id", "")),
            ],
        )
        note = (
            '<p class="note">Every write pxar attempted, newest first. A <span class="bad">pending</span> R1 row '
            "means the service stopped after recording the R1 and before recording how the scrobble went, so Plex may "
            "or may not have it. Failed writes are retried on the next run.</p>"
        )
        try:
            total = self.ledger_view.count(filters)
            page = Page.of(params, total)
            rows = self.ledger_view.rows(filters, page.offset)
        except LedgerUnavailable as exc:
            return 200, self.layout("Ledger", f"<h1>Ledger</h1>{note}<p>{esc(exc)}</p>", "/ledger")
        body = (
            f"<h1>Ledger</h1>{note}{form}"
            f'<p class="note">{total} rows match.</p>'
            + LEDGER_TABLE.render([ledger_cells(r) for r in rows], empty=no_match("/ledger"))
            + pager("/ledger", params, page)
        )
        return 200, self.layout("Ledger", body, "/ledger")


# --- HTTP -------------------------------------------------------------------------------------------------------

_RUN_PATH = re.compile(r"/runs/(" + RUN_ID.pattern + r")")
_RUN_CSV_PATH = re.compile(r"/runs/(" + RUN_ID.pattern + r")\.csv")
# Page methods by path, looked up on each request.
ROUTES = {"/": "status", "/runs": "runs", "/reported": "reported", "/ledger": "ledger"}


@dataclass(frozen=True)
class Reply:
    code: int
    body: bytes
    content_type: str = "text/html; charset=utf-8"
    headers: Mapping[str, str] = field(default_factory=dict)


UNAUTHORIZED = Reply(
    401,
    b"authentication required\n",
    "text/plain; charset=utf-8",
    {"WWW-Authenticate": 'Basic realm="pxar", charset="UTF-8"'},
)
SERVER_ERROR = Reply(
    500, b"<!doctype html><title>Error</title><h1>Something went wrong</h1><p>See the service log.</p>\n"
)


def respond(pages: Pages, path: str, params: Mapping[str, str]) -> Reply:
    """The reply to an authorized GET of ``path`` (already stripped of its query and trailing slash)."""
    if path == "/style.css":
        return Reply(200, _STYLE.encode(), "text/css; charset=utf-8")
    if path in ROUTES:
        code, page = getattr(pages, ROUTES[path])(params)
    elif m := _RUN_CSV_PATH.fullmatch(path):
        data = pages.run_csv(m.group(1))
        if data is not None:
            disposition = f"attachment; filename={quote(f'pxar-{m.group(1)}.csv')}"
            return Reply(200, data, "text/csv; charset=utf-8", {"Content-Disposition": disposition})
        code, page = pages.not_found("Report")
    elif m := _RUN_PATH.fullmatch(path):
        code, page = pages.run_detail(m.group(1), params)
    else:
        code, page = pages.not_found()
    return Reply(code, page.encode())


def make_ui_server(pages: Pages, token: str, host: str, port: int) -> BoundedServer:
    class Handler(QuietHandler):
        def _reply(self) -> Reply:
            if not authorized(self.headers.get("Authorization"), token):
                return UNAUTHORIZED
            url = urlsplit(self.path)
            try:
                return respond(pages, url.path.rstrip("/") or "/", query_params(url.query))
            except Exception as exc:  # noqa: BLE001 - a broken page must not kill the handler or leak details
                log.error("web UI request failed: %s", type(exc).__name__)
                return SERVER_ERROR

        def _send(self, reply: Reply) -> None:
            try:
                self.send_response(reply.code)
                self.send_header("Content-Type", reply.content_type)
                self.send_header("Content-Length", str(len(reply.body)))
                for name, value in {**_SECURITY_HEADERS, **reply.headers}.items():
                    self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(reply.body)
            except OSError:  # the client went away mid-reply: there is no one left to tell
                self.close_connection = True

        def do_GET(self) -> None:
            self._send(self._reply())

        def do_HEAD(self) -> None:
            self._send(self._reply())

    return bind(host, port, Handler)
