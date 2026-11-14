from plex_anidb_reconciler.models import ShokoFile
from plex_anidb_reconciler.paths import Ambiguous, PathMatcher


def sf(file_id, *paths):
    return ShokoFile(file_id, None, (), False, tuple(paths))


MAP = [("/data/TV", "/mnt/media/TV")]


def test_exact_match_with_prefix_map():
    f = sf(1, "/mnt/media/TV/tv_anime/A/Season 01/x.mkv")
    assert PathMatcher([f], MAP).match("/data/TV/tv_anime/A/Season 01/x.mkv") == f


def test_exact_match_is_component_bounded_and_longest_wins():
    f = sf(1, "/long/A/x.mkv")
    m = PathMatcher([f], [("/data", "/short"), ("/data/TV", "/long")])
    assert m.match("/data/TV/A/x.mkv") == f
    g = sf(2, "/mnt/media/TVX/a/x.mkv")
    # /data/TV must not rewrite /data/TVX/...; falls back on parent/filename
    assert PathMatcher([g], MAP).match("/data/TVX/a/x.mkv") == g


def test_fallback_unique_parent_filename():
    f = sf(1, "/other/root/Season 01/X.mkv")
    assert PathMatcher([f], MAP).match("/data/TV/zzz/season 01/x.MKV") == f


def test_fallback_collision_returns_ambiguous():
    a, b = sf(5, "/r1/Season 01/x.mkv"), sf(3, "/r2/Season 01/x.mkv")
    assert PathMatcher([a, b], MAP).match("/nowhere/Season 01/x.mkv") == Ambiguous((3, 5))


def test_same_file_twice_under_key_is_unique():
    f = sf(1, "/r1/Season 01/x.mkv", "/r2/Season 01/x.mkv")
    assert PathMatcher([f], MAP).match("/nowhere/Season 01/x.mkv") == f


def test_exact_match_case_sensitive_but_fallback_rescues():
    f = sf(1, "/mnt/media/TV/A/X.mkv")
    assert PathMatcher([f], []).match("/mnt/media/TV/A/x.mkv") == f  # via fallback


def test_no_match_returns_none():
    f = sf(1, "/r1/Season 01/x.mkv")
    assert PathMatcher([f], MAP).match("/data/TV/Q/y.mkv") is None
