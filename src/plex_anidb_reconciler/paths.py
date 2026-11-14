"""Map Plex file paths to Shoko files."""

from dataclasses import dataclass

from plex_anidb_reconciler.models import ShokoFile


@dataclass(frozen=True)
class Ambiguous:
    candidates: tuple[int, ...]


def _key(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-2:]).casefold()


class PathMatcher:
    def __init__(self, files: list[ShokoFile], path_map: list[tuple[str, str]]):
        self._map = sorted(((a.rstrip("/"), b.rstrip("/")) for a, b in path_map), key=lambda m: -len(m[0]))
        self._exact: dict[str, ShokoFile] = {}
        self._by_key: dict[str, dict[int, ShokoFile]] = {}
        for f in files:
            for p in f.abs_paths:
                self._exact.setdefault(p, f)
                self._by_key.setdefault(_key(p), {})[f.file_id] = f

    def _rewrite(self, path: str) -> str:
        for src, dst in self._map:
            if path == src or path.startswith(src + "/"):
                return dst + path[len(src) :]
        return path

    def match(self, plex_path: str) -> ShokoFile | Ambiguous | None:
        hit = self._exact.get(self._rewrite(plex_path))
        if hit is not None:
            return hit
        cands = self._by_key.get(_key(plex_path), {})
        if len(cands) == 1:
            return next(iter(cands.values()))
        if len(cands) > 1:
            return Ambiguous(tuple(sorted(cands)))
        return None
