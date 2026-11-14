"""Environment-driven settings. Error messages never include values."""

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REQUIRED = ("SHOKO_URL", "SHOKO_API_KEY", "PLEX_URL", "PLEX_TOKEN")


def _int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    """Blank or unset -> default. Only plain decimal integers >= minimum are accepted."""
    raw = (env.get(name) or "").strip()
    if raw == "":
        return default
    digits = raw.removeprefix("-")
    if not (digits.isascii() and digits.isdigit()):  # no "+5", "1_000" or Unicode digits
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
        try:
            value = int(part, 10)
        except ValueError:
            raise ValueError("PLEX_SECTIONS must be comma-separated positive integers") from None
        if value < 1:
            raise ValueError("PLEX_SECTIONS must be comma-separated positive integers")
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
    if not port_s.isdigit() or not 1 <= int(port_s) <= 65535:
        raise bad
    return host, int(port_s)


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
    r2b_policy: str
    snapshot_max_age_h: int
    max_writes: int
    poll_minutes: int
    shoko_index_ttl_min: int  # 0 = no cache (fetch the Shoko index every run)
    listen: str
    shutdown_grace_s: int = 25

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        required = {k: (env.get(k) or "").strip() for k in REQUIRED}
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError("missing required environment variables: " + ", ".join(missing))

        def opt(name: str, default: str) -> str:
            return (env.get(name) or "").strip() or default

        policy = opt("R2B_POLICY", "record")
        if policy not in ("record", "ignore"):
            raise ValueError("R2B_POLICY must be 'record' or 'ignore'")

        tz_name = env.get("SHOKO_TZ")
        tz_name = "America/New_York" if tz_name is None else tz_name.strip()
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError("SHOKO_TZ is not a valid timezone") from None

        listen = opt("LISTEN", "0.0.0.0:8787")
        parse_listen(listen)

        sections = env.get("PLEX_SECTIONS")
        return cls(
            shoko_url=required["SHOKO_URL"],
            shoko_api_key=required["SHOKO_API_KEY"],
            plex_url=required["PLEX_URL"],
            plex_token=required["PLEX_TOKEN"],
            shoko_tz=tz,
            plex_sections=_parse_sections("4,5" if sections is None else sections),
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
        )
