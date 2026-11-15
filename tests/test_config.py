import pytest

from plex_anidb_reconciler.config import Settings

BASE = {
    "SHOKO_URL": "http://shoko:8111",
    "SHOKO_API_KEY": "shoko-secret",
    "PLEX_URL": "http://plex:32400",
    "PLEX_TOKEN": "plex-secret",
    "PLEX_SECTIONS": "3,7",
}


def test_defaults():
    s = Settings.from_env(BASE)
    assert s.plex_sections == (3, 7)
    assert s.r2b_policy == "record"
    assert s.max_writes == 500
    assert s.snapshot_max_age_h == 26
    assert s.shoko_tz.key == "UTC"
    assert s.mylist_path == "/mylist/mylist.json"
    assert s.data_dir == "/data"
    assert s.path_map == ()
    assert s.poll_minutes == 15
    assert s.shoko_index_ttl_min == 60
    assert s.listen == "0.0.0.0:8787"
    assert s.shutdown_grace_s == 25
    assert s.run_stale_minutes == 360
    assert s.health_max_failed_runs == 3


def test_path_map_parsing():
    s = Settings.from_env({**BASE, "PATH_MAP": "/plex/anime=/shoko/anime;/x=/y"})
    assert s.path_map == (("/plex/anime", "/shoko/anime"), ("/x", "/y"))


def test_missing_secret_raises():
    env = {k: v for k, v in BASE.items() if k != "PLEX_TOKEN"}
    with pytest.raises(ValueError) as exc:
        Settings.from_env(env)
    msg = str(exc.value)
    assert "PLEX_TOKEN" in msg
    for value in env.values():
        assert value not in msg


@pytest.mark.parametrize("value", [None, "", "   "])
def test_plex_sections_is_required(value):
    env = {k: v for k, v in BASE.items() if k != "PLEX_SECTIONS"}
    if value is not None:
        env["PLEX_SECTIONS"] = value
    with pytest.raises(ValueError, match="PLEX_SECTIONS"):
        Settings.from_env(env)


def test_invalid_r2b_policy_raises():
    with pytest.raises(ValueError, match="R2B_POLICY"):
        Settings.from_env({**BASE, "R2B_POLICY": "bogus"})


def test_overrides():
    s = Settings.from_env({**BASE, "PLEX_SECTIONS": "1, 2", "SHOKO_TZ": "Europe/Berlin", "MAX_WRITES": "5"})
    assert s.plex_sections == (1, 2)
    assert s.shoko_tz.key == "Europe/Berlin"
    assert s.max_writes == 5


def test_repr_hides_secrets():
    text = repr(Settings.from_env(BASE))
    assert BASE["SHOKO_API_KEY"] not in text
    assert BASE["PLEX_TOKEN"] not in text


def _err(**over):
    with pytest.raises(ValueError) as exc:
        Settings.from_env({**BASE, **over})
    msg = str(exc.value)
    for secret in (BASE["SHOKO_API_KEY"], BASE["PLEX_TOKEN"]):
        assert secret not in msg
    return msg


@pytest.mark.parametrize("name", ["SHOKO_URL", "SHOKO_API_KEY", "PLEX_URL", "PLEX_TOKEN"])
def test_whitespace_only_required_var_is_missing(name):
    assert name in _err(**{name: "   "})


def test_required_values_are_stripped():
    s = Settings.from_env({**BASE, "SHOKO_URL": " http://shoko:8111\n"})
    assert s.shoko_url == "http://shoko:8111"


@pytest.mark.parametrize("raw", ["", "  ", ",", "4,,5", "4,x", "0", "-4", "4.5"])
def test_bad_plex_sections_rejected(raw):
    assert "PLEX_SECTIONS" in _err(PLEX_SECTIONS=raw)


def test_plex_sections_deduplicated():
    assert Settings.from_env({**BASE, "PLEX_SECTIONS": "4, 5,4"}).plex_sections == (4, 5)


def test_path_map_parts_are_stripped():
    s = Settings.from_env({**BASE, "PATH_MAP": " /plex/anime = /shoko/anime ; /x=/y ;"})
    assert s.path_map == (("/plex/anime", "/shoko/anime"), ("/x", "/y"))


@pytest.mark.parametrize("raw", ["/a", "=/b", "/a=", " = /b", "/a= ", "/a=/b=/c", "/a=/b;junk"])
def test_malformed_path_map_rejected(raw):
    assert "PATH_MAP" in _err(PATH_MAP=raw)


@pytest.mark.parametrize("name", ["MAX_WRITES", "POLL_MINUTES", "SNAPSHOT_MAX_AGE_H"])
@pytest.mark.parametrize("raw", ["0", "-1"])
def test_non_positive_ints_rejected(name, raw):
    assert name in _err(**{name: raw})


@pytest.mark.parametrize(
    "name", ["MAX_WRITES", "POLL_MINUTES", "SNAPSHOT_MAX_AGE_H", "SHOKO_INDEX_TTL_MIN", "SHUTDOWN_GRACE_S"]
)
@pytest.mark.parametrize("raw", ["ten", "1.5", "0x10"])
def test_non_int_values_rejected(name, raw):
    assert name in _err(**{name: raw})


