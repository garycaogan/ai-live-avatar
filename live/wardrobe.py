"""Outfits as whole avatar images.

Vidu S2 applies the first reference-image outfit change in a session well, gets weaker with
each further change, cannot reliably undo one, and drifts the face over many edits. So an
outfit change starts a fresh Vidu session from a pre-made base photo of the same person in
that outfit. New outfits are generated once from the base photo and kept here.
"""
from __future__ import annotations

import base64
import json
import random
import re
import time
from pathlib import Path

import aiohttp

from .assets import EvolinkGenerator, GenerationError, shrink_to_jpeg

OUTFIT_PROMPT = (
    "Keep the same woman, the same face, the same hairstyle, the same room and lighting, the same pose and the same "
    "candid smartphone front-camera framing as the reference photo. Change only her clothes: she now wears {item}. "
    "Keep any small necklace she wears. Realistic photograph with slight sensor noise and natural skin texture, NOT CG, "
    "NOT a 3D render, NOT anime. Single person only, vertical portrait, no text, no watermark."
)


class Wardrobe:
    def __init__(self, root: Path, manifest: str):
        self.root = root
        self.manifest = root / manifest
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.items: list[dict] = data["items"]

    def save(self) -> None:
        self.manifest.write_text(json.dumps({"items": self.items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def base(self) -> dict:
        return next((i for i in self.items if i.get("base")), self.items[0])

    def find(self, item: str) -> dict | None:
        best, best_score = None, 0
        for entry in self.items:
            for name in (entry["name"], *entry.get("aliases", [])):
                if name == item:
                    return entry
                if name in item or item in name:
                    score = min(len(name), len(item))
                    if score > best_score:
                        best, best_score = entry, score
        return best if best_score >= 2 else None

    def random_other(self, current: str | None) -> dict:
        pool = [i for i in self.items if i["name"] != current and not i.get("base")] or self.items
        return random.choice(pool)

    async def make(self, http: aiohttp.ClientSession, generator: EvolinkGenerator, item: str) -> dict:
        """Generate a new outfit photo from the base photo (about 20 s) and keep it."""
        base_file = self.root / self.base()["file"]
        ref = self.root / ".send" / "wardrobe-ref.jpg"
        ref.parent.mkdir(parents=True, exist_ok=True)
        shrink_to_jpeg(base_file, ref, 1024)
        data_uri = "data:image/jpeg;base64," + base64.b64encode(ref.read_bytes()).decode()
        slug = re.sub(r"[^\w一-鿿]+", "-", item).strip("-")[:30] or "outfit"
        raw = self.manifest.parent / f"gen-{slug}-{int(time.time())}.png"
        await generator.generate(http, OUTFIT_PROMPT.format(item=item), "9:16", raw, image_urls=[data_uri], resolution="2K")
        final = shrink_to_jpeg(raw, raw.with_suffix(".jpg"), 1280)
        if final != raw:
            raw.unlink(missing_ok=True)
        entry = {"name": item, "aliases": [], "file": final.relative_to(self.root).as_posix(), "generated": True}
        self.items.append(entry)
        self.save()
        return entry


__all__ = ["Wardrobe", "GenerationError"]
