"""Map Plex file paths to Shoko files."""

from collections.abc import Iterable
from dataclasses import dataclass

from plex_anidb_reconciler.models import ShokoFile


@dataclass(frozen=True)
class Ambiguous:
    candidates: tuple[int, ...]


@dataclass(frozen=True)
class Match:
    """One Shoko file for a Plex path. ``path`` is the Shoko absolute path the PATH_MAP-rewritten Plex
    path equals exactly; ``None`` means the hit came only from the parent-dir + filename fallback, which
    can pick the same-named episode of another show, so it must never drive a write."""

    file: ShokoFile
    path: str | None


def _key(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-2:]).casefold()


class PathMatcher:
    def __init__(self, files: Iterable[ShokoFile], path_map: Iterable[tuple[str, str]]):
        self._map = sorted(((a.rstrip("/"), b.rstrip("/")) for a, b in path_map), key=lambda m: -len(m[0]))
        self._exact: dict[str, dict[int, ShokoFile]] = {}
        self._by_key: dict[str, dict[int, ShokoFile]] = {}
        for f in files:
            for p in f.abs_paths:
                self._exact.setdefault(p, {})[f.file_id] = f
                self._by_key.setdefault(_key(p), {})[f.file_id] = f

    def _rewrite(self, path: str) -> str:
        for src, dst in self._map:
            if path == src or path.startswith(src + "/"):
                return dst + path[len(src) :]
        return path

    def lookup(self, plex_path: str) -> Match | Ambiguous | None:
        rewritten = self._rewrite(plex_path)
        exact = self._exact.get(rewritten)
        cands = exact or self._by_key.get(_key(plex_path), {})
        if len(cands) > 1:
            return Ambiguous(tuple(sorted(cands)))
        if not cands:
            return None
        return Match(next(iter(cands.values())), rewritten if exact else None)