def test_ttl_zero_means_no_cache_and_negative_rejected():
    assert Settings.from_env({**BASE, "SHOKO_INDEX_TTL_MIN": "0"}).shoko_index_ttl_min == 0
    assert "SHOKO_INDEX_TTL_MIN" in _err(SHOKO_INDEX_TTL_MIN="-1")


def test_shutdown_grace():
    assert Settings.from_env({**BASE, "SHUTDOWN_GRACE_S": "0"}).shutdown_grace_s == 0
    assert "SHUTDOWN_GRACE_S" in _err(SHUTDOWN_GRACE_S="-1")


@pytest.mark.parametrize("raw", ["Mars/Base", "   ", "../etc/passwd"])
def test_invalid_shoko_tz_rejected(raw):
    assert "SHOKO_TZ" in _err(SHOKO_TZ=raw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.0.0.0:8787", ("0.0.0.0", 8787)),
        ("127.0.0.1:1", ("127.0.0.1", 1)),
        ("localhost:65535", ("localhost", 65535)),
        ("[::]:8787", ("::", 8787)),
        ("[::1]:80", ("::1", 80)),
        (":8787", ("0.0.0.0", 8787)),
    ],
)
def test_listen_accepted(raw, expected):
    from plex_anidb_reconciler.config import parse_listen

    assert Settings.from_env({**BASE, "LISTEN": raw}).listen == raw
    assert parse_listen(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "8787",
        "host",
        "host:",
        "host:0",
        "host:65536",
        "host:-1",
        "host:http",
        "::1:8787",
        "[::1]8787",
        "[zz]:80",
        "[::1]:",
        "host:\u0665",
        "[::1]:\uff18",
        "host:+80",
        "host:8_0",
    ],
)
def test_listen_rejected(raw):
    assert "LISTEN" in _err(LISTEN=raw)


def test_r2b_policy_is_stripped():
    assert Settings.from_env({**BASE, "R2B_POLICY": " ignore "}).r2b_policy == "ignore"


@pytest.mark.parametrize("raw", ["1_000", "+5", "\u0665", "\uff15", " 5_0 "])
def test_int_rejects_underscores_plus_and_unicode_digits(raw):
    assert "MAX_WRITES" in _err(MAX_WRITES=raw)


def test_int_accepts_plain_digits_and_negative_gives_range_error():
    assert "must be >=" in _err(MAX_WRITES="-3")


def test_run_stale_minutes_override_and_validation():
    assert Settings.from_env({**BASE, "RUN_STALE_MINUTES": "90"}).run_stale_minutes == 90
    for raw in ("0", "-5", "1_0", "x"):
        assert "RUN_STALE_MINUTES" in _err(RUN_STALE_MINUTES=raw)


@pytest.mark.parametrize("raw", ["+3", "1_0", "٥", "５", "3,+7", "3,1_0"])
def test_plex_sections_reject_non_ascii_decimal(raw):
    assert "PLEX_SECTIONS" in _err(PLEX_SECTIONS=raw)


def test_health_max_failed_runs_override_and_validation():
    assert Settings.from_env({**BASE, "HEALTH_MAX_FAILED_RUNS": "5"}).health_max_failed_runs == 5
    for raw in ("0", "-1", "1_0", "+2", "x"):
        assert "HEALTH_MAX_FAILED_RUNS" in _err(HEALTH_MAX_FAILED_RUNS=raw)


# --- web UI ---

UI_TOKEN = "t" * 32


def test_ui_is_off_by_default_and_ignores_a_lone_token():
    s = Settings.from_env({**BASE, "UI_TOKEN": UI_TOKEN})
    assert (s.ui_listen, s.ui_token) == ("", "")


def test_ui_settings_accepted_and_token_kept_out_of_repr():
    s = Settings.from_env({**BASE, "UI_LISTEN": " 0.0.0.0:8788 ", "UI_TOKEN": f" {UI_TOKEN} "})
    assert (s.ui_listen, s.ui_token) == ("0.0.0.0:8788", UI_TOKEN)
    assert UI_TOKEN not in repr(s)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"UI_LISTEN": "nope"}, "UI_LISTEN must be host:port"),
        ({"UI_LISTEN": "127.0.0.1:8787", "UI_TOKEN": UI_TOKEN}, "UI_LISTEN must use a different port than LISTEN"),
        ({"UI_LISTEN": "[::1]:9000", "LISTEN": "0.0.0.0:9000", "UI_TOKEN": UI_TOKEN}, "different port"),
        ({"UI_LISTEN": "0.0.0.0:8788"}, "UI_TOKEN must be set to at least 32 characters"),
        ({"UI_LISTEN": "0.0.0.0:8788", "UI_TOKEN": "t" * 31}, "UI_TOKEN must be set to at least 32"),
        ({"UI_LISTEN": "0.0.0.0:8788", "UI_TOKEN": "é" * 32}, "UI_TOKEN must be printable ASCII"),
        ({"UI_LISTEN": "0.0.0.0:8788", "UI_TOKEN": "a\x07" * 16}, "UI_TOKEN must be printable ASCII"),
    ],
)
def test_ui_settings_rejected_without_echoing_values(env, expected):
    message = _err(**env)
    assert expected in message
    assert "t" * 31 not in message
