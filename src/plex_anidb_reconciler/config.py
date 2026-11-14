"""Environment-driven settings. Error messages never include values."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REQUIRED = ("SHOKO_URL", "SHOKO_API_KEY", "PLEX_URL", "PLEX_TOKEN")


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def _parse_sections(raw: str) -> list[int]:
    try:
        return [int(p) for p in raw.split(",") if p.strip()]
    except ValueError:
        raise ValueError("PLEX_SECTIONS must be comma-separated integers") from None


def _parse_path_map(raw: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in raw.split(";"):
        if not part.strip():
            continue
        plex, sep, shoko = part.partition("=")
        if not sep or not plex or not shoko:
            raise ValueError("PATH_MAP must be plexprefix=shokoprefix[;...]")
        pairs.append((plex, shoko))
    return pairs


@dataclass(frozen=True)
class Settings:
    shoko_url: str
    shoko_api_key: str = field(repr=False)
    plex_url: str
    plex_token: str = field(repr=False)
    shoko_tz: ZoneInfo
    plex_sections: list[int]
    mylist_path: str
    data_dir: str
    path_map: list[tuple[str, str]]
    r2b_policy: str
    snapshot_max_age_h: int
    max_writes: int
    poll_minutes: int
    shoko_index_ttl_min: int
    listen: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        missing = [k for k in REQUIRED if not env.get(k)]
        if missing:
            raise ValueError("missing required environment variables: " + ", ".join(missing))

        policy = env.get("R2B_POLICY", "record")
        if policy not in ("record", "ignore"):
            raise ValueError("R2B_POLICY must be 'record' or 'ignore'")

        tz_name = env.get("SHOKO_TZ", "America/New_York")
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError("SHOKO_TZ is not a valid timezone") from None

        return cls(
            shoko_url=env["SHOKO_URL"],
            shoko_api_key=env["SHOKO_API_KEY"],
            plex_url=env["PLEX_URL"],
            plex_token=env["PLEX_TOKEN"],
            shoko_tz=tz,
            plex_sections=_parse_sections(env.get("PLEX_SECTIONS", "4,5")),
            mylist_path=env.get("MYLIST_PATH", "/mylist/mylist.json"),
            data_dir=env.get("DATA_DIR", "/data"),
            path_map=_parse_path_map(env.get("PATH_MAP", "")),
            r2b_policy=policy,
            snapshot_max_age_h=_int(env, "SNAPSHOT_MAX_AGE_H", 26),
            max_writes=_int(env, "MAX_WRITES", 500),
            poll_minutes=_int(env, "POLL_MINUTES", 15),
            shoko_index_ttl_min=_int(env, "SHOKO_INDEX_TTL_MIN", 60),
            listen=env.get("LISTEN", "0.0.0.0:8787"),
        )
