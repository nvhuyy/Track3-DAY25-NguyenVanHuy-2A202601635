from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache with semantic similarity and guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity."""

        # 1. Privacy guardrail
        if _is_uncacheable(query):
            return None, 0.0

        # 2. Evict expired entries
        now = time.time()

        self._entries = [
            entry
            for entry in self._entries
            if now - entry.created_at <= self.ttl_seconds
        ]

        # 3. Find the best matching entry
        best_entry: CacheEntry | None = None
        best_score = 0.0

        for entry in self._entries:
            score = self.similarity(query, entry.key)

            if score > best_score:
                best_score = score
                best_entry = entry

        # 4. No match above threshold
        if best_entry is None or best_score < self.similarity_threshold:
            return None, best_score

        # 4a. False-hit guardrail
        if _looks_like_false_hit(query, best_entry.key):
            self.false_hit_log.append(
                {
                    "query": query,
                    "cached_key": best_entry.key,
                    "score": best_score,
                    "reason": "date_or_number_mismatch",
                }
            )
            return None, best_score

        # 4b. Valid cache hit
        return best_entry.value, best_score

    def set(
        self,
        query: str,
        value: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Store a response in cache."""

        # Privacy guardrail
        if _is_uncacheable(query):
            return

        self._entries.append(
            CacheEntry(
                key=query,
                value=value,
                created_at=time.time(),
                metadata=metadata or {},
            )
        )

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute cosine similarity over word tokens and character trigrams."""

        # 1. Exact match
        if a == b:
            return 1.0

        # Tokenizer:
        # - words
        # - every 3-character n-gram inside each word
        def tokenize(text: str) -> list[str]:
            words = re.findall(r"\w+", text.lower())

            tokens: list[str] = []

            for word in words:
                # Add the complete word
                tokens.append(word)

                # Add character trigrams
                for i in range(len(word) - 2):
                    tokens.append(word[i : i + 3])

            return tokens

        tokens_a = tokenize(a)
        tokens_b = tokenize(b)

        # Empty strings / no tokens
        if not tokens_a or not tokens_b:
            return 0.0

        # 3. Build frequency vectors
        counter_a = Counter(tokens_a)
        counter_b = Counter(tokens_b)

        # 4. Dot product
        common_tokens = counter_a.keys() & counter_b.keys()

        dot_product = sum(
            counter_a[token] * counter_b[token]
            for token in common_tokens
        )

        # Vector magnitudes
        magnitude_a = math.sqrt(
            sum(count * count for count in counter_a.values())
        )

        magnitude_b = math.sqrt(
            sum(count * count for count in counter_b.values())
        )

        # Avoid division by zero
        if magnitude_a == 0.0 or magnitude_b == 0.0:
            return 0.0

        return dot_product / (magnitude_a * magnitude_b)


# ---------------------------------------------------------------------------
# Redis shared cache
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(
            redis_url,
            decode_responses=True,
        )

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis."""

        # 1. Privacy guardrail
        if _is_uncacheable(query):
            return None, 0.0

        # 2. Try exact-match key first
        key = f"{self.prefix}{self._query_hash(query)}"

        response = self._redis.hget(key, "response")

        # 3. Exact match
        if response is not None:
            return response, 1.0

        # 4. Semantic lookup
        best_key: str | None = None
        best_response: str | None = None
        best_score = 0.0

        for cached_key in self._redis.scan_iter(f"{self.prefix}*"):
            cached_query = self._redis.hget(cached_key, "query")

            if cached_query is None:
                continue

            # 5. Compute similarity
            score = ResponseCache.similarity(query, cached_query)

            # Track the best match
            if score > best_score:
                best_score = score
                best_key = cached_query
                best_response = self._redis.hget(cached_key, "response")

        # 6. No match above threshold
        if (
            best_key is None
            or best_response is None
            or best_score < self.similarity_threshold
        ):
            return None, best_score

        # 7. False-hit guardrail
        if _looks_like_false_hit(query, best_key):
            self.false_hit_log.append(
                {
                    "query": query,
                    "cached_key": best_key,
                    "score": best_score,
                    "reason": "date_or_number_mismatch",
                }
            )
            return None, best_score

        return best_response, best_score

    def set(
        self,
        query: str,
        value: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Store a response in Redis with TTL."""

        # Privacy guardrail
        if _is_uncacheable(query):
            return

        # Build deterministic Redis key
        key = f"{self.prefix}{self._query_hash(query)}"

        # Store query and response
        self._redis.hset(
            key,
            mapping={
                "query": query,
                "response": value,
            },
        )

        # Set TTL
        self._redis.expire(key, self.ttl_seconds)

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(
            query.lower().strip().encode()
        ).hexdigest()[:12]
    