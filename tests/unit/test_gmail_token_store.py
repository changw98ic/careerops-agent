"""Unit tests for the shared GmailTokenStore (dual-layer refresh).

Covers the two layers from spec ``proactive-trigger-loop``:
- scheduled layer (``refresh_now`` force-refreshes),
- send layer (``get_access_token`` returns cached token while fresh, refreshes
  near expiry, and SKIPS a refresh when one just happened).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from careerops.integrations.gmail_token_store import GmailTokenStore


def _store(
    *,
    clock,
    refresh_count: list[int],
    lifetime: float = 3600.0,
) -> GmailTokenStore:
    """Build a store with a fake refresher that counts calls."""

    def fake_refresh(**kwargs):
        refresh_count[0] += 1
        return f"tok-{refresh_count[0]}"

    store = GmailTokenStore(
        refresh_token="rt",
        client_id="cid",
        client_secret="sec",
        clock=clock,
        refresher=fake_refresh,
    )
    return store


class TestGetAccessToken:
    def test_first_call_refreshes(self) -> None:
        now = [0.0]
        count = [0]
        store = _store(clock=lambda: now[0], refresh_count=count)
        assert store.get_access_token() == "tok-1"
        assert count[0] == 1

    def test_cached_token_reused_while_fresh(self) -> None:
        now = [0.0]
        count = [0]
        store = _store(clock=lambda: now[0], refresh_count=count)
        store.get_access_token()  # tok-1
        # Still well within the 3600s lifetime (5min margin).
        now[0] = 600.0
        assert store.get_access_token() == "tok-1"
        assert count[0] == 1  # no second refresh

    def test_refreshes_when_near_expiry(self) -> None:
        now = [0.0]
        count = [0]
        store = _store(clock=lambda: now[0], refresh_count=count)
        store.get_access_token()  # tok-1, expires at 3600
        # Within the 5-minute (300s) margin -> must refresh.
        now[0] = 3400.0
        assert store.get_access_token() == "tok-2"
        assert count[0] == 2

    def test_send_layer_skips_refresh_just_after_scheduled_layer(self) -> None:
        """The scheduled layer refreshed at t=6000; a send at t=6010 must NOT
        refresh again even though the old token is near expiry."""
        now = [0.0]
        count = [0]
        store = _store(clock=lambda: now[0], refresh_count=count)
        store.get_access_token()  # tok-1, last_refresh_at=0, expires 3600
        # Simulate the scheduled layer firing later, force-refreshing.
        now[0] = 6000.0
        assert store.refresh_now() == "tok-2"  # last_refresh_at=6000, expires 9600
        assert count[0] == 2
        # A send happens 10s later — the token is fresh, but even if it were
        # near expiry, the recent-refresh window (60s) must suppress a refresh.
        now[0] = 6005.0
        assert store.get_access_token() == "tok-2"
        assert count[0] == 2  # no redundant refresh

    def test_force_refresh_always_refreshes(self) -> None:
        now = [0.0]
        count = [0]
        store = _store(clock=lambda: now[0], refresh_count=count)
        store.get_access_token()  # tok-1
        assert store.get_access_token(force_refresh=True) == "tok-2"
        assert count[0] == 2


class TestRefreshNow:
    def test_force_refresh_caches_new_token(self) -> None:
        now = [100.0]
        count = [0]
        store = _store(clock=lambda: now[0], refresh_count=count)
        assert store.refresh_now() == "tok-1"
        # Cached: a follow-up get within the fresh window returns the same.
        now[0] = 200.0
        assert store.get_access_token() == "tok-1"
        assert count[0] == 1


class TestFromTokenFile:
    def test_loads_valid_record(self, tmp_path: Path) -> None:
        token_file = tmp_path / "token.json"
        token_file.write_text(
            json.dumps(
                {
                    "refresh_token": "rt",
                    "client_id": "cid",
                    "client_secret": "sec",
                    "access_token": "old",
                }
            )
        )
        store = GmailTokenStore.from_token_file(token_file)
        assert store.refresh_token == "rt"
        assert store.client_id == "cid"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError):
            GmailTokenStore.from_token_file(tmp_path / "missing.json")

    def test_incomplete_record_raises(self, tmp_path: Path) -> None:
        token_file = tmp_path / "token.json"
        token_file.write_text(json.dumps({"refresh_token": "rt"}))
        with pytest.raises(RuntimeError):
            GmailTokenStore.from_token_file(token_file)

    def test_requires_all_credentials(self) -> None:
        with pytest.raises(RuntimeError):
            GmailTokenStore(
                refresh_token="", client_id="cid", client_secret="sec"
            )
