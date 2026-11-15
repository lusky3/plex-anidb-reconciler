from pathlib import Path

import pytest

from plex_anidb_reconciler import cli

ENV = {
    "SHOKO_URL": "http://shoko",
    "SHOKO_API_KEY": "k",
    "PLEX_URL": "http://plex",
    "PLEX_TOKEN": "t",
    "PLEX_SECTIONS": "3",
}


class _Result:
    def __init__(self) -> None:
        self.counts: dict = {}
        self.report_path = Path("/tmp/report.csv")
        self.snapshot = "ok"
        self.detail = ""


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_run_once(settings, **kwargs):
        calls.append(kwargs)
        return _Result()

    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "run_once", fake_run_once)
    return calls


def test_rating_key_scopes_the_run(captured):
    assert cli.main(["apply", "--rules", "R1", "--rating-key", "123", "--limit", "1"]) == 0
    assert captured[0]["rating_keys"] == frozenset({"123"})


def test_rating_key_can_repeat(captured):
    assert cli.main(["plan", "--rating-key", "1", "--rating-key", "2"]) == 0
    assert captured[0]["rating_keys"] == frozenset({"1", "2"})


def test_without_rating_key_the_run_is_full(captured):
    assert cli.main(["plan"]) == 0
    assert captured[0]["rating_keys"] is None
