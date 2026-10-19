"""One live room: owns the Vidu session, the danmaku queue and the schedule."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import time
from collections import deque
from pathlib import Path

import aiohttp

from .assets import AssetResolver, GenerationError
from .moderation import Moderator
from .queue import CommandQueue
from .router import RANDOM_ACTIONS, VISUAL_KINDS, Command, classify, desk_action, garment_text, interaction_for, prop_scene
from .wardrobe import Wardrobe
from .vidu import (AVATAR_TRANSCRIPT, CONN_INIT_ACK, ERROR, FORCE_HANGUP, PROMPT_OPERATION_ACK,
                   SESSION_UPDATE_ACK, TEXT_ACTION, TEXT_DIALOG, TEXT_MSG, TEXT_TTS, USER_TRANSCRIPT,
                   ControlChannel, ViduClient, ViduError)

ACTIVE = ("creating", "connecting", "waiting_stage", "live")
# Small, varied, everyday motions between replies; the platform default loops visibly.
IDLE_MOTION = {
    "source": "mixed", "recommend": "intelligent",
    "motions": [
        {"name": "撩头发", "content": "用手把耳边的头发撩到耳后", "weight": 2, "description": "说完话或害羞时"},
        {"name": "托腮", "content": "单手托着下巴，歪头看着屏幕上的弹幕", "weight": 2, "description": "等弹幕、听观众说话时"},
        {"name": "低头笑", "content": "低头看一眼手机，然后抿嘴笑一下", "weight": 2, "description": "看到有趣弹幕时"},
        {"name": "凑近屏幕", "content": "身体往前凑近屏幕，眯眼认真看", "weight": 1.5, "description": "看不清弹幕时"},
        {"name": "伸懒腰", "content": "伸一个小小的懒腰，打个哈欠", "weight": 0.8, "description": "直播间安静时"},
        {"name": "喝口水", "content": "拿起桌上的杯子喝一小口水", "weight": 1, "description": "说了比较多话之后"},
        {"name": "整理衣服", "content": "低头整理一下衣领", "weight": 1, "description": "换完衣服后"},
        {"name": "轻轻晃身体", "content": "跟着心情轻轻左右晃了晃身体", "weight": 1, "description": "开心时"},
    ],
    "scheduling": {"motion_interval": 8, "rest_after_motion": 3},
}
STATUS_LABELS = {
    "idle": "未开播", "creating": "创建会话中", "connecting": "连接渲染中", "waiting_stage": "等待舞台页入会",
    "live": "直播中", "ending": "下播中", "ended": "已下播", "error": "出错",
}


def _find_text(value) -> str:
    """Pull the longest human text out of a frame payload whose exact shape is undocumented."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in ("content", "text", "transcript") and isinstance(child, str):
                    found.append(child)
                else:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return max(found, key=len) if found else ""


def _redact_frame(frame: dict) -> dict:
    text = json.dumps(frame, ensure_ascii=False)
    if "base64," not in text:
        return frame

    def scrub(node):
        if isinstance(node, dict):
            return {k: (f"<data-uri {len(v)} chars>" if isinstance(v, str) and v.startswith("data:") else scrub(v))
                    for k, v in node.items()}
        if isinstance(node, list):
            return [scrub(v) for v in node]
        return node

    return scrub(frame)


