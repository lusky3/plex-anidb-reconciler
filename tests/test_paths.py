from plex_anidb_reconciler.models import ShokoFile
from plex_anidb_reconciler.paths import Ambiguous, PathMatcher


def sf(file_id, *paths):
    return ShokoFile(file_id, None, (), False, tuple(paths))


MAP = [("/data/TV", "/mnt/media/TV")]


def test_exact_match_with_prefix_map():
    # Both files share the fallback key, so only the rewritten exact hit can pick one.
    f = sf(1, "/mnt/media/TV/tv_anime/A/Season 01/x.mkv")
    other = sf(2, "/elsewhere/A/Season 01/x.mkv")
    m = PathMatcher([f, other], MAP)
    assert m.match("/data/TV/tv_anime/A/Season 01/x.mkv") == f


def test_exact_hit_beats_fallback_collision():
    a, b = sf(1, "/r1/Season 01/x.mkv"), sf(2, "/r2/Season 01/x.mkv")
    assert PathMatcher([a, b], [("/p", "/r2")]).match("/p/Season 01/x.mkv") == b


def test_longest_prefix_wins():
    long_f, short_f = sf(1, "/long/A/x.mkv"), sf(2, "/short/TV/A/x.mkv")
    m = PathMatcher([long_f, short_f], [("/data", "/short"), ("/data/TV", "/long")])
    assert m.match("/data/TV/A/x.mkv") == long_f


def test_prefix_matches_only_on_component_boundary():
    literal, decoy = sf(1, "/data/TVX/a/x.mkv"), sf(2, "/mnt/TVX/a/x.mkv")
    m = PathMatcher([literal, decoy], [("/data/TV", "/mnt/TV")])
    assert m.match("/data/TVX/a/x.mkv") == literal


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


def test_duplicate_exact_path_is_ambiguous():
    a, b = sf(7, "/mnt/media/TV/A/x.mkv"), sf(3, "/mnt/media/TV/A/x.mkv")
    assert PathMatcher([a, b], []).match("/mnt/media/TV/A/x.mkv") == Ambiguous((3, 7))
