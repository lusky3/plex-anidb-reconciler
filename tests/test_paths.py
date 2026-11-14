from plex_anidb_reconciler.models import ShokoFile
from plex_anidb_reconciler.paths import Ambiguous, Match, PathMatcher


def sf(file_id, *paths):
    return ShokoFile(file_id, None, (), False, tuple(paths))


MAP = [("/plex/anime", "/shoko/anime")]


def test_exact_match_with_prefix_map():
    # Both files share the fallback key, so only the rewritten exact hit can pick one.
    f = sf(1, "/shoko/anime/A/Season 01/x.mkv")
    other = sf(2, "/elsewhere/A/Season 01/x.mkv")
    m = PathMatcher([f, other], MAP)
    assert m.lookup("/plex/anime/A/Season 01/x.mkv") == Match(f, "/shoko/anime/A/Season 01/x.mkv")


def test_exact_hit_beats_fallback_collision():
    a, b = sf(1, "/r1/Season 01/x.mkv"), sf(2, "/r2/Season 01/x.mkv")
    assert PathMatcher([a, b], [("/p", "/r2")]).lookup("/p/Season 01/x.mkv") == Match(b, "/r2/Season 01/x.mkv")


def test_longest_prefix_wins():
    long_f, short_f = sf(1, "/long/A/x.mkv"), sf(2, "/short/anime/A/x.mkv")
    m = PathMatcher([long_f, short_f], [("/plex", "/short"), ("/plex/anime", "/long")])
    assert m.lookup("/plex/anime/A/x.mkv") == Match(long_f, "/long/A/x.mkv")


def test_prefix_matches_only_on_component_boundary():
    literal, decoy = sf(1, "/plex/animeX/a/x.mkv"), sf(2, "/shoko/animeX/a/x.mkv")
    m = PathMatcher([literal, decoy], [("/plex/anime", "/shoko/anime")])
    assert m.lookup("/plex/animeX/a/x.mkv") == Match(literal, "/plex/animeX/a/x.mkv")


def test_fallback_unique_parent_filename():
    f = sf(1, "/other/root/Season 01/X.mkv")
    assert PathMatcher([f], MAP).lookup("/plex/anime/zzz/season 01/x.MKV") == Match(f, None)


def test_fallback_collision_returns_ambiguous():
    a, b = sf(5, "/r1/Season 01/x.mkv"), sf(3, "/r2/Season 01/x.mkv")
    assert PathMatcher([a, b], MAP).lookup("/nowhere/Season 01/x.mkv") == Ambiguous((3, 5))


def test_same_file_twice_under_key_is_unique():
    f = sf(1, "/r1/Season 01/x.mkv", "/r2/Season 01/x.mkv")
    assert PathMatcher([f], MAP).lookup("/nowhere/Season 01/x.mkv") == Match(f, None)


def test_exact_match_case_sensitive_but_fallback_rescues():
    f = sf(1, "/shoko/anime/A/X.mkv")
    assert PathMatcher([f], []).lookup("/shoko/anime/A/x.mkv") == Match(f, None)  # via fallback


def test_no_match_returns_none():
    f = sf(1, "/r1/Season 01/x.mkv")
    assert PathMatcher([f], MAP).lookup("/plex/anime/Q/y.mkv") is None


def test_duplicate_exact_path_is_ambiguous():
    a, b = sf(7, "/shoko/anime/A/x.mkv"), sf(3, "/shoko/anime/A/x.mkv")
    assert PathMatcher([a, b], []).lookup("/shoko/anime/A/x.mkv") == Ambiguous((3, 7))


# --- lookup() says whether a hit was exact (after PATH_MAP) or the parent+filename fallback ---


def test_lookup_exact_hit_carries_shoko_path():
    f = sf(1, "/shoko/anime/A/Season 01/x.mkv")
    assert PathMatcher([f], MAP).lookup("/plex/anime/A/Season 01/x.mkv") == Match(f, "/shoko/anime/A/Season 01/x.mkv")


def test_lookup_fallback_hit_has_no_path():
    show_a = sf(1, "/shoko/anime/Show A/Season 01/01.mkv")
    assert PathMatcher([show_a], MAP).lookup("/plex/anime/Show B/Season 01/01.mkv") == Match(show_a, None)


def test_lookup_none_and_ambiguous_unchanged():
    a, b = sf(5, "/r1/Season 01/x.mkv"), sf(3, "/r2/Season 01/x.mkv")
    m = PathMatcher([a, b], MAP)
    assert m.lookup("/nowhere/Season 01/x.mkv") == Ambiguous((3, 5))
    assert m.lookup("/nowhere/Season 02/y.mkv") is None
