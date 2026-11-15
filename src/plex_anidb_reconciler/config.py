"""Environment-driven settings. Error messages never include values."""

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from plex_anidb_reconciler.models import R2bPolicy

# Planning reads (the Shoko file index can be large and slow to build) wait up to 60 s for data.
PLAN_TIMEOUT = httpx.Timeout(connect=10, read=60, write=30, pool=10)
# Every request on the write path (the re-reads right before a write, the Shoko PUT, the Plex scrobble) takes
# typically up to pool + connect + write + read = 25 s, which the default SHUTDOWN_GRACE_S of 25 s covers. httpx applies
# each limit per phase and the read limit per chunk, so this is a typical bound, not a hard deadline.
WRITE_PATH_TIMEOUT = httpx.Timeout(connect=5, read=10, write=5, pool=5)

UI_TOKEN_MIN_LEN = 32  # e.g. `openssl rand -hex 32` gives 64

REQUIRED = ("SHOKO_URL", "SHOKO_API_KEY", "PLEX_URL", "PLEX_TOKEN", "PLEX_SECTIONS")


def _is_ascii_int(raw: str) -> bool:
    """ASCII decimal digits with an optional leading '-': no '+5', '1_000' or Unicode digits."""
    digits = raw.removeprefix("-")
    return digits.isascii() and digits.isdigit()


def _int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    """Blank or unset -> default. Only plain decimal integers >= minimum are accepted."""
    raw = (env.get(name) or "").strip()
    if raw == "":
        return default
    if not _is_ascii_int(raw):
        raise ValueError(f"{name} must be an integer")
    value = int(raw, 10)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _parse_sections(raw: str) -> tuple[int, ...]:
    """Comma-separated positive section ids; empty lists and empty parts are rejected."""
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not _is_ascii_int(part) or int(part, 10) < 1:
            raise ValueError("PLEX_SECTIONS must be comma-separated positive integers")
        value = int(part, 10)
        if value not in out:
            out.append(value)
    return tuple(out)


def _parse_path_map(raw: str) -> tuple[tuple[str, str], ...]:
    """``plexprefix=shokoprefix[;...]``; sides are stripped, blank segments (e.g. a trailing ';') ignored."""
    pairs: list[tuple[str, str]] = []
    for part in raw.split(";"):
        if not part.strip():
            continue
        if part.count("=") != 1:
            raise ValueError("PATH_MAP must be plexprefix=shokoprefix[;...]")
        plex, _, shoko = (s.strip() for s in part.partition("="))
        if not plex or not shoko:
            raise ValueError("PATH_MAP must be plexprefix=shokoprefix[;...]")
        pairs.append((plex, shoko))
    return tuple(pairs)


