import pytest

from plex_anidb_reconciler.config import Settings

BASE = {
    "SHOKO_URL": "http://shoko:8111",
    "SHOKO_API_KEY": "shoko-secret",
    "PLEX_URL": "http://plex:32400",
    "PLEX_TOKEN": "plex-secret",
}


def test_defaults():
    s = Settings.from_env(BASE)
    assert s.plex_sections == (4, 5)
    assert s.r2b_policy == "record"
    assert s.max_writes == 500
    assert s.snapshot_max_age_h == 26
    assert s.shoko_tz.key == "America/New_York"
    assert s.mylist_path == "/mylist/mylist.json"
    assert s.data_dir == "/data"
    assert s.path_map == ()
    assert s.poll_minutes == 15
    assert s.shoko_index_ttl_min == 60
    assert s.listen == "0.0.0.0:8787"
    assert s.shutdown_grace_s == 25


def test_path_map_parsing():
    s = Settings.from_env({**BASE, "PATH_MAP": "/data/TV=/mnt/media/TV;/x=/y"})
    assert s.path_map == (("/data/TV", "/mnt/media/TV"), ("/x", "/y"))


def test_missing_secret_raises():
    env = {k: v for k, v in BASE.items() if k != "PLEX_TOKEN"}
    with pytest.raises(ValueError) as exc:
        Settings.from_env(env)
    msg = str(exc.value)
    assert "PLEX_TOKEN" in msg
    for value in env.values():
        assert value not in msg


def test_invalid_r2b_policy_raises():
    with pytest.raises(ValueError, match="R2B_POLICY"):
        Settings.from_env({**BASE, "R2B_POLICY": "bogus"})


def test_overrides():
    s = Settings.from_env({**BASE, "PLEX_SECTIONS": "1, 2", "SHOKO_TZ": "UTC", "MAX_WRITES": "5"})
    assert s.plex_sections == (1, 2)
    assert s.shoko_tz.key == "UTC"
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
    s = Settings.from_env({**BASE, "PATH_MAP": " /data/TV = /mnt/media/TV ; /x=/y ;"})
    assert s.path_map == (("/data/TV", "/mnt/media/TV"), ("/x", "/y"))


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
    ],
)
def test_listen_rejected(raw):
    assert "LISTEN" in _err(LISTEN=raw)


def test_r2b_policy_is_stripped():
    assert Settings.from_env({**BASE, "R2B_POLICY": " ignore "}).r2b_policy == "ignore"