class LiveRoom:
    def __init__(self, cfg: dict, root: Path, http: aiohttp.ClientSession, api_key: str | None,
                 resolver: AssetResolver, moderator: Moderator, work_dir: Path | None = None):
        self.cfg = cfg
        self.root = root
        self.work = work_dir or root  # logs/ and .state/ live here
        self.http = http
        self.api_key = api_key
        self.vidu = ViduClient(http, api_key or "", cfg["vidu"]["env"], cfg["vidu"].get("base_url")) if api_key else None
        self.resolver = resolver
        self.moderator = moderator
        sched = cfg["schedule"]
        self.queue = CommandQueue(sched["policy"], sched["max_age_seconds"], sched["max_queue"])
        wanted = cfg.get("avatar_profile")
        self.profile = wanted if wanted in cfg["avatars"] else next(iter(cfg["avatars"]))
        self.broadcaster = None  # set by the server when pushing is available
        self._wardrobes: dict[str, Wardrobe] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.logs: deque = deque(maxlen=300)
        self.history: deque = deque(maxlen=60)
        self.filtered: deque = deque(maxlen=60)
        self.output_mode = self._load_output_mode()
        self._reset_session_state()

    def _reset_session_state(self) -> None:
        self.status = "idle"
        self.mode: str | None = None
        self.model = self.cfg["vidu"]["model"]
        self.live: dict | None = None
        self.rtc: dict | None = None
        self.channel: ControlChannel | None = None
        self.ready_at: float | None = None
        self.ended_at: float | None = None
        self.end_reason = ""
        self.stage_joined = False
        self.stage_owner: str | None = None   # only one stage page may join RTC per live session
        self.stage_seen = 0.0
        self.current: Command | None = None
        self.next_at: float | None = None
        self.subtitle = ""
        self.subtitle_at = 0.0
        self.swapping: dict | None = None   # outfit change in progress
        self.voice_override: str | None = None  # voice switched mid-show; outfit swaps keep it
        self.outfit: str | None = None
        self.past_billing = {"seconds": 0, "credits": 0.0}  # sessions retired by outfit changes
        self.visual: dict[str, dict] = {}
        self.pending_ops: deque = deque()
        self.billing = {"billed_seconds": 0, "credits_cost": 0, "rmb": 0.0, "estimate_credits": 0.0}
        self.last_exec_at = time.time()
        self.last_idle_at = 0.0
        self.last_idle_prompt = ""
        self.unknown_frames = 0
        self.frame_file: Path | None = None
        self._kick = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ---------- output ----------
    # Starting the digital human and pushing the stream are separate. The output mode decides which
    # page may take the single RTC seat: the workbench preview, an OBS browser source, or the push page.
    OUTPUT_ROLES = {"local": "preview", "obs": "obs", "builtin": "broadcast"}
    OUTPUT_LABELS = {"local": "只在本地看", "obs": "交给 OBS", "builtin": "内置推流"}

    def _load_output_mode(self) -> str:
        path = self.work / ".state" / "output.json"
        try:
            mode = json.loads(path.read_text()).get("mode")
        except (OSError, ValueError):
            mode = None
        return mode if mode in self.OUTPUT_ROLES else self.cfg.get("output_mode", "local")

    def set_output_mode(self, mode: str) -> None:
        if mode not in self.OUTPUT_ROLES:
            raise ValueError("输出方式只能是 local、obs 或 builtin")
        if self.status in ACTIVE:
            raise PermissionError("数字人还在运行，先停掉再切换输出方式")
        self.output_mode = mode
        path = self.work / ".state" / "output.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mode": mode}))
        self.log("info", f"输出方式改为：{self.OUTPUT_LABELS[mode]}")
        self.broadcast("state")

    def avatar_orient(self) -> str:
        """landscape or portrait, from the base photo; the overlay layer (no video of its own) lays out by it."""
        path = self.root / self.avatar_cfg["image"]
        key = (str(path), path.stat().st_mtime if path.exists() else 0)
        if getattr(self, "_orient_cache", (None, None))[0] != key:
            orient = "portrait"
            try:
                from PIL import Image
                with Image.open(path) as img:
                    orient = "landscape" if img.width > img.height else "portrait"
            except Exception:  # noqa: BLE001
                pass
            self._orient_cache = (key, orient)
        return self._orient_cache[1]

    @property
    def avatar_cfg(self) -> dict:
        return self.cfg["avatars"][self.profile]

    def wardrobe(self) -> Wardrobe | None:
        manifest = self.avatar_cfg.get("outfits")
        if not manifest:
            return None
        if self.profile not in self._wardrobes:
            self._wardrobes[self.profile] = Wardrobe(self.root, manifest)
        return self._wardrobes[self.profile]

    # ---------- events ----------
    def snapshot(self) -> dict:
        now = time.time()
        return {
            "status": self.status, "status_label": STATUS_LABELS.get(self.status, self.status),
            "mode": self.mode, "model": self.model, "end_reason": self.end_reason,
            "live_id": (self.live or {}).get("id"), "rtc_ready": bool(self.rtc) and self.status in ("waiting_stage", "live"),
            "stage_joined": self.stage_joined, "stage_owner": bool(self.stage_owner),
            "elapsed": int((self.ended_at or now) - self.ready_at) if self.ready_at else 0,
            "billing": self.billing, "budget_credits": self.cfg["safety"]["budget_credits"],
            "max_session_minutes": self.cfg["safety"]["max_session_minutes"],
            "credit_price_rmb": self.cfg["vidu"]["credit_price_rmb"],
            "interval_seconds": self.cfg["schedule"]["interval_seconds"], "policy": self.queue.policy,
            "next_in": max(0, round(self.next_at - now)) if self.next_at and self.status in ("waiting_stage", "live") else None,
            "current": self.current.public() if self.current else None,
            "queue": [c.public() for c in self.queue.waiting()],
            "history": [c.public() for c in self.history],
            "filtered": list(self.filtered),
            "subtitle": self.subtitle if now - self.subtitle_at < 10 else "",
            "broadcast": self.broadcaster.public() if self.broadcaster else None,
            "visual": {k: v["name"] for k, v in self.visual.items()},
            "swapping": self.swapping, "outfit": self.outfit,
            "voice": self.voice_override or self.avatar_cfg.get("voice", ""),
            "avatar_name": self.avatar_cfg["name"], "profile": self.profile,
            "profiles": [{"key": k, "name": v["name"], "note": v.get("note", "")} for k, v in self.cfg["avatars"].items()],
            "generator": self.resolver.generator is not None,
            "has_api_key": bool(self.api_key),
            "output_mode": self.output_mode, "stage_role": self.OUTPUT_ROLES[self.output_mode],
            "avatar_orient": self.avatar_orient(),
        }

    def broadcast(self, event: str, data: dict | None = None) -> None:
        message = {"event": event, "data": data if data is not None else self.snapshot()}
        for q in list(self.subscribers):
            if q.qsize() < 200:
                q.put_nowait(message)

    def log(self, level: str, message: str, **data) -> None:
        entry = {"t": time.time(), "level": level, "message": message, **({"data": data} if data else {})}
        self.logs.append(entry)
        self.broadcast("log", entry)

    def _frame_log(self, direction: str, frame: dict) -> None:
        if self.frame_file:
            with self.frame_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"t": time.time(), "dir": direction, "frame": _redact_frame(frame)}, ensure_ascii=False) + "\n")

    # ---------- danmaku ----------
    async def ingest(self, user: str, text: str, gift: float = 0.0, source: str = "simulator") -> dict:
        user = (user or "观众").strip()[:20]
        text = (text or "").strip()
        if text:
            verdict = self.moderator.check(user, text)
            if not verdict.ok:
                item = {"t": time.time(), "user": user, "text": text, "category": verdict.category, "reason": verdict.reason, "source": source}
                self.filtered.appendleft(item)
                self.broadcast("state")
                return {"accepted": False, **item}
        cmd = classify(user, text, gift)
        desk = self.avatar_cfg.get("desk_items") or {}
        if cmd.kind == "object" and not cmd.parts and desk:
            action = desk_action(cmd.text, desk)
            if action:
                cmd.kind, cmd.action_text, cmd.note = "action", action, "用桌上现成的东西"
        wardrobe = self.wardrobe()
        garment_part = next((p for p in cmd.parts if p.kind == "garment"), None)
        if wardrobe and (cmd.kind == "reset" or cmd.kind == "garment" or garment_part):
            return self._queue_outfit(cmd, wardrobe, garment_part)
        if cmd.kind == "reset" and self.model != "vidu-s2":
            cmd.kind, cmd.note = "chat", "当前模型不支持换装换景，按聊天处理"
        if cmd.kind in VISUAL_KINDS:
            if self.model != "vidu-s2":
                cmd.kind, cmd.parts, cmd.note = "chat", [], "当前模型不支持换装换景，按聊天处理"
            else:
                missing = False
                for part in cmd.parts or [cmd]:
                    part.item = self.resolver.pick_name(part.kind, part.item)
                    if not part.user_text:
                        part.user_text = {"garment": garment_text(part.item),
                                          "object": prop_scene(part.item), "background": f"把背景换成{part.item}"}[part.kind]
                    if part.kind == "object" and not part.action_text:
                        part.action_text = interaction_for(part.item)
                    found = self.resolver.library.find(part.kind, part.item)
                    if found:
                        part.asset = found.to_json()
                    else:
                        missing = True
                if cmd.parts:
                    cmd.item = "、".join(p.item for p in cmd.parts)
                    cmd.user_text = "，".join(p.user_text for p in cmd.parts)
                if missing and self.resolver.generator:
                    cmd.status, cmd.note = "preparing", "正在生成参考图"
                    asyncio.create_task(self._prepare(cmd))
                elif missing:
                    cmd.status, cmd.note = "failed", "素材库里没有，生图未开启"
                    self.history.appendleft(cmd)
                    self.broadcast("state")
                    return {"accepted": True, "command": cmd.public()}
        self.queue.push(cmd)
        self.broadcast("state")
        return {"accepted": True, "command": cmd.public()}

    def _queue_outfit(self, cmd: Command, wardrobe: Wardrobe, garment_part: Command | None) -> dict:
        """Outfit changes restart the Vidu session from a whole-outfit photo (see wardrobe.py)."""
        wanted = garment_part.item if garment_part else cmd.item
        if cmd.kind == "reset":
            entry = wardrobe.base()
        elif wanted:
            entry = wardrobe.find(wanted)
        else:
            entry = wardrobe.random_other(self.outfit)
        if garment_part:
            cmd.note = "一条弹幕里的换装和其他画面变化，只执行换装"
        cmd.kind, cmd.parts = "outfit", []
        if entry:
            cmd.item, cmd.asset = entry["name"], entry
        elif self.resolver.generator:
            cmd.item, cmd.status, cmd.note = wanted, "preparing", "衣柜里没有，正在生成这套衣服"
            asyncio.create_task(self._make_outfit(cmd, wardrobe, wanted))
        else:
            cmd.item, cmd.status, cmd.note = wanted, "failed", "衣柜里没有这套，生图未开启"
            self.history.appendleft(cmd)
            self.broadcast("state")
            return {"accepted": True, "command": cmd.public()}
        self.queue.push(cmd)
        self.broadcast("state")
        return {"accepted": True, "command": cmd.public()}

    async def _make_outfit(self, cmd: Command, wardrobe: Wardrobe, item: str) -> None:
        started = time.time()
        try:
            cmd.asset = await wardrobe.make(self.http, self.resolver.generator, item)
        except (GenerationError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            cmd.status, cmd.note = "failed", f"生成衣服失败：{error}"[:160]
            if cmd in self.queue.items:
                self.queue.items.remove(cmd)
            self.history.appendleft(cmd)
        else:
            if cmd.status == "preparing":
                cmd.status = "ready"
            cmd.note = f"新衣服已生成，用时 {time.time() - started:.0f}s"
        self.broadcast("state")

    async def _prepare(self, cmd: Command) -> None:
        started = time.time()
        try:
            for part in cmd.parts or [cmd]:
                if not part.asset:
                    asset = await self.resolver.resolve(self.http, part.kind, part.item, part.user_text or "")
                    part.asset = asset.to_json()
        except (GenerationError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            cmd.status, cmd.note = "failed", f"参考图生成失败：{error}"[:160]
            if cmd in self.queue.items:
                self.queue.items.remove(cmd)
            self.history.appendleft(cmd)
            self.log("warn", f"#{cmd.id} 参考图生成失败", error=str(error)[:300])
        else:
            if cmd.status == "preparing":
                cmd.status = "ready"
            cmd.note = f"参考图就绪，用时 {time.time() - started:.0f}s"
        self.broadcast("state")

    # ---------- lifecycle ----------
    async def start(self, model: str | None = None, dry_run: bool = False, profile: str | None = None) -> dict:
        if self.status in ACTIVE or self.status == "ending":
            raise RuntimeError("已经在直播中")
        if profile:
            if profile not in self.cfg["avatars"]:
                raise RuntimeError(f"没有这个形象：{profile}")
            self.profile = profile
        self._reset_session_state()
        self.model = model or self.cfg["vidu"]["model"]
        if dry_run:
            self.mode, self.status, self.ready_at = "dry_run", "live", time.time()
            self.log("info", "空跑模式开播：不调用 Vidu，只演示排队和舞台叠层")
            self._spawn(self._scheduler())
            self.broadcast("state")
            return self.snapshot()
        if not self.vidu:
            raise RuntimeError("缺少 Vidu API Key（~/.deepsea/vidu/api.key 或 VIDU_API_KEY）")
        self.mode, self.status = "vidu", "creating"
        self.broadcast("state")
        try:
            wardrobe = self.wardrobe()
            self.outfit = wardrobe.base()["name"] if wardrobe else None
            avatar_ref = await self._ensure_avatar()
            response = await self.vidu.create_live(self._create_body(avatar_ref))
            self.live, self.rtc = response.get("live") or {}, response.get("rtc") or {}
            live_id = str(self.live.get("id"))
            self.live["id"] = live_id
            logs = self.work / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            self.frame_file = logs / f"live-{time.strftime('%Y%m%d-%H%M%S')}-{live_id}.jsonl"
            self.log("info", "Vidu 会话已创建", live_id=live_id, rtc_provider=self.rtc.get("provider"),
                     live_duration=self.live.get("live_duration"))
            self.status = "connecting"
            self.broadcast("state")
            self.channel = self._new_channel(live_id)
            await self.channel.connect()
            await self.channel.conn_init()
            self._spawn(self._await_ready())
        except Exception as error:
            self.status, self.end_reason = "error", str(error)[:300]
            self.log("error", "开播失败", error=str(error)[:500])
            if self.channel:
                await self.channel.close()
            self.broadcast("state")
            raise
        return self.snapshot()

    def _create_body(self, avatar_ref: dict, greeting: str | None = None) -> dict:
        vcfg, acfg = self.cfg["vidu"], self.avatar_cfg
        persona = (self.root / acfg["persona_file"]).read_text(encoding="utf-8").replace("{name}", acfg["name"])
        body = {
            "model": self.model,
            "call_mode": vcfg["call_mode"],
            "rtc": {"provider": vcfg["rtc_provider"]},
            "avatar": {"persona": persona, "voice": self.voice_override or acfg.get("voice", ""), **avatar_ref},
            "audio": {"enable_transcription": True},
            "action": {"enabled": True},
            "idle_motion": {**IDLE_MOTION, "motions": acfg["idle_motions"]} if acfg.get("idle_motions") else IDLE_MOTION,
            "llm": {"temperature": 0.8, "max_tokens": vcfg["max_tokens"]},
            "idle_timeout_seconds": 7200,
            "enable_recording": bool(vcfg.get("enable_recording")),
        }
        if acfg.get("voice_model"):  # e.g. {"provider": "doubao", "doubao": {"voice": "zh_female_vv_uranus_bigtts"}}
            body["voice_model"] = acfg["voice_model"]
        if greeting or acfg.get("greeting_instruction"):
            body["avatar"]["greeting_instruction"] = (greeting or acfg["greeting_instruction"])[:200]
        return body

    async def _ensure_avatar(self, image: Path | None = None, quiet: bool = False) -> dict:
        image = image or self.root / self.avatar_cfg["image"]
        raw = image.read_bytes()
        mime = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
        data_uri = f"data:{mime};base64," + base64.b64encode(raw).decode()
        cache_file = self.work / ".state" / "avatars.json"
        cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
        key = f"{self.cfg['vidu']['env']}:{self.cfg['vidu'].get('base_url') or ''}:{hashlib.sha256(raw).hexdigest()[:16]}"
        if key in cache and time.time() - cache[key]["created"] < 80 * 86400:
            return {"id": cache[key]["id"]}
        try:
            created = await self.vidu.upload_avatar(data_uri, self.avatar_cfg["name"])
            avatar_id = str(created["id"])
            for _ in range(30):
                state = await self.vidu.get_avatar(avatar_id)
                status = str(state.get("status", ""))
                if status in ("success", "11"):
                    break
                if status in ("failed", "12"):
                    raise ViduError(200, f"形象资产生成失败 {state}")
                await asyncio.sleep(2)
            else:
                raise ViduError(200, "形象资产 60s 内没有就绪")
        except (ViduError, aiohttp.ClientError, KeyError) as error:
            self.log("warn", "形象图上传未成功，本场直接传图片创建（创建会慢一些）", error=str(error)[:300])
            return {"image_uri": data_uri}
        cache[key] = {"id": avatar_id, "created": time.time()}
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, indent=2))
        self.log("info", f"形象图已上传为 Vidu 资产{'（预上传）' if quiet else ''}", avatar_id=avatar_id, image=image.name)
        return {"id": avatar_id}

    async def _await_ready(self) -> None:
        try:
            await asyncio.wait_for(self.channel.ready.wait(), timeout=150)
        except asyncio.TimeoutError:
            await self.stop("渲染侧 150 秒内没有就绪")
            return
        self.ready_at = time.time()
        self.status = "waiting_stage"
        self.log("info", "渲染侧就绪，舞台页可以入会了；计费从这里开始")
        self.broadcast("state")
        self._spawn(self._scheduler())
        self._spawn(self._watchdog())
        self._spawn(self._prewarm_wardrobe())

    async def stop(self, reason: str = "手动下播") -> dict:
        if self.status not in ACTIVE:
            return self.snapshot()
        self.status, self.end_reason = "ending", reason
        self.broadcast("state")
        if self.channel:
            try:
                await self.channel.hangup()
            except Exception as error:  # noqa: BLE001
                self.log("warn", "挂断信令发送失败", error=str(error)[:200])
        await self._finish(reason)
        return self.snapshot()

    async def _finish(self, reason: str) -> None:
        if self.status in ("ended", "error", "idle"):
            return
        self.status, self.end_reason, self.ended_at = "ended", reason, time.time()
        self.next_at = None
        for task in self._tasks:
            if task is not asyncio.current_task():
                task.cancel()
        self.log("info", f"已下播：{reason}")
        self.broadcast("state")
        if self.broadcaster and self.broadcaster.state == "live":
            await self.broadcaster.stop("已下播")
        if self.mode == "vidu" and self.live:
            await asyncio.sleep(2)
            await self._refresh_billing()
            self.broadcast("state")

    def _spawn(self, coro) -> None:
        self._tasks.append(asyncio.create_task(coro))

    # ---------- control channel ----------
    async def _on_frame(self, frame: dict) -> None:
        kind, payload = frame.get("type"), frame.get("payload") or {}
        if kind == CONN_INIT_ACK:
            ack = payload.get("conn_init_ack") or {}
            if ack.get("success"):
                return
            if ack.get("error_code") == "NOT_READY":
                return  # the channel retries conn_init by itself
            else:
                self.log("error", "控制通道初始化失败", ack=ack)
                await self.stop(f"初始化失败 {ack.get('error_code')}")
        elif kind == FORCE_HANGUP:
            reason = (payload.get("hangup") or {}).get("hangup_reason", "unknown")
            labels = {"timeout": "会话超时", "audit_violation": "触发 Vidu 风控", "credit_insufficient": "积分不足"}
            self.log("error" if reason != "timeout" else "warn", f"Vidu 强制挂断：{labels.get(reason, reason)}")
            await self._finish(f"Vidu 强制挂断：{labels.get(reason, reason)}")
        elif kind == AVATAR_TRANSCRIPT:
            text = _find_text(payload)
            if text and not self.swapping:  # the new session's opening line is unheard until the stage switches
                self.subtitle, self.subtitle_at = text, time.time()
                self.broadcast("subtitle", {"text": text})
        elif kind == USER_TRANSCRIPT:
            text = _find_text(payload)
            if text:
                self.log("debug", "用户音频转写", text=text)
        elif kind == PROMPT_OPERATION_ACK:
            ack = payload.get("prompt_operation_ack") or {}
            cmd = self.pending_ops.popleft() if self.pending_ops else None
            if cmd:
                cmd.acked_at = time.time()
                if not ack.get("success"):
                    cmd.status, cmd.note = "failed", f"换图失败 {ack.get('error_code')} {ack.get('error_msg', '')}"[:160]
                else:
                    cmd.note = f"换图回执 {cmd.acked_at - (cmd.executed_at or cmd.acked_at):.1f}s"
                    props = [p for p in (cmd.parts or [cmd]) if p.kind == "object" and p.action_text]
                    if props:
                        self._spawn(self._interact(props[0].action_text, (self.visual.get("object") or {}).get("name")))
            self.log("info" if ack.get("success") else "warn", "参考图切换回执", ack=ack)
            self.broadcast("state")
        elif kind == SESSION_UPDATE_ACK:
            ack = payload.get("session_update_ack") or {}
            self.log("info" if ack.get("success", True) else "warn", "会话配置更新回执", ack=ack)
        elif kind == ERROR:
            self.log("error", "Vidu 返回错误", payload=payload)
        else:
            self.unknown_frames += 1
            if self.unknown_frames <= 30:
                self.log("debug", f"收到未处理信令 type={kind}", payload=_redact_frame(payload))

    def _new_channel(self, live_id: str, conn_id: str | None = None) -> ControlChannel:
        """Frames and closes from a channel retired by an outfit change must not touch the room."""
        channel = ControlChannel(self.vidu, live_id, None, None, self._frame_log, conn_id=conn_id)

        async def on_message(frame, ch=channel):
            if ch is self.channel:
                await self._on_frame(frame)

        async def on_closed(reason, ch=channel):
            if ch is self.channel:
                await self._on_closed(reason)

        channel.on_message, channel.on_closed = on_message, on_closed
        return channel

    async def _prewarm_wardrobe(self, require_live: bool = True) -> dict:
        """Vidu needs about a minute to process a new avatar image; do it before anyone asks (in parallel)."""
        wardrobe = self.wardrobe()
        if not wardrobe or not self.vidu:
            return {}
        results: dict = {}

        async def one(entry):
            if require_live and self.status not in ("waiting_stage", "live"):
                return
            try:
                await self._ensure_avatar(self.root / entry["file"], quiet=True)
                results[entry["name"]] = "ok"
            except Exception as error:  # noqa: BLE001
                results[entry["name"]] = str(error)[:120]
                self.log("warn", f"预上传底图失败：{entry['name']}", error=str(error)[:200])

        await asyncio.gather(*(one(e) for e in list(wardrobe.items)))
        return results

    async def _swap_outfit(self, cmd: Command) -> None:
        entry = cmd.asset or {}
        name = entry.get("name", cmd.item)
        if name == self.outfit:
            await self._send(TEXT_DIALOG, f"【弹幕】观众{cmd.user}让你换上{name}，但你已经穿着了，随口吐槽一句。", "换装台词")
            return
        if self.mode == "dry_run":
            self.outfit = name
            self.log("info", f"[空跑] 换装重开会话：{name}")
            return
        self.swapping = {"to": name, "user": cmd.user, "since": time.time()}
        segments_before = self.broadcaster.segments if self.broadcaster else 0
        self.broadcast("state")
        channel = None
        try:
            avatar_ref = await self._ensure_avatar(self.root / entry["file"])
            # Stay quiet until the stage shows the new outfit, otherwise viewers hear about a dress they cannot
            # see yet. Keep the opening line neutral: a quoted line gets read out word for word ("好哒，等你哈"),
            # and "about to change" primes the reply to the 换装完成 message below ("好，马上就好").
            response = await self.vidu.create_live(self._create_body(avatar_ref, "只用一个很轻的语气词应一声，不要说别的"))
            live, rtc = response.get("live") or {}, response.get("rtc") or {}
            live["id"] = str(live.get("id"))
            channel = self._new_channel(live["id"])
            await channel.connect()
            await channel.conn_init()
            await asyncio.wait_for(channel.ready.wait(), timeout=60)
        except Exception as error:  # noqa: BLE001
            self.swapping = None
            cmd.status, cmd.note = "failed", f"换装失败：{error}"[:160]
            self.log("error", "换装重开会话失败，继续用原来的会话", error=str(error)[:300])
            if channel:
                await channel.close()
            self.broadcast("state")
            return
        old_channel, old_live = self.channel, self.live
        self.channel, self.live, self.rtc = channel, live, rtc
        self.stage_owner, self.stage_joined = None, False
        self.outfit = name
        self.visual.clear()
        self.pending_ops.clear()
        self.log("info", f"换装：新会话已就绪（{name}），等舞台页切过去", live_id=live["id"])
        self.broadcast("state")
        for _ in range(50):
            if self.stage_joined:
                break
            await asyncio.sleep(0.5)
        else:
            self.log("warn", "舞台页 25 秒内没有切到新会话")
        self._spawn(self._retire(old_channel, old_live))
        try:
            await self._wait_new_frames(segments_before)
        finally:
            self.swapping = None  # never leave the notice up, even if waiting fails
        cmd.note = f"换装用时 {time.time() - cmd.executed_at:.0f}s"
        self.broadcast("state")
        await self._send(TEXT_MSG, f"【换装完成】{cmd.user}点的{name}已经换好了，观众现在能看到。用你自己的话说一句穿着的真实感受，不要问观众好不好看。", "换装台词")

    async def _wait_new_frames(self, segments_before: int) -> None:
        """RTC "joined" is not "on screen": when pushing, wait for the new session's frames to reach the
        encoder (plus the jitter buffer and crossfade) before dropping the notice and talking about it."""
        b = self.broadcaster
        if b and b.state == "live":
            for _ in range(24):
                if b.segments > segments_before and b.receiving:
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(2)
        else:
            await asyncio.sleep(1.5)

    async def _retire(self, channel: ControlChannel, live: dict) -> None:
        try:
            await channel.hangup()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(3)
        try:
            old = (await self.vidu.get_live(live["id"])).get("live") or {}
            self.past_billing["seconds"] += int(float(old.get("billed_seconds") or 0))
            self.past_billing["credits"] += float(old.get("credits_cost") or 0)
        except Exception as error:  # noqa: BLE001
            self.log("warn", "查询旧会话计费失败", error=str(error)[:200])
        self.log("info", "旧会话已挂断", live_id=live.get("id"))

    async def _interact(self, action: str, prop: str | None = None) -> None:
        """Use the prop once it is on screen (Vidu needs a moment after the ack to render it), then put it
        down. A prop left in the reference set stays in her hand for the rest of the show."""
        await asyncio.sleep(2)
        if not (self.status in ("waiting_stage", "live") and self.channel):
            return
        await self.channel.text(TEXT_ACTION, action)
        self.log("info", "道具互动动作", action=action)
        await asyncio.sleep(float(self.cfg["schedule"].get("prop_hold_seconds", 10)))
        state = self.visual.get("object")
        if not (state and state["name"] == prop and self.status in ("waiting_stage", "live") and self.channel and not self.swapping):
            return  # already replaced, reset or swapped away
        self.visual.pop("object", None)
        if state.get("image_id"):
            self.pending_ops.append(Command("系统", f"放下{prop}", "object"))  # keeps acks paired with their commands
            await self.channel.remove_images([state["image_id"]])
        await self.channel.text(TEXT_ACTION, f"把手里的{prop}放到一边，空着手")
        self.log("info", f"道具用完放下：{prop}")
        self.broadcast("state")

    async def _on_closed(self, reason: str) -> None:
        if self.status not in ACTIVE:
            return
        self.log("warn", f"控制通道断开：{reason}，尝试在 Vidu 的宽限期内重连")
        old = self.channel
        for attempt in range(1, 4):
            await asyncio.sleep(1.5 * attempt)
            if self.status not in ACTIVE:
                return
            try:
                channel = self._new_channel(old.live_id, conn_id=old.conn_id)
                await channel.connect()
                await channel.conn_init()
                await asyncio.wait_for(channel.ready.wait(), timeout=15)
            except Exception as error:  # noqa: BLE001
                self.log("warn", f"第 {attempt} 次重连失败", error=str(error)[:200])
                continue
            self.channel = channel
            self.pending_ops.clear()
            self.log("info", f"控制通道已重连（第 {attempt} 次）")
            return
        await self._finish(f"控制通道断开且重连失败：{reason}")

    def claim_stage(self, client_id: str, role: str = "") -> dict:
        """Hand the RTC join credentials to exactly one stage page.

        Vidu treats the first RTC participant as the App client. If a second page joins with
        the same identity and then leaves (a reload, a stray tab), Vidu starts its reconnect
        timer and closes the whole session about a minute later, even if another page rejoins.
        """
        if self.status not in ("waiting_stage", "live") or not self.rtc:
            raise PermissionError("还没有可入会的会话")
        wanted = self.OUTPUT_ROLES[self.output_mode]
        if (role or "preview") != wanted:
            # e.g. in OBS mode the workbench preview must not take the seat after an outfit swap
            where = {"local": "工作台的预览", "obs": "OBS 里的数字人图层", "builtin": "推流舞台页"}[self.output_mode]
            raise PermissionError(f"现在的输出方式是{self.OUTPUT_LABELS[self.output_mode]}，画面交给{where}")
        now = time.time()
        if self.stage_owner and self.stage_owner != client_id and now - self.stage_seen < 15:
            raise PermissionError("另一个舞台页正在使用这场直播的画面")
        if self.stage_owner != client_id:
            if self.stage_owner:
                self.log("warn", "舞台页换人：上一个舞台页 15 秒没有心跳，Vidu 可能会因为原客户端断开而关闭本场")
            self.stage_owner = client_id
        self.stage_seen = now
        return self.rtc

    def stage_heartbeat(self, client_id: str) -> bool:
        if client_id and client_id == self.stage_owner:
            self.stage_seen = time.time()
            return True
        return False

    def release_stage(self, client_id: str, why: str) -> None:
        if client_id and client_id == self.stage_owner:
            self.stage_owner = None
            was_live = self.status in ("waiting_stage", "live")
            self.stage_joined = False
            if was_live:
                self.log("error", f"舞台页断开（{why}）。Vidu 约 60 秒后会因客户端掉线关闭本场，别再刷新舞台页，必要时重新开播")
            self.broadcast("state")

    async def stage_event(self, event: str, detail: str = "") -> None:
        before = (self.status, self.stage_joined)
        if event == "joined":
            self.stage_joined = True
            if self.status == "waiting_stage":
                self.status = "live"
            self.log("info", "舞台页已入会 RTC")
        elif event == "remote_video":
            self.log("info", "舞台页收到数字人画面", detail=detail)
        else:
            self.stage_joined = event != "left" and self.stage_joined
            self.log("warn" if event == "error" else "info", f"舞台页事件：{event}", detail=detail[:300])
        if (self.status, self.stage_joined) != before:
            self.broadcast("state")

    # ---------- scheduling ----------
    async def _scheduler(self) -> None:
        first = True
        while self.status in ("waiting_stage", "live"):
            interval = 4 if first else self.cfg["schedule"]["interval_seconds"]
            first = False
            self.next_at = time.time() + interval
            self.broadcast("state")
            self._kick.clear()
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self.status not in ("waiting_stage", "live"):
                break
            for gone in self.queue.expire():
                self.history.appendleft(gone)
            cmd = self.queue.pick()
            if cmd:
                await self._execute(cmd)
            else:
                await self._maybe_idle()

    def kick(self) -> None:
        self._kick.set()

    async def _maybe_idle(self) -> None:
        idle_after = self.cfg["schedule"]["idle_prompt_after_seconds"]
        now = time.time()
        if idle_after and now - self.last_exec_at > idle_after and now - self.last_idle_at > idle_after:
            self.last_idle_at = now
            prompts = [
                "【暖场】直播间有点安静，随口说一件今天身边的小事。",
                "【暖场】没人说话，自言自语一句你现在在干嘛，或者吐槽一句。",
                "【暖场】随口问一句大家这会儿在干嘛。",
            ]
            wardrobe = self.wardrobe()
            if wardrobe and len(wardrobe.items) > 1:  # only mention outfits when there are some to change into
                prompts.append("【暖场】随口提一句可以发弹幕让你换身衣服。")
            prompt = random.choice([p for p in prompts if p != self.last_idle_prompt] or prompts)  # same prompt twice gets the same line twice
            self.last_idle_prompt = prompt
            await self._send(TEXT_MSG, prompt, label="暖场")

    async def _send(self, signal_type: int, content: str, label: str = "") -> None:
        if self.mode == "dry_run":
            self.log("info", f"[空跑] {label or signal_type}：{content}")
            return
        await self.channel.text(signal_type, content)

    async def _execute(self, cmd: Command) -> None:
        cmd.status, cmd.executed_at = "executing", time.time()
        self.current, self.last_exec_at = cmd, time.time()
        self.broadcast("state")
        try:
            if cmd.kind == "chat":
                await self._send(TEXT_MSG, f"【弹幕】{cmd.user}：{cmd.text}", "聊天")
            elif cmd.kind == "action":
                action = cmd.action_text or random.choice(RANDOM_ACTIONS)
                await self._send(TEXT_ACTION, action, "动作")
                await self._send(TEXT_DIALOG, f"【指令执行】观众{cmd.user}让你{cmd.text}，你正在做「{action}」，按你的性格随口接一句，可以吐槽，别用应答腔。", "动作台词")
            elif cmd.kind in VISUAL_KINDS:
                await self._switch_visual(cmd)
                await self._send(TEXT_DIALOG, f"【指令执行】观众{cmd.user}让你{cmd.user_text}，画面正在变化，按你的性格随口接一句。", "换装台词")
            elif cmd.kind == "outfit":
                await self._swap_outfit(cmd)
            elif cmd.kind == "reset":
                await self._reset_look(cmd)
                await self._send(TEXT_DIALOG, f"【指令执行】观众{cmd.user}让你恢复原来的样子，随口接一句。", "复原台词")
            elif cmd.kind == "gift_thanks":
                await self._send(TEXT_DIALOG, f"【礼物】观众{cmd.user}刚刚送了礼物，真诚又简短地感谢一句。", "礼物感谢")
            if cmd.status == "executing":
                cmd.status = "done"
        except Exception as error:  # noqa: BLE001 - one bad command must not stop the show
            cmd.status, cmd.note = "failed", f"执行失败：{error}"[:160]
            self.log("error", f"#{cmd.id} 执行失败", error=str(error)[:300])
        self.history.appendleft(cmd)
        self.broadcast("state")

    async def _reset_look(self, cmd: Command) -> None:
        """Removing references does not undo them: realtime video keeps evolving from the current
        frame. So a reset actively switches back to the profile's base outfit and puts props down."""
        had_prop = "object" in self.visual
        base = self.avatar_cfg.get("base_garment")
        asset = self.resolver.library.find("garment", base) if base else None
        self.visual.clear()
        if self.mode == "dry_run":
            self.log("info", f"[空跑] 复原：{base or '移除参考图'}")
            return
        self.pending_ops.append(cmd)
        if asset:
            self.visual["garment"] = {"name": asset.name, "asset": asset, "user_text": asset.user_text or f"整套换回{asset.name}"}
            await self.channel.switch_images([{"image_uri": self.resolver.library.data_uri(asset), "image_id": f"garment-{cmd.id}",
                                               "kind": "garment", "user_text": self.visual["garment"]["user_text"]}])
        else:
            await self.channel.remove_images()
        if had_prop:
            await self.channel.text(TEXT_ACTION, "把手里的东西放到一边，空着手")

    async def _switch_visual(self, cmd: Command) -> None:
        prop_before = self.visual.get("object")
        # Props are one-shot: a new outfit or background should not keep re-rendering the old snack.
        if prop_before and not any(p.kind == "object" for p in cmd.parts or [cmd]):
            self.visual.pop("object", None)
            if self.mode != "dry_run":
                await self.channel.text(TEXT_ACTION, f"把手里的{prop_before['name']}放到一边")
        for part in cmd.parts or [cmd]:
            if not part.asset:
                raise RuntimeError("没有可用的参考图")
            asset = self.resolver.library.find(part.kind, part.asset["name"])
            if asset is None:
                raise RuntimeError(f"参考图文件不见了：{part.asset.get('file')}")
            self.visual[part.kind] = {"name": asset.name, "asset": asset, "user_text": part.user_text}
        if self.mode == "dry_run":
            self.log("info", "[空跑] 换图：" + "，".join(f"{p.kind} → {p.item}" for p in cmd.parts or [cmd]))
            return
        # A switch replaces the whole reference set, so the current outfit and background ride along.
        images = []
        for kind in ("background", "garment", "object"):
            state = self.visual.get(kind)
            if not state:
                continue
            state["image_id"] = f"{kind}-{cmd.id}"
            images.append({"image_uri": self.resolver.library.data_uri(state["asset"]), "image_id": state["image_id"],
                           "kind": kind, "user_text": state["user_text"] or ""})
        self.pending_ops.append(cmd)
        await self.channel.switch_images(images[:3])

    async def set_voice(self, voice: str) -> None:
        """Switch the voice of the running show; Vidu applies it after the current sentence."""
        if self.status not in ("waiting_stage", "live"):
            raise RuntimeError("还没开播")
        voice = voice.strip()
        if not voice:
            raise ValueError("音色不能为空")
        if self.mode != "dry_run":
            await self.channel.update_session({"avatar": {"voice": voice}})
        self.voice_override = voice
        self.log("info", f"直播中切换音色：{voice}")
        self.broadcast("state")

    async def manual(self, kind: str, content: str) -> None:
        if self.status not in ("waiting_stage", "live"):
            raise RuntimeError("还没开播")
        signal = {"text_msg": TEXT_MSG, "text_action": TEXT_ACTION, "text_dialog": TEXT_DIALOG, "text_tts": TEXT_TTS}
        if kind == "interrupt":
            if self.mode != "dry_run":
                await self.channel.interrupt()
        elif kind == "reset":
            await self._execute(Command("运营", "换回来", "reset"))
        elif kind in signal:
            await self._send(signal[kind], content, f"手动 {kind}")
        else:
            raise ValueError(f"不支持的手动信令 {kind}")
        self.log("info", f"手动发送 {kind}", content=content)

    # ---------- cost guard ----------
    async def _refresh_billing(self) -> None:
        try:
            live = (await self.vidu.get_live(self.live["id"])).get("live") or {}
        except Exception as error:  # noqa: BLE001
            self.log("warn", "查询计费失败", error=str(error)[:200])
            return
        billed = int(float(live.get("billed_seconds") or 0)) + self.past_billing["seconds"]
        credits = float(live.get("credits_cost") or 0) + self.past_billing["credits"]
        self.billing.update(billed_seconds=billed, credits_cost=credits,
                            rmb=round(credits * self.cfg["vidu"]["credit_price_rmb"], 2))

    async def _watchdog(self) -> None:
        safety, vcfg = self.cfg["safety"], self.cfg["vidu"]
        ticks = 0
        while self.status in ("waiting_stage", "live"):
            await asyncio.sleep(15)
            ticks += 1
            elapsed = time.time() - (self.ready_at or time.time())
            self.billing["estimate_credits"] = round(elapsed * vcfg["credits_per_second"], 1)
            if self.stage_owner and time.time() - self.stage_seen > 15:
                self.release_stage(self.stage_owner, "15 秒没有心跳")
            join_limit = safety.get("stage_join_timeout_seconds", 90)
            if self.status == "waiting_stage" and not self.stage_joined and elapsed >= join_limit:
                # Vidu bills from render-ready; nobody watching means money for nothing
                where = {"local": "工作台的预览", "obs": "OBS 里的数字人图层", "builtin": "推流舞台页"}[self.output_mode]
                await self.stop(f"{join_limit} 秒没有画面入会（该由{where}接），自动停止，免得空计费")
                return
            if ticks % 2 == 0:
                await self._refresh_billing()
            spent = max(self.billing["credits_cost"], self.billing["estimate_credits"])
            if spent >= safety["budget_credits"]:
                await self.stop(f"达到单场预算上限 {safety['budget_credits']} 积分")
                return
            if elapsed >= safety["max_session_minutes"] * 60:
                await self.stop(f"达到单场时长上限 {safety['max_session_minutes']} 分钟")
                return
            self.broadcast("state")
