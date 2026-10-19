"""Danmaku filtering before anything reaches the avatar.

Douyin punishes vulgar/suggestive live rooms hard, and Vidu itself force-hangs a
session on audit violations, so filtering is strict by default. Rules live in
moderation.json so operators can tune them without touching code.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

_PUNCT = re.compile(r"[\s　,，.。!！?？~～、;；:：'\"“”‘’()（）\[\]【】<>《》·…—\-_=+*/\\|@#$%^&]+")
_EMOJI_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    category: str = ""


class Moderator:
    def __init__(self, rules: dict):
        self.blocked: dict[str, list[str]] = {
            category: [word.lower() for word in words]
            for category, words in rules.get("blocked", {}).items()
        }
        self.allow_phrases = [p.lower() for p in rules.get("allow_phrases", [])]
        self.max_length = int(rules.get("max_length", 40))
        self.dedupe_seconds = int(rules.get("dedupe_seconds", 60))
        self._recent: dict[tuple[str, str], float] = {}

    @classmethod
    def from_file(cls, path: Path) -> "Moderator":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def normalize(text: str) -> str:
        return _PUNCT.sub("", text).lower()

    def check(self, user: str, text: str, now: float | None = None) -> Verdict:
        now = time.time() if now is None else now
        stripped = text.strip()
        if not stripped or _EMOJI_ONLY.match(stripped):
            return Verdict(False, "空内容或只有表情", "噪声")
        if len(stripped) > self.max_length:
            return Verdict(False, f"超过 {self.max_length} 字", "过长")
        compact = self.normalize(stripped)
        scan = compact
        for phrase in self.allow_phrases:
            scan = scan.replace(self.normalize(phrase), "")
        for category, words in self.blocked.items():
            for word in words:
                if word and word in scan:
                    return Verdict(False, f"命中「{word}」", category)
        key = (user, compact)
        last = self._recent.get(key)
        if last is not None and now - last < self.dedupe_seconds:
            return Verdict(False, "重复刷屏", "重复")
        self._recent[key] = now
        if len(self._recent) > 5000:
            cutoff = now - self.dedupe_seconds
            self._recent = {k: t for k, t in self._recent.items() if t >= cutoff}
        return Verdict(True)
