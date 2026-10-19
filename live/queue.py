"""Which danmaku command runs next.

The reference live room runs one random danmaku every 20 seconds. That cadence is
what makes realtime video generation feel instant: a 1-3 s render delay is hidden
inside the wait, and viewers keep sending commands hoping theirs gets picked.
Gifts jump the line, which is the room's revenue lever.
"""
from __future__ import annotations

import random
import time

from .router import Command

POLICIES = ("gift_first_random", "gift_first_fifo", "fifo", "random_recent")


class CommandQueue:
    def __init__(self, policy: str = "gift_first_random", max_age_seconds: int = 180,
                 max_size: int = 200, recent_window: int = 10, rng: random.Random | None = None):
        if policy not in POLICIES:
            raise ValueError(f"unknown policy {policy!r}, choose one of {POLICIES}")
        self.policy = policy
        self.max_age_seconds = max_age_seconds
        self.max_size = max_size
        self.recent_window = recent_window
        self.items: list[Command] = []
        self.rng = rng or random.Random()

    def push(self, command: Command) -> None:
        self.items.append(command)
        if len(self.items) > self.max_size:
            # Drop the oldest gift-free command first; never silently drop a gift.
            for index, item in enumerate(self.items):
                if item.gift <= 0 and item.status in ("ready", "preparing"):
                    item.status = "expired"
                    item.note = "队列已满"
                    del self.items[index]
                    break

    def expire(self, now: float | None = None) -> list[Command]:
        now = time.time() if now is None else now
        expired = [c for c in self.items if c.gift <= 0 and c.status in ("ready", "preparing")
                   and now - c.created_at > self.max_age_seconds]
        for command in expired:
            command.status = "expired"
            command.note = f"排队超过 {self.max_age_seconds} 秒，已丢弃"
        self.items = [c for c in self.items if c not in expired]
        return expired

    def waiting(self) -> list[Command]:
        return [c for c in self.items if c.status in ("ready", "preparing")]

    def pick(self, now: float | None = None) -> Command | None:
        self.expire(now)
        ready = [c for c in self.items if c.status == "ready"]
        if not ready:
            return None
        if self.policy.startswith("gift_first"):
            gifted = [c for c in ready if c.gift > 0]
            if gifted:
                chosen = max(gifted, key=lambda c: (c.gift, -c.created_at))
                return self._take(chosen)
        if self.policy in ("gift_first_random", "random_recent"):
            recent = sorted(ready, key=lambda c: c.created_at)[-self.recent_window:]
            return self._take(self.rng.choice(recent))
        return self._take(min(ready, key=lambda c: c.created_at))

    def _take(self, command: Command) -> Command:
        self.items.remove(command)
        return command
