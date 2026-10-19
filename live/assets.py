"""Reference images for outfit / prop / background changes.

Vidu's prompt_operation needs 1-3 reference images. A local library answers known
requests instantly; anything new is generated once with an image model and cached,
so the second viewer asking for the same thing gets it immediately.
"""
from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

DEFAULT_OUTFITS = ["米白色针织开衫", "藏青色西装外套", "红黑格子衬衫", "明黄色雨衣", "浅蓝色牛仔外套", "白色连衣裙"]
DEFAULT_BACKGROUNDS = ["海边日落", "樱花街道", "赛博朋克城市夜景", "温馨的书房", "雪山木屋", "星空草地"]

PROMPTS = {
    "garment": "单件服饰产品图：{item}。平铺或挂拍，纯白背景，主体居中且完整可见，无人物、无模特、无文字、无水印，写实摄影风格。",
    "hair": "假发产品图：{item}，戴在白色无脸人台头模上，纯白背景，居中，无文字、无水印，写实摄影风格。",
    "object": "单个物品产品图：{item}。纯白背景，主体居中且完整可见，无人物、无手、无文字、无水印，写实摄影风格。",
    "background": "直播间背景图：{item}。竖构图，场景中没有任何人，光线明亮自然，写实摄影风格，无文字、无水印。",
}


@dataclass
class Asset:
    kind: str
    name: str
    file: str
    user_text: str = ""
    aliases: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"kind": self.kind, "name": self.name, "file": self.file, "user_text": self.user_text,
                "aliases": list(self.aliases)}


def _slug(text: str) -> str:
    return re.sub(r"[^\w一-鿿]+", "-", text).strip("-")[:40] or "item"


class AssetLibrary:
    def __init__(self, root: Path):
        self.root = root
        self.manifest = root / "library.json"
        self.items: list[Asset] = []
        if self.manifest.exists():
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
            self.items = [Asset(i["kind"], i["name"], i["file"], i.get("user_text", ""), tuple(i.get("aliases", [])))
                          for i in data.get("items", [])]

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"items": [a.to_json() for a in self.items]}
        self.manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def find(self, kind: str, item: str) -> Asset | None:
        best, best_score = None, 0
        for asset in self.items:
            if asset.kind != kind or not (self.root / asset.file).exists():
                continue
            for name in (asset.name, *asset.aliases):
                if name == item:
                    return asset
                if name in item or item in name:
                    score = min(len(name), len(item))
                    if score > best_score:
                        best, best_score = asset, score
        return best if best_score >= 2 else None

    def random(self, kind: str) -> Asset | None:
        pool = [a for a in self.items if a.kind == kind and (self.root / a.file).exists()]
        return random.choice(pool) if pool else None

    def add(self, asset: Asset) -> Asset:
        self.items = [a for a in self.items if not (a.kind == asset.kind and a.name == asset.name)]
        self.items.append(asset)
        self.save()
        return asset

    def data_uri(self, asset: Asset, max_side: int = 640) -> str:
        """Small copies keep each switch quick for Vidu to apply (big frames stall it for 10 s+)."""
        path = self.root / asset.file
        small = self.root / ".send" / f"{max_side}" / (Path(asset.file).with_suffix(".jpg").as_posix().replace("/", "__"))
        if not small.exists() or small.stat().st_mtime < path.stat().st_mtime:
            small.parent.mkdir(parents=True, exist_ok=True)
            try:
                shrink_to_jpeg(path, small, max_side)
            except Exception:  # noqa: BLE001 - fall back to the original file
                small = path
        mime = "image/png" if small.suffix.lower() == ".png" else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(small.read_bytes()).decode()


def shrink_to_jpeg(src: Path, dst: Path, max_side: int = 1024) -> Path:
    """Keep WebSocket frames small (Vidu caps one message at 16MB). Uses macOS sips when present."""
    if shutil.which("sips"):
        subprocess.run(["sips", "-Z", str(max_side), "-s", "format", "jpeg", "-s", "formatOptions", "85",
                        str(src), "--out", str(dst)], check=True, capture_output=True)
        return dst
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf",
                        f"scale='min({max_side},iw)':-2", "-q:v", "3", str(dst)], check=True)
        return dst
    shutil.copyfile(src, dst.with_suffix(src.suffix))
    return dst.with_suffix(src.suffix)


