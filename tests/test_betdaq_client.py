"""Unit tests for BetdaqClient."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from clients.betdaq_client import BetdaqClient
from models.types import Market, Venue


def _settings(dry_run=True, username="", api_key="", password=""):
    s = MagicMock()
    s.dry_run = dry_run
    s.betdaq_username = username
    s.betdaq_password = password
    s.betdaq_api_key = api_key
    s.http_timeout_seconds = 10.0
    s.retry_max_attempts = 1
    s.retry_wait_min_seconds = 0.0
    s.retry_wait_max_seconds = 0.1
    return s


def _client(dry_run=True, username="", api_key=""):
    return BetdaqClient(_settings(dry_run=dry_run, username=username, api_key=api_key))


def test_venue_property():
    c = _client()
    assert c.venue == Venue.BETDAQ


def test_place_order_dry_run_returns_id():
    c = _client(dry_run=True)
    m = Market(Venue.BETDAQ, "mkt1", "Test market",
               {"_runners": [{"id": "r1", "name": "yes"}, {"id": "r2", "name": "no"}]})
    oid = c.place_order(m, "yes", 10.0, 0.55)
    assert oid.startswith("dry-betdaq-")


def test_place_order_dry_run_no_http():
    c = _client(dry_run=True)
    m = Market(Venue.BETDAQ, "mkt1", "Test market",
               {"_runners": [{"id": "r1", "name": "yes"}, {"id": "r2", "name": "no"}]})
    with patch.object(c._client, "post") as mock_post:
        c.place_order(m, "yes", 10.0, 0.55)
        mock_post.assert_not_called()


def test_get_markets_no_credentials_returns_empty():
    c = _client(username="", api_key="")
    result = c.get_markets()
    assert result == []


def test_best_quotes_no_credentials_returns_none():
    c = _client(username="", api_key="")
    m = Market(Venue.BETDAQ, "mkt1", "Test", {})
    assert c.best_quotes(m) is None


@pytest.mark.parametrize("runners,expected", [
    ([{"name": "yes"}, {"name": "no"}], True),
    ([{"name": "Yes"}, {"name": "No"}], True),
    ([{"name": "yes"}, {"name": "no"}, {"name": "draw"}], False),
    ([{"name": "team_a"}, {"name": "team_b"}], False),
    ([{"name": "yes"}], False),
    ([], False),
])
def test_is_binary(runners, expected):
    assert BetdaqClient._is_binary(runners) == expected


def test_close_does_not_raise():
    c = _client()
    c.close()


def test_context_manager():
    with _client() as c:
        assert c.venue == Venue.BETDAQ
