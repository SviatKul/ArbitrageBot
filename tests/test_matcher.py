"""Unit tests for ``MarketMatcher`` (overrides + fuzzy + normalization)."""

from __future__ import annotations

import json
from pathlib import Path

from core.market_matcher import MarketMatcher, MatchResult
from models.types import Market, Venue


def _k(ticker: str, title: str) -> Market:
    return Market(venue=Venue.KALSHI, market_id=ticker, title=title, extra={})


def test_normalize_strips_noise_and_year() -> None:
    raw = "Will YES Donald Trump win the 2024 US Presidential Election?"
    norm = MarketMatcher.normalize_title(raw)
    assert "2024" not in norm
    assert "yes" not in norm.split()
    assert "will" not in norm.split()
    assert "donald" in norm
    assert "trump" in norm


def test_manual_override_wins_over_fuzzy(tmp_path: Path) -> None:
    cfg = tmp_path / "m.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "id": "test-rule",
                        "polymarket_title_contains": "Donald Trump win the 2024",
                        "kalshi_ticker": "OVERRIDE_TICK",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    kalshi = [
        _k("OVERRIDE_TICK", "Unrelated short title"),
        _k("OTHER", "Donald Trump wins 2024 US Presidential Election"),
    ]
    m = MarketMatcher(overrides_path=cfg, min_fuzzy_score=50.0)
    r = m.find_match("Will Donald Trump win the 2024 US Presidential Election?", kalshi)
    assert isinstance(r, MatchResult)
    assert r.method == "manual_override"
    assert r.kalshi_market is not None
    assert r.kalshi_market.market_id == "OVERRIDE_TICK"
    assert r.score == 100.0
    assert r.rule_id == "test-rule"


def test_fuzzy_picks_closest_when_no_override(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"version": 1, "rules": []}), encoding="utf-8")
    kalshi = [
        _k("A", "Fed cuts rates before June 2025"),
        _k("B", "Donald Trump wins 2024 US Presidential Election"),
        _k("C", "Bitcoin above 100k by end of 2025"),
    ]
    m = MarketMatcher(overrides_path=empty, min_fuzzy_score=60.0)
    r = m.find_match("Will Donald Trump win the 2024 US Presidential Election?", kalshi)
    assert r.method == "fuzzy"
    assert r.kalshi_market is not None
    assert r.kalshi_market.market_id == "B"
    assert r.score >= 60.0


def test_empty_kalshi_returns_none(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"version": 1, "rules": []}), encoding="utf-8")
    m = MarketMatcher(overrides_path=empty)
    r = m.find_match("Any Polymarket title", [])
    assert r.kalshi_market is None
    assert r.method == "none"


def test_low_fuzzy_score_returns_none(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"version": 1, "rules": []}), encoding="utf-8")
    kalshi = [_k("X", "Totally unrelated market about corn futures Iowa")]
    m = MarketMatcher(overrides_path=empty, min_fuzzy_score=90.0)
    r = m.find_match("Will Donald Trump win the 2024 US Presidential Election?", kalshi)
    assert r.kalshi_market is None
    assert r.method == "none"


def test_example_pres_2024_mapping_from_repo_json() -> None:
    """Uses bundled ``src/config/market_matches.json`` illustrative Trump row."""
    repo_rules = Path(__file__).resolve().parents[1] / "src" / "config" / "market_matches.json"
    kalshi = [
        _k("KX_PRES_2024_TRUMP_EXAMPLE", "Donald Trump wins 2024 US Presidential Election"),
        _k("OTHER", "Corn yield Iowa 2026"),
    ]
    m = MarketMatcher(overrides_path=repo_rules, min_fuzzy_score=80.0)
    title = "Will Donald Trump win the 2024 US Presidential Election?"
    r = m.find_match(title, kalshi)
    assert r.method == "manual_override"
    assert r.kalshi_market and r.kalshi_market.market_id == "KX_PRES_2024_TRUMP_EXAMPLE"


def test_reload_overrides(tmp_path: Path) -> None:
    cfg = tmp_path / "dyn.json"
    cfg.write_text(json.dumps({"version": 1, "rules": []}), encoding="utf-8")
    kalshi = [_k("T1", "Alpha market"), _k("T2", "Beta market similar alpha")]
    m = MarketMatcher(overrides_path=cfg, min_fuzzy_score=50.0)
    r0 = m.find_match("Alpha market futures", kalshi)
    assert r0.kalshi_market is not None

    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "id": "dyn",
                        "polymarket_title_contains": "alpha market futures",
                        "kalshi_ticker": "T2",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    m.reload_overrides()
    r1 = m.find_match("Alpha market futures", kalshi)
    assert r1.method == "manual_override"
    assert r1.kalshi_market and r1.kalshi_market.market_id == "T2"
