"""
Turning token counts into money, and holding a monthly ceiling.

Rates are per million tokens and come from .env, because prices change and a
number baked into code goes stale silently. The defaults are the published
Anthropic rates for claude-opus-5 at the time of writing:

    input        $5.00 / 1M
    output      $25.00 / 1M
    cache write  $6.25 / 1M   (1.25x input — what it costs to put the prompt in)
    cache read   $0.50 / 1M   (0.10x input — what a repeat turn pays instead)

If the provider or model changes, change these four numbers in .env. Nothing
else in the service needs to know.

The month's spend is kept in a small JSON file rather than in memory. A ceiling
that forgets what has been spent every time the service restarts is not a
ceiling.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MILLION = 1_000_000


@dataclass(frozen=True)
class Rates:
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float
    cache_read_per_mtok: float

    @classmethod
    def from_env(cls, env: dict) -> "Rates":
        def num(name: str, default: float) -> float:
            try:
                return float(str(env.get(name, default)).strip())
            except ValueError:
                raise SystemExit(f"{name} must be a number, got {env.get(name)!r}")
        return cls(
            input_per_mtok=num("PRICE_INPUT_PER_MTOK", 5.00),
            output_per_mtok=num("PRICE_OUTPUT_PER_MTOK", 25.00),
            # A 1-hour cache costs 2x the input rate to write; the 5-minute
            # default costs 1.25x. Reads are 0.1x either way.
            cache_write_per_mtok=num(
                "PRICE_CACHE_WRITE_PER_MTOK",
                10.00 if str(env.get("CACHE_TTL", "5m")).strip() == "1h" else 6.25),
            cache_read_per_mtok=num("PRICE_CACHE_READ_PER_MTOK", 0.50),
        )

    def usd(self, fresh_input: int, cache_write: int, cache_read: int,
            output: int) -> float:
        return (
            fresh_input * self.input_per_mtok
            + cache_write * self.cache_write_per_mtok
            + cache_read * self.cache_read_per_mtok
            + output * self.output_per_mtok
        ) / MILLION

    def usd_for(self, reply) -> float:
        """Cost of one Reply, from what the provider actually reported."""
        return self.usd(reply.fresh_input_tokens, reply.cache_write_tokens,
                        reply.cached_input_tokens, reply.output_tokens)


class MonthlyLedger:
    """
    Spend so far this calendar month (UTC), persisted to one small JSON file.

    Single process only, like the rest of the service. Two workers would each
    keep their own file handle and the ceiling would be as many times too high
    as there are workers.
    """

    def __init__(self, path: Path, ceiling_usd: float):
        self.path = path
        self.ceiling_usd = ceiling_usd
        self._lock = threading.Lock()
        self._month, self._usd = self._read()

    @staticmethod
    def _now_month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _read(self) -> tuple[str, float]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return str(data["month"]), float(data["usd"])
        except (OSError, ValueError, KeyError, TypeError):
            return self._now_month(), 0.0

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename, so a crash mid-write cannot leave
        # a truncated ledger that reads back as zero spent.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"month": self._month, "usd": round(self._usd, 6)}, fh)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _roll(self) -> None:
        month = self._now_month()
        if month != self._month:
            self._month, self._usd = month, 0.0

    def spent(self) -> float:
        with self._lock:
            self._roll()
            return self._usd

    def remaining(self) -> float:
        return max(0.0, self.ceiling_usd - self.spent())

    def would_exceed(self) -> bool:
        """True when the month is already at or over the ceiling."""
        return self.ceiling_usd > 0 and self.spent() >= self.ceiling_usd

    def add(self, usd: float) -> float:
        with self._lock:
            self._roll()
            self._usd += max(0.0, usd)
            self._write()
            return self._usd