class GenerationError(RuntimeError):
    pass


class EvolinkGenerator:
    """Image generation through Evolink (GPT Image by default)."""

    api = "https://api.evolink.ai/v1"

    def __init__(self, api_key: str, model: str, resolution: str, quality: str, timeout_seconds: int = 180):
        self.api_key = api_key
        self.model = model
        self.resolution = resolution
        self.quality = quality
        self.timeout_seconds = timeout_seconds

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "[REDACTED]")

    async def generate(self, session: aiohttp.ClientSession, prompt: str, size: str, out: Path,
                       image_urls: list[str] | None = None, resolution: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body = {"model": self.model, "prompt": prompt, "size": size, "resolution": resolution or self.resolution,
                "quality": self.quality, "n": 1}
        if image_urls:
            body["image_urls"] = image_urls
        async with session.post(f"{self.api}/images/generations", json=body, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise GenerationError(f"Evolink {resp.status}: {self._redact(text)[:300]}")
            task_id = json.loads(text)["id"]
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            async with session.get(f"{self.api}/tasks/{task_id}", headers=headers) as resp:
                state = json.loads(await resp.text())
            if state.get("status") == "completed":
                url = state["results"][0]
                url = url if isinstance(url, str) else url.get("url")
                ua = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36"}
                async with session.get(url, headers=ua) as img:
                    out.write_bytes(await img.read())
                return {"task": task_id, "usage": state.get("usage")}
            if state.get("status") == "failed":
                raise GenerationError(f"Evolink task failed: {self._redact(json.dumps(state.get('error') or state, ensure_ascii=False))[:300]}")
        raise GenerationError(f"Evolink task {task_id} timed out after {self.timeout_seconds}s")


class AssetResolver:
    """Library first, then generate-and-cache. One in-flight generation per name."""

    def __init__(self, library: AssetLibrary, generator: EvolinkGenerator | None, max_parallel: int = 2):
        self.library = library
        self.generator = generator
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}
        self._sem = asyncio.Semaphore(max_parallel)

    def pick_name(self, kind: str, item: str | None) -> str:
        """A vague request ("换件衣服", "换个背景") picks from a curated pool, never a random hat or prop."""
        if item:
            return item
        pool = list(DEFAULT_BACKGROUNDS if kind == "background" else DEFAULT_OUTFITS)
        if kind == "background":
            pool += [a.name for a in self.library.items if a.kind == "background"]
        return random.choice(sorted(set(pool)))

    async def resolve(self, session: aiohttp.ClientSession, kind: str, name: str, user_text: str) -> Asset:
        found = self.library.find(kind, name)
        if found:
            return found
        if self.generator is None:
            raise GenerationError("素材库里没有，且未开启生图")
        key = (kind, name)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._generate(session, kind, name, user_text))
            self._inflight[key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(key, None))
        return await asyncio.shield(task)

    async def _generate(self, session: aiohttp.ClientSession, kind: str, name: str, user_text: str) -> Asset:
        prompt_kind = "hair" if kind == "garment" and ("头发" in name or "发型" in name or name.endswith("头")) else kind
        size = "9:16" if kind == "background" else "1:1"
        folder = self.library.root / "generated" / kind
        folder.mkdir(parents=True, exist_ok=True)
        raw = folder / f"{_slug(name)}-{int(time.time())}.png"
        async with self._sem:
            await self.generator.generate(session, PROMPTS[prompt_kind].format(item=name), size, raw)
        final = await asyncio.to_thread(shrink_to_jpeg, raw, raw.with_suffix(".jpg"), 768 if kind != "background" else 1024)
        if final != raw:
            raw.unlink(missing_ok=True)
        rel = final.relative_to(self.library.root).as_posix()
        return self.library.add(Asset(kind, name, rel, user_text))
