"""Workbench backend: edit avatar profiles, wardrobe, action presets and voices, and generate
images and videos into a local gallery.

Everything stays on this machine. Provider keys never leave the server; the page only sees
gallery files under /files/. Generation runs as background jobs so the page can poll them, and
jobs that already reached Evolink resume polling after a server restart.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web

from . import router
from .assets import shrink_to_jpeg
from .session import IDLE_MOTION, IDLE_TOPICS
from .wardrobe import OUTFIT_PROMPT, Wardrobe

EVOLINK = "https://api.evolink.ai/v1"
FILES_API = "https://files-api.evolink.ai/api/v1/files/upload/stream"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36"
IMAGE_MODELS = {"gpt-image-2.5": ("gpt-image-2.5-sunburst", {"quality": "high"}),
                "nano-banana-2.1": ("gemini-nano-banana-2.1", {})}
VIDEO_MODELS = ("seedance-2.0-fast", "seedance-2.0", "seedance-2.5")
VIDEO_MODES = ("text", "image", "reference")
SIZES = ("9:16", "1:1", "16:9")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
VOICE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,31}$")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}

# Realtime voices Vidu offers for provider qwen_omni (from the Vidu voice catalogue, 2026-10).
SYSTEM_VOICES = [
    {"voice": "Sunnybobi", "name": "知芝", "desc": "大大咧咧的社恐邻家姑娘"},
    {"voice": "Tina", "name": "甜甜", "desc": "奶茶小姐姐，甜甜的、暖暖的"},
    {"voice": "Cindy", "name": "林欣宜", "desc": "台湾口音，说话嗲嗲的小姐姐"},
    {"voice": "Liora Mira", "name": "清欢", "desc": "温柔，有烟火气"},
    {"voice": "Serena", "name": "苏瑶", "desc": "温柔小姐姐"},
    {"voice": "Maia", "name": "四月", "desc": "知性又温柔"},
    {"voice": "Qiao", "name": "小乔妹", "desc": "活泼的妹妹音"},
    {"voice": "Ethan", "name": "晨煦", "desc": "阳光男声，带一点北方口音"},
    {"voice": "Raymond", "name": "林川野", "desc": "声音清亮的宅男"},
    {"voice": "Evan", "name": "江晨", "desc": "男大学生，年下奶狗"},
    {"voice": "Theo Calm", "name": "予安", "desc": "沉静、疗愈的男声"},
    {"voice": "Harvey", "name": "厚", "desc": "低沉温和的成熟男声"},
]

PHOTO_REAL = (" Phone camera image quality: mild sensor noise, slight softness, natural skin texture with visible pores and "
              "small imperfections, no beauty filter, no studio lighting. NOT CG, NOT a 3D render, NOT anime, NOT a magazine "
              "photo. No text, no watermark.")
SCENE_PROMPT = (
    "Use the reference photo for the person: keep exactly the same face, the same hairstyle and hair colour, the same "
    "clothes and accessories. Re-shoot her in a new setting as a candid frame from a casual phone livestream, the phone "
    "framing her from head to chest, looking at the screen with a natural expression, not posing. New setting and "
    "lighting: {scene}." + PHOTO_REAL + " Single person only, vertical 9:16 portrait."
)


def _now() -> float:
    return round(time.time(), 3)


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


def _kind_for(name: str, content_type: str = "") -> str | None:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT or content_type.startswith("image/"):
        return "image"
    if ext in VIDEO_EXT or content_type.startswith("video/"):
        return "video"
    if ext in AUDIO_EXT or content_type.startswith("audio/"):
        return "audio"
    return None


def _tmp(suffix: str, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=suffix, dir=folder)
    os.close(fd)
    return Path(name)


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class WorkbenchError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Gallery:
    """Every generated or uploaded image, video and audio clip. Files live in assets/gallery/files."""

    def __init__(self, assets: Path):
        self.assets = assets
        self.dir = assets / "gallery"
        self.files = self.dir / "files"
        self.manifest = self.dir / "gallery.json"
        if self.manifest.exists():
            self.items: list[dict] = json.loads(self.manifest.read_text(encoding="utf-8"))["items"]
        else:
            # First run: show the base-photo candidates made earlier, without copying them.
            self.items = []
            for path in sorted((assets / "avatars" / "candidates").glob("*.jpg")):
                self.items.append({"id": _short_id(), "kind": "image", "file": path.relative_to(assets).as_posix(),
                                   "prompt": "", "model": "gpt-image-2.5" if path.name.startswith("gpt") else "nano-banana-2.1",
                                   "created": path.stat().st_mtime, "source": "候选底图", "cost_cny": None})
            if self.items:
                self.save()  # ids must survive restarts

    def save(self) -> None:
        _write_json(self.manifest, {"items": self.items})

    def url(self, item: dict) -> str:
        return "/files/" + quote(item["file"])

    def public(self, item: dict) -> dict:
        out = {**item, "url": self.url(item)}
        if item["kind"] == "image":
            out["thumb_url"] = f"/api/media/thumb/{item['id']}"  # the wall loads 640 px copies, the lightbox the original
        return out

    def thumb(self, item: dict) -> Path:
        src = self.assets / item["file"]
        dst = self.dir / "thumbs" / f"{item['id']}.jpg"
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shrink_to_jpeg(src, dst, 640)
        return dst

    def get(self, item_id: str, kind: str | None = None) -> dict:
        item = next((i for i in self.items if i["id"] == item_id), None)
        if item is None or not (self.assets / item["file"]).exists():
            raise WorkbenchError(f"素材库里找不到 {item_id}", 404)
        if kind and item["kind"] != kind:
            raise WorkbenchError(f"素材 {item_id} 不是{ {'image': '图片', 'video': '视频', 'audio': '音频'}[kind] }")
        return item

    def path(self, item: dict) -> Path:
        return self.assets / item["file"]

    def add_file(self, src: Path, kind: str, *, prompt: str = "", model: str = "", source: str = "",
                 cost_cny: float | None = None, move: bool = True, extra: dict | None = None) -> dict:
        self.files.mkdir(parents=True, exist_ok=True)
        item_id = _short_id()
        ext = src.suffix.lower() or {"image": ".jpg", "video": ".mp4", "audio": ".wav"}[kind]
        dst = self.files / f"{item_id}{ext}"
        if kind == "image" and ext in (".png", ".webp", ".heic") or kind == "image" and src.stat().st_size > 3_000_000:
            dst = shrink_to_jpeg(src, dst.with_suffix(".jpg"), 2048)  # big PNGs from the image models, phone HEIC/WebP
            if move:
                src.unlink(missing_ok=True)
        elif move:
            shutil.move(src, dst)
        else:
            shutil.copyfile(src, dst)
        item = {"id": item_id, "kind": kind, "file": dst.relative_to(self.assets).as_posix(), "prompt": prompt,
                "model": model, "created": _now(), "source": source, "cost_cny": cost_cny, **(extra or {})}
        self.items.insert(0, item)
        self.save()
        return item

    def delete(self, item_id: str) -> None:
        item = next((i for i in self.items if i["id"] == item_id), None)
        if item is None:
            raise WorkbenchError(f"素材库里找不到 {item_id}", 404)
        path = self.assets / item["file"]
        if path.resolve().is_relative_to(self.files.resolve()):
            path.unlink(missing_ok=True)  # seeded entries point at files used elsewhere; keep those
        self.items.remove(item)
        self.save()


class Workbench:
    def __init__(self, root: Path, assets: Path, room, http: aiohttp.ClientSession, evolink_key: str | None):
        self.root = root
        self.assets = assets
        self.room = room
        self.http = http
        self.key = evolink_key
        self.api, self.files_api = EVOLINK, FILES_API  # tests point these at a local mock
        self.poll_seconds = {"video": 4.0, "image": 3.0}
        self.profiles_file = root / "assets" / "avatars" / "profiles.json"
        self.actions_file = root / "actions.json"
        self.gallery = Gallery(assets)
        self.jobs_file = self.gallery.dir / "jobs.json"
        self.jobs: list[dict] = json.loads(self.jobs_file.read_text(encoding="utf-8"))["jobs"] if self.jobs_file.exists() else []
        self._sem = asyncio.Semaphore(3)
        self._tasks: set[asyncio.Task] = set()
        if self.actions_file.exists():
            self._apply_actions(json.loads(self.actions_file.read_text(encoding="utf-8"))["actions"])

    # ---------- jobs ----------
    def resume_jobs(self) -> None:
        for job in self.jobs:
            if job["status"] in ("queued", "running"):
                if job.get("task_id"):
                    self._spawn(self._run_evolink(job, None))
                else:
                    job.update(status="failed", error="服务重启，任务还没提交就中断了")
        self._save_jobs()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _save_jobs(self) -> None:
        self.jobs = self.jobs[:200]
        _write_json(self.jobs_file, {"jobs": self.jobs})

    def _new_job(self, kind: str, prompt: str, model: str, **meta) -> dict:
        job = {"id": _short_id(), "kind": kind, "status": "queued", "progress": 0, "prompt": prompt, "model": model,
               "created": _now(), "result_id": None, "error": "", "cost_cny": None, "task_id": None, "meta": meta}
        self.jobs.insert(0, job)
        self._save_jobs()
        return job

    def public_job(self, job: dict) -> dict:
        out = {k: v for k, v in job.items() if k not in ("task_id", "body")}
        if job.get("result_id"):
            item = next((i for i in self.gallery.items if i["id"] == job["result_id"]), None)
            out["result_url"] = self.gallery.url(item) if item else None
        return out

    def _evolink_headers(self) -> dict:
        if not self.key:
            raise WorkbenchError("没有 Evolink Key，生成功能不可用（~/.deepsea/evolink/api.key）")
        return {"Authorization": f"Bearer {self.key}"}

    def _redact(self, text: str) -> str:
        return text.replace(self.key, "[REDACTED]") if self.key else text

    async def _evolink(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
        async with self.http.request(method, self.api + path, json=body, headers=self._evolink_headers(),
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise WorkbenchError(f"Evolink {resp.status}: {self._redact(text)[:300]}", 502)
            return json.loads(text)

    async def upload_public(self, path: Path, mime: str) -> str:
        """Evolink's file host gives a public URL for 72 hours (video references and voice cloning need URLs)."""
        form = aiohttp.FormData()
        form.add_field("file", path.read_bytes(), filename=path.name, content_type=mime)
        headers = {**self._evolink_headers(), "User-Agent": BROWSER_UA}
        async with self.http.post(self.files_api, data=form, headers=headers, timeout=aiohttp.ClientTimeout(total=180)) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise WorkbenchError(f"上传文件失败 {resp.status}: {self._redact(text)[:200]}", 502)
            data = json.loads(text)
        data = data.get("data") or data
        url = data.get("file_url") or data.get("url")
        if not url:
            raise WorkbenchError("上传文件没有返回地址", 502)
        return url

    def _image_uri(self, path: Path, max_side: int = 1024) -> str:
        small = self.assets / ".send" / "wb" / f"{max_side}-{path.stem}-{int(path.stat().st_mtime)}.jpg"
        if not small.exists():
            small.parent.mkdir(parents=True, exist_ok=True)
            shrink_to_jpeg(path, small, max_side)
        return "data:image/jpeg;base64," + base64.b64encode(small.read_bytes()).decode()

    def submit(self, kind: str, prompt: str, model: str, body: dict, **meta) -> dict:
        self._evolink_headers()
        job = self._new_job(kind, prompt, model, **meta)
        self._spawn(self._run_evolink(job, body))
        return job

    async def _run_evolink(self, job: dict, body: dict | None) -> None:
        """body=None resumes a job whose Evolink task already exists."""
        try:
            if body is not None:
                async with self._sem:
                    job["status"] = "running"
                    body = await self._prepare(job, body)
                    created = await self._evolink("POST", "/videos/generations" if job["kind"] == "video" else "/images/generations", body)
                job["task_id"] = created["id"]
                self._save_jobs()
            job["status"] = "running"
            limit = 1200 if job["kind"] == "video" else 300
            deadline = time.monotonic() + limit
            while True:
                await asyncio.sleep(self.poll_seconds["video" if job["kind"] == "video" else "image"])
                state = await self._evolink("GET", f"/tasks/{job['task_id']}")
                job["progress"] = int(state.get("progress") or job["progress"])
                if state.get("status") == "completed":
                    break
                if state.get("status") == "failed":
                    raise WorkbenchError(f"生成失败：{self._redact(json.dumps(state.get('error') or state, ensure_ascii=False))[:300]}", 502)
                if time.monotonic() > deadline:
                    raise WorkbenchError(f"等了 {limit // 60} 分钟还没生成完", 504)
            results = state.get("results") or [r.get("url") for r in state.get("result_data") or []]
            url = results[0] if isinstance(results[0], str) else results[0].get("url")
            cost = ((state.get("usage") or {}).get("cost") or {}).get("cny")
            job["cost_cny"] = round(float(cost), 3) if cost is not None else None
            ext = ".mp4" if job["kind"] == "video" else Path(url.split("?")[0]).suffix.lower() or ".png"
            tmp = _tmp(ext, self.assets)
            async with self.http.get(url, headers={"User-Agent": BROWSER_UA}, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status >= 300:
                    raise WorkbenchError(f"下载结果失败 {resp.status}", 502)
                tmp.write_bytes(await resp.read())
            item = self.gallery.add_file(tmp, "video" if job["kind"] == "video" else "image", prompt=job["prompt"],
                                         model=job["model"], source=job["meta"].get("source", ""), cost_cny=job["cost_cny"])
            job["result_id"] = item["id"]
            await self._finish(job, item)
            job.update(status="done", progress=100)
        except Exception as error:  # noqa: BLE001
            job.update(status="failed", error=str(error)[:300])
            self.room.log("warn", f"工作台任务失败：{job['kind']}", error=str(error)[:300])
        finally:
            self._save_jobs()

    async def _prepare(self, job: dict, body: dict) -> dict:
        """Turn gallery ids into what the provider needs right before submitting (uploads can be slow)."""
        body = dict(body)
        refs = body.pop("_ref_files", None)
        if refs:
            body["image_urls"] = [self._image_uri(Path(p)) for p in refs]
        upload_image = body.pop("_upload_image", None)
        if upload_image:
            body["image_urls"] = [await self.upload_public(Path(upload_image), "image/jpeg")]
        upload_audio = body.pop("_upload_audio", None)
        if upload_audio:
            body["audio_urls"] = [await self.upload_public(Path(upload_audio), "audio/wav")]
        return body

    async def _finish(self, job: dict, item: dict) -> None:
        meta = job["meta"]
        if job["kind"] == "outfit":
            key = meta["profile"]
            wardrobe = self.wardrobe_for(key)
            file = self._copy_into_profile(key, self.gallery.path(item), "outfit")
            existing = next((i for i in wardrobe.items if i["name"] == meta["name"]), None)
            if existing:  # regenerate: same outfit, new photo that matches the current base
                existing["file"] = file
                existing["generated"] = True
            else:
                wardrobe.items.append({"name": meta["name"], "aliases": meta.get("aliases") or [], "file": file,
                                       "description": meta.get("description", ""), "generated": True})
            wardrobe.save()
            self.room.log("info", f"工作台：衣柜里的{meta['name']}已更新", profile=key)

    # ---------- profiles ----------
    def _file_url(self, rel: str) -> str:
        path = self.root / rel
        version = int(path.stat().st_mtime) if path.exists() else 0
        if rel.startswith("assets/"):
            return f"/files/{quote(rel[len('assets/'):])}?v={version}"
        return f"/avatar.jpg?v={version}"

    def profile(self, key: str) -> dict:
        profile = self.room.cfg["avatars"].get(key)
        if profile is None:
            raise WorkbenchError(f"没有角色 {key}", 404)
        return profile

    def wardrobe_for(self, key: str) -> Wardrobe | None:
        profile = self.profile(key)
        if not profile.get("outfits"):
            return None
        cached = self.room._wardrobes.get(key)
        if cached is None or cached.manifest != self.root / profile["outfits"]:
            cached = self.room._wardrobes[key] = Wardrobe(self.root, profile["outfits"])
        return cached

    def profile_dir(self, key: str) -> Path:
        profile = self.profile(key)
        if profile.get("outfits"):
            return (self.root / profile["outfits"]).parent
        return self.root / "assets" / "avatars" / key

    def _copy_into_profile(self, key: str, src: Path, prefix: str) -> str:
        folder = self.profile_dir(key)
        folder.mkdir(parents=True, exist_ok=True)
        dst = shrink_to_jpeg(src, folder / f"{prefix}-{_short_id()}.jpg", 1280)
        return dst.relative_to(self.root).as_posix()

    def profile_summary(self, key: str) -> dict:
        p = self.profile(key)
        wardrobe = self.wardrobe_for(key)
        return {"key": key, "name": p["name"], "note": p.get("note", ""), "image_url": self._file_url(p["image"]),
                "voice": p.get("voice", ""), "greeting_instruction": p.get("greeting_instruction", ""),
                "outfits_count": len(wardrobe.items) if wardrobe else 0}

    def profile_detail(self, key: str) -> dict:
        p = self.profile(key)
        wardrobe = self.wardrobe_for(key)
        persona_path = self.root / p.get("persona_file", "persona.md")
        outfits = []
        for item in (wardrobe.items if wardrobe else []):
            outfits.append({"name": item["name"], "aliases": item.get("aliases", []), "image_url": self._file_url(item["file"]),
                            "base": bool(item.get("base")), "description": item.get("description", ""),
                            "generated": bool(item.get("generated"))})
        return {**self.profile_summary(key),
                "persona_text": persona_path.read_text(encoding="utf-8") if persona_path.exists() else "",
                "idle_motions": p.get("idle_motions") or IDLE_MOTION["motions"],
                "custom_idle_motions": bool(p.get("idle_motions")),
                "idle_topics": p.get("idle_topics") or IDLE_TOPICS,
                "custom_idle_topics": bool(p.get("idle_topics")),
                "desk_items": p.get("desk_items") or {},
                "outfits": outfits, "in_use": self.room.profile == key and self.room.status in ("creating", "connecting", "waiting_stage", "live"),
                "current_outfit": self.room.outfit if self.room.profile == key else None}

    def _save_profile(self, key: str) -> None:
        data = json.loads(self.profiles_file.read_text(encoding="utf-8")) if self.profiles_file.exists() else {}
        data[key] = self.room.cfg["avatars"][key]
        _write_json(self.profiles_file, data)

    def update_profile(self, key: str, body: dict) -> dict:
        avatars = self.room.cfg["avatars"]
        creating = key not in avatars
        if creating:
            if not KEY_RE.match(key):
                raise WorkbenchError("角色标识只能用小写字母、数字和下划线，字母开头，2 到 32 位")
            if not str(body.get("name") or "").strip():
                raise WorkbenchError("新角色要有名字")
            if not body.get("base_gallery_id"):
                raise WorkbenchError("新角色要先从素材库选一张底图")
            source = self.gallery.path(self.gallery.get(body["base_gallery_id"], "image"))
            folder = self.root / "assets" / "avatars" / key
            folder.mkdir(parents=True, exist_ok=True)
            base = shrink_to_jpeg(source, folder / f"base-{_short_id()}.jpg", 1280).relative_to(self.root).as_posix()
            manifest = (folder / "outfits.json").relative_to(self.root).as_posix()
            _write_json(self.root / manifest, {"items": [{"name": str(body.get("base_outfit_name") or "日常").strip()[:12],
                                                           "aliases": [], "file": base, "base": True}]})
            template = (self.root / "persona.md").read_text(encoding="utf-8") if (self.root / "persona.md").exists() else ""
            avatars[key] = {"name": "", "note": "", "image": base, "voice": "", "persona_file": "persona.md",
                            "greeting_instruction": "笑着跟刚进直播间的人打个招呼，随口说一句可以发弹幕让你换衣服、拿东西",
                            "outfits": manifest}
            body.setdefault("persona_text", template)
        p = avatars[key]
        if "name" in body:
            name = str(body["name"]).strip()[:20]
            if not name:
                raise WorkbenchError("名字不能为空")
            p["name"] = name
        for field, limit in (("note", 80), ("greeting_instruction", 200)):
            if field in body:
                p[field] = str(body[field] or "").strip()[:limit]
        if "voice" in body:
            voice = str(body["voice"] or "").strip()
            if voice and not re.match(r"^[A-Za-z][A-Za-z0-9_ ]{1,40}$", voice):
                raise WorkbenchError("音色名不对")
            p["voice"] = voice
        if "persona_text" in body:
            text = str(body["persona_text"] or "").strip()
            if len(text) < 20:
                raise WorkbenchError("人设太短了，至少写几句")
            if len(text) > 6000:
                raise WorkbenchError("人设太长了，控制在 6000 字以内")
            persona_rel = f"personas/{key}.md"  # profiles used to share persona files; editing one must not change the others
            (self.root / "personas").mkdir(exist_ok=True)
            (self.root / persona_rel).write_text(text + "\n", encoding="utf-8")
            p["persona_file"] = persona_rel
        if "idle_motions" in body:
            p["idle_motions"] = self._clean_motions(body["idle_motions"])
        if "idle_topics" in body:
            topics = self._clean_topics(body["idle_topics"])
            if topics:
                p["idle_topics"] = topics
            else:
                p.pop("idle_topics", None)  # back to the built-in set
        if "desk_items" in body:
            desk = {k: str((body["desk_items"] or {}).get(k) or "").strip()[:20] for k in ("drink", "eat")}
            p["desk_items"] = {k: v for k, v in desk.items() if v}
        self._save_profile(key)
        self.room.log("info", f"工作台：{'新建' if creating else '修改'}角色 {p['name']}", profile=key, fields=sorted(body))
        self.room.broadcast("state")
        return self.profile_detail(key)

    @staticmethod
    def _clean_topics(topics) -> list[str]:
        if isinstance(topics, str):
            topics = topics.splitlines()
        if not isinstance(topics, list):
            raise WorkbenchError("冷场话题要一行一个")
        out: list[str] = []
        for t in topics:
            t = str(t or "").strip().rstrip("。")
            if not t or t in out:
                continue
            if len(t) > 60:
                raise WorkbenchError(f"冷场话题太长了：{t[:20]}…（一条 60 字以内）")
            out.append(t)
        if len(out) > 100:
            raise WorkbenchError("冷场话题最多 100 条")
        return out

    @staticmethod
    def _clean_motions(motions) -> list[dict] | None:
        if motions in (None, [], "default"):
            return None  # back to the built-in set
        if not isinstance(motions, list) or len(motions) > 20:
            raise WorkbenchError("待机动作最多 20 个")
        out = []
        for m in motions:
            content = str(m.get("content") or "").strip()
            if not content:
                continue
            if len(content) > 80:
                raise WorkbenchError(f"动作描述太长：{content[:20]}…")
            out.append({"name": str(m.get("name") or content[:6]).strip()[:12], "content": content,
                        "weight": max(0.1, min(5.0, float(m.get("weight") or 1))),
                        "description": str(m.get("description") or "").strip()[:40]})
        if not out:
            raise WorkbenchError("至少要有一个待机动作")
        return out

    def set_base(self, key: str, gallery_id: str, outfit_name: str | None = None) -> dict:
        p = self.profile(key)
        source = self.gallery.path(self.gallery.get(gallery_id, "image"))
        file = self._copy_into_profile(key, source, "base")
        p["image"] = file
        wardrobe = self.wardrobe_for(key)
        if wardrobe is None:
            manifest = (self.profile_dir(key) / "outfits.json").relative_to(self.root).as_posix()
            _write_json(self.root / manifest, {"items": [{"name": (outfit_name or "日常")[:12], "aliases": [], "file": file, "base": True}]})
            p["outfits"] = manifest
        else:
            base = wardrobe.base()
            base["file"] = file
            base["base"] = True
            if outfit_name:
                base["name"] = outfit_name.strip()[:12]
            wardrobe.save()
        self._save_profile(key)
        self.room.log("info", f"工作台：{p['name']} 换了底图", profile=key, gallery_id=gallery_id)
        self.room.broadcast("state")
        return self.profile_detail(key)

    def add_outfit(self, key: str, name: str, aliases: list[str], gallery_id: str) -> dict:
        wardrobe = self._wardrobe_or_fail(key)
        name = self._outfit_name(wardrobe, name)
        file = self._copy_into_profile(key, self.gallery.path(self.gallery.get(gallery_id, "image")), "outfit")
        wardrobe.items.append({"name": name, "aliases": self._clean_aliases(aliases), "file": file})
        wardrobe.save()
        return self.profile_detail(key)

    def generate_outfit(self, key: str, name: str, description: str, model: str, aliases: list[str] | None = None) -> dict:
        wardrobe = self._wardrobe_or_fail(key)
        name = self._outfit_name(wardrobe, name)
        return self._outfit_job(key, wardrobe, name, description or name, model, aliases or [])

    def regenerate_outfits(self, key: str, model: str, names: list[str] | None = None) -> list[dict]:
        wardrobe = self._wardrobe_or_fail(key)
        targets = [i for i in wardrobe.items if not i.get("base") and (not names or i["name"] in names)]
        if not targets:
            raise WorkbenchError("衣柜里除了底图没有别的衣服")
        return [self._outfit_job(key, wardrobe, i["name"], i.get("description") or i["name"], model, i.get("aliases") or [])
                for i in targets]

    def _outfit_job(self, key: str, wardrobe: Wardrobe, name: str, description: str, model: str, aliases: list[str]) -> dict:
        model_id, extra = self._image_model(model)
        base_file = self.root / wardrobe.base()["file"]
        prompt = OUTFIT_PROMPT.format(item=description)
        body = {"model": model_id, "prompt": prompt, "size": "9:16", "resolution": "2K", "n": 1, **extra,
                "_ref_files": [str(base_file)]}
        return self.submit("outfit", f"{name}：{description}", model, body, profile=key, name=name, description=description,
                           aliases=self._clean_aliases(aliases), source=f"衣柜·{self.profile(key)['name']}", size="9:16")

    def patch_outfit(self, key: str, name: str, body: dict) -> dict:
        wardrobe = self._wardrobe_or_fail(key)
        item = next((i for i in wardrobe.items if i["name"] == name), None)
        if item is None:
            raise WorkbenchError(f"衣柜里没有{name}", 404)
        if body.get("new_name") and body["new_name"] != name:
            item["name"] = self._outfit_name(wardrobe, body["new_name"])
            if self.room.profile == key and self.room.outfit == name:
                self.room.outfit = item["name"]
        if "aliases" in body:
            item["aliases"] = self._clean_aliases(body["aliases"])
        if "description" in body:
            item["description"] = str(body["description"] or "").strip()[:120]
        wardrobe.save()
        return self.profile_detail(key)

    def delete_outfit(self, key: str, name: str) -> dict:
        wardrobe = self._wardrobe_or_fail(key)
        item = next((i for i in wardrobe.items if i["name"] == name), None)
        if item is None:
            raise WorkbenchError(f"衣柜里没有{name}", 404)
        if item.get("base"):
            raise WorkbenchError("底图那套不能删，先换底图")
        wardrobe.items.remove(item)
        wardrobe.save()
        return self.profile_detail(key)

    def _wardrobe_or_fail(self, key: str) -> Wardrobe:
        wardrobe = self.wardrobe_for(key)
        if wardrobe is None:
            raise WorkbenchError("这个角色还没有衣柜，先设一张底图")
        return wardrobe

    @staticmethod
    def _outfit_name(wardrobe: Wardrobe, name: str) -> str:
        name = str(name or "").strip()[:12]
        if not name:
            raise WorkbenchError("衣服要有名字")
        if any(i["name"] == name for i in wardrobe.items):
            raise WorkbenchError(f"衣柜里已经有{name}了")
        return name

    @staticmethod
    def _clean_aliases(aliases) -> list[str]:
        if isinstance(aliases, str):
            aliases = re.split(r"[,，、\s]+", aliases)
        return [a.strip()[:12] for a in (aliases or []) if str(a).strip()][:12]

    @staticmethod
    def _image_model(model: str) -> tuple[str, dict]:
        if model not in IMAGE_MODELS:
            raise WorkbenchError(f"生图模型只能是 {'、'.join(IMAGE_MODELS)}")
        return IMAGE_MODELS[model]

    # ---------- actions ----------
    @staticmethod
    def actions_public() -> list[dict]:
        return [{"keywords": list(words), "action": action} for words, action in router.ACTIONS]

    @staticmethod
    def _apply_actions(items: list[dict]) -> None:
        router.ACTIONS[:] = [(tuple(i["keywords"]), i.get("action")) for i in items]

    def update_actions(self, items) -> list[dict]:
        if not isinstance(items, list) or len(items) > 80:
            raise WorkbenchError("动作最多 80 条")
        clean = []
        for i in items:
            words = [w.strip()[:12] for w in (i.get("keywords") or []) if str(w).strip()][:12]
            action = str(i.get("action") or "").strip()[:60] or None
            if not words:
                continue
            clean.append({"keywords": words, "action": action})
        if not clean:
            raise WorkbenchError("至少要有一条动作")
        _write_json(self.actions_file, {"actions": clean})
        self._apply_actions(clean)
        self.room.log("info", f"工作台：动作表已更新，共 {len(clean)} 条")
        return self.actions_public()

    # ---------- voices ----------
    async def voices(self) -> dict:
        cloned, error = [], ""
        if self.room.vidu:
            try:
                cloned = (await self.room.vidu.request("GET", "/live/v1/voices")).get("voices") or []
            except Exception as err:  # noqa: BLE001
                error = str(err)[:200]
        used: dict[str, list[str]] = {}
        for key, p in self.room.cfg["avatars"].items():
            if p.get("voice"):
                used.setdefault(p["voice"], []).append(key)
        samples: dict[str, list[dict]] = {}
        for item in self.gallery.items:  # newest first
            if item["kind"] == "audio" and item.get("voice"):
                samples.setdefault(item["voice"], []).append({"id": item["id"], "url": self.gallery.url(item),
                                                              "label": item.get("label") or item.get("prompt") or "",
                                                              "sample_kind": item.get("sample_kind", "source")})
        # assets/voices.json is the curated library shown first; every other voice counts as old and stays folded
        path = self.assets / "voices.json"
        library = json.loads(path.read_text(encoding="utf-8")).get("library", []) if path.exists() else []
        in_lib = {v["voice"] for v in library}
        old_cloned = [{"voice": v.get("voice")} for v in cloned if v.get("voice") and v.get("voice") not in in_lib]
        old_system = [v for v in SYSTEM_VOICES if v["voice"] not in in_lib]
        return {"library": library, "system": old_system, "cloned": old_cloned, "old_count": len(old_system) + len(old_cloned) if library else 0,
                "used_by": used, "samples": samples, "error": error}

    @staticmethod
    def process_audio(src: Path, dst: Path, speed: float, pitch: float, max_seconds: int = 30) -> float:
        """Resample to 24 kHz mono, then shift pitch by semitones and change speed independently."""
        speed = max(0.8, min(1.4, float(speed)))
        pitch = max(-3.0, min(3.0, float(pitch)))
        mid = dst.with_suffix(".mid.wav")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-t", str(max_seconds), "-ac", "1", "-ar", "24000", str(mid)],
                       check=True, capture_output=True)
        rate = 2 ** (pitch / 12)
        filters = f"asetrate={24000 * rate:.2f},aresample=24000,atempo={speed / rate:.5f}"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mid), "-af", filters, "-ac", "1", "-ar", "24000", str(dst)],
                       check=True, capture_output=True)
        mid.unlink(missing_ok=True)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(dst)],
                             capture_output=True, text=True)
        return float(out.stdout.strip() or 0)

    async def transcribe(self, wav: Path) -> str:
        """Vidu's clone wants the sample's transcript; Gemini writes it when the user leaves it empty."""
        mp4 = wav.with_suffix(".probe.mp4")
        await asyncio.to_thread(subprocess.run, ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-f", "lavfi", "-i",
                                                 "color=c=black:s=64x64:r=5", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(mp4)],
                                check=True, capture_output=True)
        body = {"model": "gemini-3.8-flash", "temperature": 0, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "逐字转写这段音频里的中文语音，加上标点。只输出转写文字，不要任何解释。"},
            {"type": "image_url", "image_url": {"url": "data:video/mp4;base64," + base64.b64encode(mp4.read_bytes()).decode()}}]}]}
        mp4.unlink(missing_ok=True)
        data = await self._evolink("POST", "/chat/completions", body, timeout=120)
        return data["choices"][0]["message"]["content"].strip().strip("\"“”")[:300]

    async def clone_voice(self, src: Path, name: str, speed: float, pitch: float, text: str) -> dict:
        if not self.room.vidu:
            raise WorkbenchError("没有 Vidu Key，不能克隆")
        if not VOICE_RE.match(name or ""):
            raise WorkbenchError("音色名只能用英文字母、数字和下划线，字母开头，3 到 32 位")
        work = Path(tempfile.mkdtemp(prefix="voice-", dir=self.assets))
        try:
            wav = work / f"{name}.wav"
            duration = await asyncio.to_thread(self.process_audio, src, wav, speed, pitch)
            if duration < 5:
                raise WorkbenchError(f"处理后只有 {duration:.1f} 秒，至少要 5 秒，10 到 20 秒最好")
            text = (text or "").strip() or await self.transcribe(wav)
            url = await self.upload_public(wav, "audio/wav")
            result = await self.room.vidu.request("POST", "/live/v1/voices/clone", {
                "audio_url": url, "voice": name, "text": text, "language": "zh", "provider": "qwen_omni"})
            kept = self.gallery.add_file(wav, "audio", prompt=text, model="vidu-clone", source=f"克隆音色·{name}", move=False,
                                         extra={"voice": result.get("voice") or name, "sample_kind": "source", "label": "克隆用的原样本"})
            self.room.log("info", f"工作台：克隆了音色 {name}", seconds=round(duration, 1), speed=speed, pitch=pitch)
            return {"voice": result.get("voice") or name, "text": text, "seconds": round(duration, 1), "sample_id": kept["id"]}
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ---------- media ----------
    def image_job(self, body: dict) -> dict:
        model_id, extra = self._image_model(body.get("model") or "gpt-image-2.5")
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise WorkbenchError("写一下要生成什么")
        size = body.get("size") or "9:16"
        if size not in SIZES:
            raise WorkbenchError(f"比例只能是 {'、'.join(SIZES)}")
        refs = [str(self.gallery.path(self.gallery.get(i, "image"))) for i in (body.get("ref_ids") or [])[:4]]
        full = prompt + (PHOTO_REAL if body.get("photo_real") else "")
        payload = {"model": model_id, "prompt": full, "size": size, "resolution": "2K", "n": 1, **extra}
        if refs:
            payload["_ref_files"] = refs
        return self.submit("image", prompt, body.get("model") or "gpt-image-2.5", payload, source="生图", size=size)

    def scene_job(self, body: dict) -> dict:
        key = body.get("profile") or self.room.profile
        p = self.profile(key)
        scene = str(body.get("scene_prompt") or "").strip()
        if not scene:
            raise WorkbenchError("写一下新场景和灯光")
        model = body.get("model") or "gpt-image-2.5"
        model_id, extra = self._image_model(model)
        payload = {"model": model_id, "prompt": SCENE_PROMPT.format(scene=scene), "size": "9:16", "resolution": "2K", "n": 1,
                   **extra, "_ref_files": [str(self.root / p["image"])]}
        return self.submit("scene", scene, model, payload, source=f"场景·{p['name']}", profile=key, size="9:16")

    def video_job(self, body: dict) -> dict:
        model = body.get("model") or "seedance-2.0-fast"
        mode = body.get("mode") or "image"
        if model not in VIDEO_MODELS:
            raise WorkbenchError(f"视频模型只能是 {'、'.join(VIDEO_MODELS)}")
        if mode not in VIDEO_MODES:
            raise WorkbenchError("模式只能是 text、image、reference")
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise WorkbenchError("写一下视频内容")
        duration = max(4, min(12, int(body.get("duration") or 5)))
        resolution = body.get("resolution") or "480p"
        if resolution not in ("480p", "720p"):
            raise WorkbenchError("清晰度只能是 480p 或 720p")
        payload = {"model": f"{model}-{mode}-to-video", "prompt": prompt, "aspect_ratio": body.get("aspect_ratio") or "9:16",
                   "duration": duration, "quality": resolution, "resolution": resolution, "generate_audio": True}
        if mode in ("image", "reference"):
            if not body.get("image_id"):
                raise WorkbenchError("图生视频和参考生视频要选一张图")
            payload["_upload_image"] = str(self.gallery.path(self.gallery.get(body["image_id"], "image")))
        if body.get("audio_id"):
            if mode != "reference":
                raise WorkbenchError("只有参考生视频能带声音参考")
            payload["_upload_audio"] = str(self.gallery.path(self.gallery.get(body["audio_id"], "audio")))
        return self.submit("video", prompt, f"{model} · {mode}", payload, source="生视频", duration=duration, resolution=resolution,
                           size=payload["aspect_ratio"])

    async def save_upload(self, field) -> dict:
        name = field.filename or "upload"
        kind = _kind_for(name, field.headers.get("Content-Type", ""))
        if kind is None:
            raise WorkbenchError("只支持图片、视频和音频文件")
        ext = Path(name).suffix.lower() or {"image": ".jpg", "video": ".mp4", "audio": ".wav"}[kind]
        tmp = _tmp(ext, self.assets)
        size = 0
        with tmp.open("wb") as out:
            while chunk := await field.read_chunk(1 << 20):
                size += len(chunk)
                if size > 300 * 1024 * 1024:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    raise WorkbenchError("文件超过 300MB")
                out.write(chunk)
        return self.gallery.add_file(tmp, kind, prompt=name, source="上传")


