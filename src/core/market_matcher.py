"""Cross-venue market title matching: fuzzy N×N across any number of exchanges."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from loguru import logger
from rapidfuzz import fuzz

from models.types import Market, Venue

_DEFAULT_OVERRIDES_PATH = Path(__file__).resolve().parents[1] / "config" / "market_matches.json"

_STOPWORDS: frozenset[str] = frozenset(
    {
        "yes", "no", "will", "the", "a", "an", "to", "of", "in", "on", "for",
        "and", "or", "is", "be", "been", "being", "are", "was", "were", "vs",
        "versus", "single", "market", "if", "at", "by", "it", "as", "from",
        "with", "this", "that", "than", "into", "about", "before", "after",
        "does", "do", "did", "has", "have", "had", "not", "us", "u", "s",
    }
)

_YEAR_RE = re.compile(r"\b20\d{2}\b")
_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
# Собственные имена: слова с заглавной буквы длиннее 2 символов (не в начале предложения)
_ENTITY_RE = re.compile(r"(?<!\.\s)(?<![?!\s])\b([A-Z][a-z]{2,})\b")


@dataclass(frozen=True)
class MatchResult:
    """Result of matching one market title against a basket of markets."""

    market: Optional[Market]
    score: float
    method: str
    rule_id: Optional[str] = None

    # Legacy alias kept for backward compat with existing tests
    @property
    def kalshi_market(self) -> Optional[Market]:
        return self.market


class MarketMatcher:
    """
    Match market titles across any pair of exchanges using:
    1. Manual rules from market_matches.json (highest priority)
    2. rapidfuzz token-set ratio on normalised strings
    3. Optional sentence-transformer semantic embeddings (improves recall ~30-50%)

    find_match()            — legacy Polymarket→Kalshi API (tests still pass)
    find_all_matches()      — legacy (Kalshi list, Poly list) → pairs
    find_all_matches_multi()— new N×N across all configured venues

    Match result caching: N×N fuzzy matching (O(N²)) is expensive. Results are
    cached by a fingerprint of all market IDs. If the market list hasn't changed
    (which it won't during the 60s cache TTL), matching returns instantly.
    """

    def __init__(
        self,
        *,
        overrides_path: Optional[Path] = None,
        min_fuzzy_score: float = 65.0,
        semantic_enabled: bool = False,
        max_expiry_diff_days: int = 14,
    ) -> None:
        self._overrides_path = overrides_path or _DEFAULT_OVERRIDES_PATH
        self._min_fuzzy_score = min_fuzzy_score
        self._max_expiry_diff_days = max_expiry_diff_days
        self._rules: list[dict[str, Any]] = []
        self._sem_model: Any = None
        # Match cache — keyed by frozenset of (venue, market_id) across all venues
        self._match_cache: Optional[list[tuple[Market, Market]]] = None
        self._match_cache_fp: Optional[frozenset] = None
        if semantic_enabled:
            self._load_semantic_model()
        self._load_overrides()

    def _load_semantic_model(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            self._sem_model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("Semantic matching enabled (all-MiniLM-L6-v2)")
        except ImportError:
            logger.warning(
                "sentence-transformers not installed; falling back to fuzzy-only. "
                "Run: pip install sentence-transformers"
            )

    def _load_overrides(self) -> None:
        path = self._overrides_path
        if not path.is_file():
            logger.warning("Market overrides file not found at {}; manual rules disabled", path)
            self._rules = []
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to read overrides {}: {}", path, e)
            self._rules = []
            return
        rules = raw.get("rules")
        if not isinstance(rules, list):
            logger.warning("Invalid overrides schema in {}: missing 'rules' list", path)
            self._rules = []
            return
        self._rules = [r for r in rules if isinstance(r, Mapping)]
        logger.info("Loaded {} manual market match rules from {}", len(self._rules), path)

    def reload_overrides(self) -> None:
        self._load_overrides()

    @staticmethod
    def normalize_title(text: str) -> str:
        t = text.casefold().strip()
        t = _NON_WORD_RE.sub(" ", t)
        t = _YEAR_RE.sub(" ", t)
        tokens = [w for w in t.split() if w and w not in _STOPWORDS]
        return " ".join(tokens)

    # ------------------------------------------------------------------ #
    # Internal matching helpers
    # ------------------------------------------------------------------ #

    def _manual_match(
        self, title_a: str, candidates: Sequence[Market]
    ) -> Optional[MatchResult]:
        """Check manual override rules (originally Polymarket→Kalshi, kept generic)."""
        haystack = title_a.casefold()
        by_ticker = {m.market_id: m for m in candidates}
        for rule in self._rules:
            needle = str(rule.get("polymarket_title_contains", "")).casefold().strip()
            ticker = str(rule.get("kalshi_ticker", "")).strip()
            if not needle or not ticker:
                continue
            if needle not in haystack:
                continue
            hit = by_ticker.get(ticker)
            if hit is None:
                continue
            rid = str(rule.get("id")) if rule.get("id") is not None else None
            return MatchResult(market=hit, score=100.0, method="manual_override", rule_id=rid)
        return None

    def _best_fuzzy(self, title_a: str, candidates: Sequence[Market]) -> MatchResult:
        norm_a = self.normalize_title(title_a)
        if not norm_a:
            return MatchResult(market=None, score=0.0, method="none")

        # Pre-compute semantic embeddings in one batch (fast) when model is available
        sem_scores: Optional[list[float]] = None
        if self._sem_model is not None and candidates:
            try:
                import numpy as np
                norms_b = [self.normalize_title(m.title) for m in candidates]
                ea = self._sem_model.encode([norm_a])[0]
                ebs = self._sem_model.encode(norms_b)
                norms_ea = float(np.linalg.norm(ea)) + 1e-9
                norms_ebs = np.linalg.norm(ebs, axis=1) + 1e-9
                cos = np.dot(ebs, ea) / (norms_ebs * norms_ea)
                sem_scores = [float(max(0.0, c)) * 100.0 for c in cos]
            except Exception as exc:
                logger.debug("Semantic scoring failed: {}", exc)
                sem_scores = None

        best_market: Optional[Market] = None
        best_score = -1.0
        method = "fuzzy"
        for i, m in enumerate(candidates):
            norm_b = self.normalize_title(m.title)
            if not norm_b:
                continue
            fuzzy_s = float(fuzz.token_set_ratio(norm_a, norm_b))
            if sem_scores is not None:
                s = 0.5 * fuzzy_s + 0.5 * sem_scores[i]
                method = "semantic+fuzzy"
            else:
                s = fuzzy_s
            if s > best_score:
                best_score = s
                best_market = m

        if best_market is None or best_score < self._min_fuzzy_score:
            return MatchResult(market=None, score=max(0.0, best_score), method="none")

        return MatchResult(market=best_market, score=best_score, method=method)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @staticmethod
    def _keyword_tokens(title: str) -> set[str]:
        """Значимые слова заголовка (без стоп-слов, нижний регистр)."""
        cleaned = _NON_WORD_RE.sub(" ", title.casefold())
        cleaned = _YEAR_RE.sub(" ", cleaned)
        return {w for w in cleaned.split() if w and w not in _STOPWORDS and len(w) > 2}

    @staticmethod
    def _entities(title: str) -> set[str]:
        """Собственные имена из заголовка (с заглавной буквы)."""
        return {m.casefold() for m in _ENTITY_RE.findall(title)}

    def _keyword_overlap_ok(self, title_a: str, title_b: str, min_common: int = 2) -> bool:
        """Требуем минимум min_common общих значимых слов."""
        tokens_a = self._keyword_tokens(title_a)
        tokens_b = self._keyword_tokens(title_b)
        if len(tokens_a) < min_common or len(tokens_b) < min_common:
            return True  # слишком короткие заголовки — не блокируем
        return len(tokens_a & tokens_b) >= min_common

    def _entity_overlap_ok(self, title_a: str, title_b: str) -> bool:
        """Если в обоих заголовках есть собственные имена — хотя бы одно должно совпадать."""
        ents_a = self._entities(title_a)
        ents_b = self._entities(title_b)
        if not ents_a or not ents_b:
            return True  # нет имён — не блокируем
        return bool(ents_a & ents_b)

    def _expiry_compatible(self, market_a: Market, market_b: Market) -> bool:
        """Возвращает False если рынки истекают с разницей > max_expiry_diff_days."""
        if self._max_expiry_diff_days <= 0:
            return True
        exp_a = market_a.expiry()
        exp_b = market_b.expiry()
        if exp_a is None or exp_b is None:
            return True  # нет даты — не блокируем
        diff = abs((exp_a - exp_b).days)
        return diff <= self._max_expiry_diff_days

    def find_match(
        self, title_a: str, candidates: Sequence[Market]
    ) -> MatchResult:
        """
        Find the best match for title_a among candidates.
        Candidates may be from any venue; manual rules are checked first.
        """
        manual = self._manual_match(title_a, candidates)
        if manual is not None:
            return manual
        return self._best_fuzzy(title_a, candidates)

    def find_all_matches(
        self,
        kalshi_markets: Sequence[Market],
        polymarket_markets: Sequence[Market],
    ) -> list[tuple[Market, Market]]:
        """
        Legacy method: pair each Polymarket market with its best Kalshi counterpart.
        Returns (polymarket_market, kalshi_market) tuples.
        """
        kalshi_only = [m for m in kalshi_markets if m.venue == Venue.KALSHI]
        ranked: list[tuple[Market, Market, float]] = []
        for poly in polymarket_markets:
            if poly.venue != Venue.POLYMARKET:
                continue
            res = self.find_match(poly.title, kalshi_only)
            if res.market is None:
                continue
            ranked.append((poly, res.market, res.score))
        ranked.sort(key=lambda x: x[2], reverse=True)
        return [(a, b) for a, b, _ in ranked]

    def find_all_matches_multi(
        self,
        markets_by_venue: dict[Venue, list[Market]],
    ) -> list[tuple[Market, Market]]:
        """
        N×N matching across all configured venues.

        For every ordered pair of venues (A, B) where A.value < B.value,
        pairs each market in A with its best match in B.
        Returns deduplicated (market_a, market_b) pairs sorted by score desc.

        Results are cached by market-ID fingerprint. Since market lists are
        cached for 60s, this method returns instantly on every hot iteration.
        """
        # Fingerprint = frozenset of all (venue, market_id) across all venues.
        # Two different market objects with the same IDs → same titles → same matches.
        fingerprint: frozenset = frozenset(
            (v.value, m.market_id)
            for v, mkts in markets_by_venue.items()
            for m in mkts
        )

        if fingerprint == self._match_cache_fp and self._match_cache is not None:
            logger.debug("MarketMatcher: cache hit ({} pairs, no market changes)", len(self._match_cache))
            return self._match_cache

        # Cache miss — run full N×N matching
        venues = sorted(markets_by_venue.keys(), key=lambda v: v.value)
        ranked: list[tuple[Market, Market, float]] = []
        seen: set[tuple[str, str]] = set()

        for i, venue_a in enumerate(venues):
            markets_a = markets_by_venue[venue_a]
            for venue_b in venues[i + 1:]:
                markets_b = markets_by_venue[venue_b]
                if not markets_a or not markets_b:
                    continue

                # Track which venue-B markets are already claimed to prevent
                # multiple venue-A markets all matching the same venue-B market
                claimed_b: set[str] = set()

                for market_a in markets_a:
                    res = self.find_match(market_a.title, markets_b)
                    if res.market is None:
                        continue

                    # Skip if this B-side market was already matched to a better A-side market
                    if res.market.market_id in claimed_b:
                        logger.debug(
                            "Skipping duplicate B-side match: '{}' already claimed",
                            res.market.title[:45],
                        )
                        continue

                    ta, tb = market_a.title, res.market.title

                    if not self._keyword_overlap_ok(ta, tb):
                        logger.debug("Keyword overlap fail: '{}' vs '{}'", ta[:45], tb[:45])
                        continue
                    if not self._entity_overlap_ok(ta, tb):
                        logger.debug("Entity mismatch: '{}' vs '{}'", ta[:45], tb[:45])
                        continue
                    if not self._expiry_compatible(market_a, res.market):
                        logger.debug("Expiry mismatch: '{}' vs '{}'", ta[:45], tb[:45])
                        continue

                    key = (market_a.market_id, res.market.market_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    claimed_b.add(res.market.market_id)
                    ranked.append((market_a, res.market, res.score))

                logger.debug(
                    "MarketMatcher: {}×{} → {} pairs",
                    venue_a.value, venue_b.value, len(ranked),
                )

        ranked.sort(key=lambda x: x[2], reverse=True)
        result = [(a, b) for a, b, _ in ranked]

        self._match_cache_fp = fingerprint
        self._match_cache = result
        logger.debug("MarketMatcher: cache updated ({} pairs)", len(result))
        return result