def parse_listen(raw: str) -> tuple[str, int]:
    """``host:port`` or ``[ipv6]:port`` (port 1-65535). An empty host means 0.0.0.0."""
    raw = raw.strip()
    bad = ValueError("LISTEN must be host:port or [ipv6]:port with port 1-65535")
    if raw.startswith("["):
        host, sep, rest = raw[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise bad
        port_s = rest[1:]
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise bad from None
    else:
        host, sep, port_s = raw.rpartition(":")
        if not sep or ":" in host:  # a bare IPv6 literal needs brackets
            raise bad
        host = host or "0.0.0.0"
    if not _is_ascii_int(port_s) or not 1 <= int(port_s) <= 65535:
        raise bad
    return host, int(port_s)


def _ui_settings(env: Mapping[str, str], listen: str) -> tuple[str, str]:
    """UI_LISTEN (empty: no web UI) and UI_TOKEN. The UI needs its own port, so exposing it never exposes the
    unauthenticated /trigger, and a token of at least UI_TOKEN_MIN_LEN characters."""
    ui_listen = (env.get("UI_LISTEN") or "").strip()
    if not ui_listen:
        return "", ""
    try:
        _, ui_port = parse_listen(ui_listen)
    except ValueError:
        raise ValueError("UI_LISTEN must be host:port or [ipv6]:port with port 1-65535") from None
    if ui_port == parse_listen(listen)[1]:
        raise ValueError("UI_LISTEN must use a different port than LISTEN")
    token = (env.get("UI_TOKEN") or "").strip()
    if len(token) < UI_TOKEN_MIN_LEN:
        raise ValueError(f"UI_TOKEN must be set to at least {UI_TOKEN_MIN_LEN} characters when UI_LISTEN is set")
    if not token.isascii() or not token.isprintable():
        # HTTP headers arrive as latin-1: a non-ASCII token could never match as a Bearer token
        raise ValueError("UI_TOKEN must be printable ASCII")
    return ui_listen, token


@dataclass(frozen=True)
class Settings:
    shoko_url: str
    shoko_api_key: str = field(repr=False)
    plex_url: str
    plex_token: str = field(repr=False)
    shoko_tz: ZoneInfo
    plex_sections: tuple[int, ...]
    mylist_path: str
    data_dir: str
    path_map: tuple[tuple[str, str], ...]
    r2b_policy: R2bPolicy
    snapshot_max_age_h: int
    max_writes: int
    poll_minutes: int
    shoko_index_ttl_min: int  # 0 = no cache (fetch the Shoko index every run)
    listen: str
    shutdown_grace_s: int = 25
    run_stale_minutes: int = 360  # an active run longer than this makes /healthz stale
    health_max_failed_runs: int = 3  # this many failed runs in a row make /healthz return 503
    ui_listen: str = ""  # read-only web UI address; empty = no UI
    ui_token: str = field(default="", repr=False)  # required with ui_listen

    @property
    def reports_dir(self) -> Path:
        return Path(self.data_dir) / "reports"

    @property
    def ledger_path(self) -> Path:
        return Path(self.data_dir) / "ledger.sqlite"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        required = {k: (env.get(k) or "").strip() for k in REQUIRED}
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError("missing required environment variables: " + ", ".join(missing))

        def opt(name: str, default: str) -> str:
            return (env.get(name) or "").strip() or default

        try:
            policy = R2bPolicy(opt("R2B_POLICY", R2bPolicy.RECORD))
        except ValueError:
            raise ValueError("R2B_POLICY must be 'record' or 'ignore'") from None

        tz_name = env.get("SHOKO_TZ")
        tz_name = "UTC" if tz_name is None else tz_name.strip()
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError("SHOKO_TZ is not a valid timezone") from None

        listen = opt("LISTEN", "0.0.0.0:8787")
        parse_listen(listen)
        ui_listen, ui_token = _ui_settings(env, listen)

        return cls(
            shoko_url=required["SHOKO_URL"],
            shoko_api_key=required["SHOKO_API_KEY"],
            plex_url=required["PLEX_URL"],
            plex_token=required["PLEX_TOKEN"],
            shoko_tz=tz,
            plex_sections=_parse_sections(required["PLEX_SECTIONS"]),
            mylist_path=opt("MYLIST_PATH", "/mylist/mylist.json"),
            data_dir=opt("DATA_DIR", "/data"),
            path_map=_parse_path_map(env.get("PATH_MAP", "")),
            r2b_policy=policy,
            snapshot_max_age_h=_int(env, "SNAPSHOT_MAX_AGE_H", 26, 1),
            max_writes=_int(env, "MAX_WRITES", 500, 1),
            poll_minutes=_int(env, "POLL_MINUTES", 15, 1),
            shoko_index_ttl_min=_int(env, "SHOKO_INDEX_TTL_MIN", 60, 0),
            listen=listen,
            shutdown_grace_s=_int(env, "SHUTDOWN_GRACE_S", 25, 0),
            run_stale_minutes=_int(env, "RUN_STALE_MINUTES", 360, 1),
            health_max_failed_runs=_int(env, "HEALTH_MAX_FAILED_RUNS", 3, 1),
            ui_listen=ui_listen,
            ui_token=ui_token,
        )