def add_routes(app: web.Application, wb_key: web.AppKey) -> None:
    """Register the workbench API (see WORKBENCH_API.md)."""
    routes = web.RouteTableDef()

    def handle(fn):
        async def wrapped(request):
            try:
                return await fn(request, request.app[wb_key])
            except WorkbenchError as error:
                return web.json_response({"error": str(error)}, status=error.status)
            except (ValueError, KeyError, TypeError) as error:
                return web.json_response({"error": f"参数不对：{error}"}, status=400)
        return wrapped

    async def body_of(request) -> dict:
        return await request.json() if request.can_read_body else {}

    @routes.get("/api/profiles")
    @handle
    async def profiles(request, wb: Workbench):
        return web.json_response({"current": wb.room.profile,
                                  "profiles": [wb.profile_summary(k) for k in wb.room.cfg["avatars"]]})

    @routes.get("/api/profiles/{key}")
    @handle
    async def profile_get(request, wb: Workbench):
        return web.json_response(wb.profile_detail(request.match_info["key"]))

    @routes.put("/api/profiles/{key}")
    @handle
    async def profile_put(request, wb: Workbench):
        return web.json_response(wb.update_profile(request.match_info["key"], await body_of(request)))

    @routes.post("/api/profiles/{key}/base")
    @handle
    async def profile_base(request, wb: Workbench):
        body = await body_of(request)
        return web.json_response(wb.set_base(request.match_info["key"], body["gallery_id"], body.get("outfit_name")))

    @routes.post("/api/profiles/{key}/outfits")
    @handle
    async def outfit_add(request, wb: Workbench):
        body = await body_of(request)
        return web.json_response(wb.add_outfit(request.match_info["key"], body.get("name"), body.get("aliases"), body["gallery_id"]))

    @routes.post("/api/profiles/{key}/outfits/generate")
    @handle
    async def outfit_generate(request, wb: Workbench):
        body = await body_of(request)
        job = wb.generate_outfit(request.match_info["key"], body.get("name"), str(body.get("description") or "").strip(),
                                 body.get("model") or "gpt-image-2.5", body.get("aliases"))
        return web.json_response({"job": wb.public_job(job)})

    @routes.post("/api/profiles/{key}/outfits/regenerate")
    @handle
    async def outfit_regenerate(request, wb: Workbench):
        body = await body_of(request)
        jobs = wb.regenerate_outfits(request.match_info["key"], body.get("model") or "gpt-image-2.5", body.get("names"))
        return web.json_response({"jobs": [wb.public_job(j) for j in jobs]})

    @routes.patch("/api/profiles/{key}/outfits/{name}")
    @handle
    async def outfit_patch(request, wb: Workbench):
        return web.json_response(wb.patch_outfit(request.match_info["key"], request.match_info["name"], await body_of(request)))

    @routes.delete("/api/profiles/{key}/outfits/{name}")
    @handle
    async def outfit_delete(request, wb: Workbench):
        return web.json_response(wb.delete_outfit(request.match_info["key"], request.match_info["name"]))

    @routes.get("/api/actions")
    @handle
    async def actions_get(request, wb: Workbench):
        return web.json_response({"actions": wb.actions_public()})

    @routes.put("/api/actions")
    @handle
    async def actions_put(request, wb: Workbench):
        return web.json_response({"actions": wb.update_actions((await body_of(request)).get("actions"))})

    @routes.get("/api/voices")
    @handle
    async def voices(request, wb: Workbench):
        return web.json_response(await wb.voices())

    async def read_audio_form(request, wb: Workbench) -> tuple[Path, dict]:
        reader = await request.multipart()
        fields, src = {}, None
        while (part := await reader.next()) is not None:
            if part.name == "file":
                ext = Path(part.filename or "a.wav").suffix.lower() or ".wav"
                src = _tmp(ext, wb.assets)
                with src.open("wb") as out:
                    while chunk := await part.read_chunk(1 << 20):
                        out.write(chunk)
            elif part.name == "gallery_id":
                fields["gallery_id"] = (await part.text()).strip()
            else:
                fields[part.name] = (await part.text()).strip()
        if src is None and fields.get("gallery_id"):
            item = wb.gallery.get(fields["gallery_id"], "audio")
            src = _tmp(Path(item["file"]).suffix, wb.assets)
            shutil.copyfile(wb.gallery.path(item), src)
        if src is None:
            raise WorkbenchError("没有收到音频文件")
        return src, fields

    @routes.post("/api/voices/clone")
    @handle
    async def voice_clone(request, wb: Workbench):
        src, f = await read_audio_form(request, wb)
        try:
            return web.json_response(await wb.clone_voice(src, f.get("name", ""), float(f.get("speed") or 1), float(f.get("pitch") or 0),
                                                          f.get("text", "")))
        finally:
            src.unlink(missing_ok=True)

    @routes.post("/api/voices/samples")
    @handle
    async def voice_sample(request, wb: Workbench):
        """Attach an audio clip to a voice so the page can play it (live excerpts, clone sources)."""
        src, f = await read_audio_form(request, wb)
        voice = f.get("voice", "").strip()
        if not voice:
            src.unlink(missing_ok=True)
            raise WorkbenchError("要说明是哪个音色的样本")
        kind = f.get("sample_kind") if f.get("sample_kind") in ("live", "source") else "live"
        item = wb.gallery.add_file(src, "audio", prompt=f.get("label", ""), source=f"试听·{voice}",
                                   extra={"voice": voice, "sample_kind": kind, "label": f.get("label", "")[:40]})
        return web.json_response(wb.gallery.public(item))

    @routes.post("/api/voices/preview")
    @handle
    async def voice_preview(request, wb: Workbench):
        src, f = await read_audio_form(request, wb)
        out = src.with_name(src.stem + "-preview.wav")
        try:
            await asyncio.to_thread(wb.process_audio, src, out, float(f.get("speed") or 1), float(f.get("pitch") or 0))
            return web.Response(body=out.read_bytes(), content_type="audio/wav")
        finally:
            src.unlink(missing_ok=True)
            out.unlink(missing_ok=True)

    @routes.post("/api/media/image")
    @handle
    async def media_image(request, wb: Workbench):
        return web.json_response({"job": wb.public_job(wb.image_job(await body_of(request)))})

    @routes.post("/api/media/video")
    @handle
    async def media_video(request, wb: Workbench):
        return web.json_response({"job": wb.public_job(wb.video_job(await body_of(request)))})

    @routes.post("/api/media/scene")
    @handle
    async def media_scene(request, wb: Workbench):
        return web.json_response({"job": wb.public_job(wb.scene_job(await body_of(request)))})

    @routes.get("/api/media/jobs")
    @handle
    async def media_jobs(request, wb: Workbench):
        jobs = [wb.public_job(j) for j in wb.jobs[:100]]
        spent = round(sum(j["cost_cny"] or 0 for j in wb.jobs if j["created"] > time.time() - 86400), 2)
        return web.json_response({"jobs": jobs, "spent_cny_24h": spent})

    @routes.get("/api/media/gallery")
    @handle
    async def media_gallery(request, wb: Workbench):
        kind = request.query.get("kind")
        items = [wb.gallery.public(i) for i in wb.gallery.items if not kind or i["kind"] == kind]
        return web.json_response({"items": items})

    @routes.post("/api/media/upload")
    @handle
    async def media_upload(request, wb: Workbench):
        reader = await request.multipart()
        while (part := await reader.next()) is not None:
            if part.name == "file":
                return web.json_response(wb.gallery.public(await wb.save_upload(part)))
        raise WorkbenchError("没有收到文件")

    @routes.get("/api/media/thumb/{item_id}")
    @handle
    async def media_thumb(request, wb: Workbench):
        item = wb.gallery.get(request.match_info["item_id"], "image")
        path = await asyncio.to_thread(wb.gallery.thumb, item)
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    @routes.patch("/api/media/gallery/{item_id}")
    @handle
    async def media_rename(request, wb: Workbench):
        body = await body_of(request)
        item = wb.gallery.get(request.match_info["item_id"])
        label = str(body.get("label") or "").strip()[:60]
        if not label:
            raise WorkbenchError("名字不能为空")
        item.setdefault("full_prompt", item.get("prompt", ""))  # keep the generation prompt
        item["prompt"] = label
        wb.gallery.save()
        return web.json_response(wb.gallery.public(item))

    @routes.delete("/api/media/gallery/{item_id}")
    @handle
    async def media_delete(request, wb: Workbench):
        wb.gallery.delete(request.match_info["item_id"])
        return web.json_response({"ok": True})

    @routes.get("/files/{tail:.+}")
    @handle
    async def files(request, wb: Workbench):
        tail = request.match_info["tail"]
        if any(part.startswith(".") for part in Path(tail).parts):
            raise web.HTTPNotFound()
        path = (wb.assets / tail).resolve()
        if not path.is_relative_to(wb.assets.resolve()) or not path.is_file():
            # profile images live under the project's own assets even when tests point the gallery elsewhere
            path = (wb.root / "assets" / tail).resolve()
            if not path.is_relative_to((wb.root / "assets").resolve()) or not path.is_file():
                raise web.HTTPNotFound()
        headers = {"Cache-Control": "no-cache"}
        if path.suffix.lower() == ".m4a":
            headers["Content-Type"] = "audio/mp4"  # aiohttp's own type table serves m4a as octet-stream
        return web.FileResponse(path, headers=headers)

    app.add_routes(routes)
