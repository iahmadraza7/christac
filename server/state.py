"""
Server side conversation state. In memory, no database, no user accounts.

A conversation is an opaque id plus its turns, the stage in play, whether a
verdict has landed, and what it has spent. Kajabi controls who reaches the
page; nothing here identifies a person.

Single process only. Two workers would each keep their own copy, and a
conversation would lose its verdict flag whenever it hit the other one — so
run one worker, or move this to shared storage first.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Conversation:
    id: str
    created: float
    updated: float
    stage: str
    verdict_delivered: bool = False
    verdict_turn: int | None = None
    tokens_used: int = 0
    usd_spent: float = 0.0
    repeats_blocked: int = 0
    messages: list[dict] = field(default_factory=list)

    @property
    def turns(self) -> int:
        return sum(1 for m in self.messages if m["role"] == "user")

    @property
    def her_messages(self) -> list[str]:
        return [m["content"] for m in self.messages if m["role"] == "user"]

    def turns_since_verdict(self) -> int | None:
        """How many of her turns have gone by since the verdict landed."""
        if self.verdict_turn is None:
            return None
        return self.turns - self.verdict_turn


class ConversationStore:
    def __init__(self, ttl_seconds: int, max_conversations: int = 5000):
        self._ttl = ttl_seconds
        self._max = max_conversations
        self._data: dict[str, Conversation] = {}
        self._lock = threading.Lock()

    def _evict(self, now: float) -> None:
        dead = [k for k, c in self._data.items() if now - c.updated > self._ttl]
        for k in dead:
            del self._data[k]
        # Hard ceiling so a flood cannot grow memory without bound.
        if len(self._data) > self._max:
            oldest = sorted(self._data.items(), key=lambda kv: kv[1].updated)
            for k, _ in oldest[: len(self._data) - self._max]:
                del self._data[k]

    def new(self, stage: str) -> Conversation:
        now = time.time()
        with self._lock:
            self._evict(now)
            c = Conversation(id=secrets.token_urlsafe(18), created=now,
                             updated=now, stage=stage)
            self._data[c.id] = c
            return c

    def get(self, cid: str | None) -> Conversation | None:
        if not cid:
            return None
        now = time.time()
        with self._lock:
            c = self._data.get(cid)
            if c is None:
                return None
            if now - c.updated > self._ttl:
                del self._data[cid]
                return None
            return c

    def touch(self, c: Conversation) -> None:
        with self._lock:
            c.updated = time.time()

    def count(self) -> int:
        with self._lock:
            return len(self._data)


class RateLimiter:
    """Fixed window per IP. Small, predictable, no dependencies."""

    def __init__(self, limit: int, window_seconds: int = 60):
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, ip: str) -> tuple[bool, int]:
        """(allowed, seconds until a slot frees up)."""
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            if len(self._hits) > 10000:
                self._hits = {k: v for k, v in self._hits.items()
                              if v and v[-1] > cutoff}
            seen = [t for t in self._hits.get(ip, []) if t > cutoff]
            if len(seen) >= self._limit:
                self._hits[ip] = seen
                return False, max(1, int(seen[0] + self._window - now) + 1)
            seen.append(now)
            self._hits[ip] = seen
            return True, 0
