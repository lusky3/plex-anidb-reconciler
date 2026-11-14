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
    assert s.plex_sections == [4, 5]
    assert s.r2b_policy == "record"
    assert s.max_writes == 500
    assert s.snapshot_max_age_h == 26
    assert s.shoko_tz.key == "America/New_York"
    assert s.mylist_path == "/mylist/mylist.json"
    assert s.data_dir == "/data"
    assert s.path_map == []
    assert s.poll_minutes == 15
    assert s.shoko_index_ttl_min == 60
    assert s.listen == "0.0.0.0:8787"


def test_path_map_parsing():
    s = Settings.from_env({**BASE, "PATH_MAP": "/data/TV=/mnt/media/TV;/x=/y"})
    assert s.path_map == [("/data/TV", "/mnt/media/TV"), ("/x", "/y")]


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
    assert s.plex_sections == [1, 2]
    assert s.shoko_tz.key == "UTC"
    assert s.max_writes == 5


def test_repr_hides_secrets():
    text = repr(Settings.from_env(BASE))
    assert BASE["SHOKO_API_KEY"] not in text
    assert BASE["PLEX_TOKEN"] not in text
