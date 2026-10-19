"""One-off comparison runs of the other two Vidu S2 products.

component: Vidu only renders; we send our own speech (TTS lines and a song) as 24 kHz PCM
           over the external-live WebSocket, with the reply text, to test lip sync and singing.
editing:   a driver video (published by the trial page as its "camera") is re-rendered live with
           the subject replaced by our avatar image (subject_replacement).
The trial page (static/trial.html) joins the RTC channel and forwards the result to the
broadcaster, so both land in logs/broadcast-*.flv like a normal show.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import subprocess
import time
import uuid
from pathlib import Path

import aiohttp

PCM_RATE = 24000
FRAME_BYTES = PCM_RATE * 2 // 10  # 100 ms of s16le mono


def to_pcm(path: Path, start: float = 0.0, duration: float | None = None) -> bytes:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-i", str(path)]
    if duration:
        args += ["-t", str(duration)]
    args += ["-ac", "1", "-ar", str(PCM_RATE), "-f", "s16le", "pipe:1"]
    return subprocess.run(args, check=True, capture_output=True).stdout


class Trial:
    def __init__(self, room, mode: str, image: Path, driver_url: str | None = None, script: list[dict] | None = None):
        self.room = room
        self.mode = mode
        self.image = image
        self.driver_url = driver_url
        self.script = script or []
        self.state = "starting"
        self.error = ""
        self.live: dict = {}
        self.rtc: dict = {}
        self.render_uid: str | None = None
        self.client_secret = ""
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.seq = itertools.count(1)
        self.conn_id = f"trial-{uuid.uuid4().hex[:10]}"
        self.started_at = time.time()
        self.billing: dict = {}
        self.stage_joined = asyncio.Event()
        self.log: list[str] = []
        self._task: asyncio.Task | None = None

    def public(self) -> dict:
        return {"mode": self.mode, "state": self.state, "error": self.error, "live_id": self.live.get("id"),
                "rtc": self.rtc, "render_uid": self.render_uid, "driver_url": self.driver_url,
                "publish": self.mode == "editing", "billing": self.billing, "log": self.log[-20:]}

    def note(self, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {text}")
        self.room.log("info", f"[试验 {self.mode}] {text}")

    @property
    def vidu(self):
        return self.room.vidu

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await (self._run_component() if self.mode == "component" else self._run_editing())
        except Exception as error:  # noqa: BLE001
            self.state, self.error = "error", str(error)[:300]
            self.note(f"失败：{error}")
        finally:
            await self.stop("试验结束")

    async def _open_ws(self, url: str, headers: dict) -> None:
        self.ws = await self.vidu.http.ws_connect(url, headers=headers, heartbeat=None, autoping=True, max_msg_size=32 * 1024 * 1024)
        asyncio.create_task(self._reader())
        asyncio.create_task(self._ping())

    async def _reader(self) -> None:
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                frame = json.loads(msg.data)
                if frame.get("type") == 2 and (frame.get("payload") or {}).get("conn_init_ack", {}).get("success"):
                    self.state = "live"
                elif frame.get("type") == 2:
                    ack = frame["payload"]["conn_init_ack"]
                    if ack.get("error_code") == "NOT_READY":
                        await asyncio.sleep(3)
                        await self.send(1, {"conn_init": {"version": 1}})
                    else:
                        self.note(f"初始化失败 {ack}")
                elif frame.get("type") in (6, 14, 103):
                    self.note(f"服务端消息 type={frame.get('type')} {json.dumps(frame.get('payload'), ensure_ascii=False)[:200]}")

    async def _ping(self) -> None:
        while self.ws is not None and not self.ws.closed:
            await asyncio.sleep(8)
            try:
                await self.ws.ping()
            except Exception:  # noqa: BLE001
                return

    async def send(self, kind: int, payload: dict) -> None:
        frame = {"type": kind, "live_id": str(self.live["id"]), "conn_id": self.conn_id, "seq_id": next(self.seq), "payload": payload}
        await self.ws.send_str(json.dumps(frame, ensure_ascii=False))

    async def _wait_live(self, timeout: float = 90) -> None:
        deadline = time.time() + timeout
        while self.state != "live":
            if time.time() > deadline:
                raise RuntimeError("渲染侧没有就绪")
            await asyncio.sleep(0.5)

    # ---------- component ----------
    async def _run_component(self) -> None:
        avatar = await self.room._ensure_avatar(self.image, quiet=True)
        body = {"model": "vidu-s2", "managed_rtc": {"provider": "artc", "enable_recording": True}}
        body.update({"avatar_id": avatar["id"]} if "id" in avatar else {"image_uri": avatar["image_uri"]})
        created = await self.vidu.request("POST", "/live/s_avatar/component", body)
        self.live, self.rtc, self.client_secret = created["live"], created["managed_rtc"], created.get("client_secret", "")
        self.live["id"] = str(self.live["id"])
        self.note(f"组件版会话已创建 {self.live['id']}")
        await self._open_ws(f"{self.vidu.ws_base}/live/v1/external-lives/{self.live['id']}/stream?conn_id={self.conn_id}", self.vidu.headers)
        await self.send(1, {"conn_init": {"version": 1}})
        await self._wait_live()
        self.note("渲染侧就绪，等试验页入会")
        await asyncio.wait_for(self.stage_joined.wait(), timeout=40)
        await asyncio.sleep(4)
        for item in self.script:
            if self.state != "live":
                break
            if item.get("action"):
                await self.send(9, {"text_msg": {"msg_id": uuid.uuid4().hex[:10], "content": item["action"], "timestamp": int(time.time() * 1000)}})
            if item.get("text"):
                await self.send(10, {"text_msg": {"msg_id": uuid.uuid4().hex[:10], "content": item["text"], "timestamp": int(time.time() * 1000)}})
            pcm = to_pcm(Path(item["audio"]), item.get("start", 0), item.get("duration"))
            self.note(f"送音频 {len(pcm) / (PCM_RATE * 2):.1f}s：{item.get('text') or item.get('label', '')}")
            t0 = time.perf_counter()
            for i in range(0, len(pcm), FRAME_BYTES):
                await self.ws.send_bytes(pcm[i:i + FRAME_BYTES])
                delay = t0 + (i + FRAME_BYTES) / (PCM_RATE * 2) - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
            await asyncio.sleep(item.get("pause", 1.5))

    # ---------- editing ----------
    async def _run_editing(self) -> None:
        raw = self.image.read_bytes()
        body = {"image_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode(), "editing_type": "subject_replacement"}
        headers = {"Authorization": self.vidu.api_key}  # Editing wants the bare key, no "Token " prefix
        async with self.vidu.http.post(self.vidu.base + "/live/s_editing/realtime", json=body, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise RuntimeError(f"创建 Editing 失败 {resp.status}: {self.vidu.redact(text)[:300]}")
            created = json.loads(text)
        self.live, self.rtc = created["live"], created["rtc"]
        self.live["id"] = str(self.live["id"])
        self.render_uid = str(created.get("render_uid") or "")
        self.note(f"Editing 会话已创建 {self.live['id']}，结果流用户 {self.render_uid}")
        await self._open_ws(f"{self.vidu.ws_base}/live/ws/live/connect?live_id={self.live['id']}&conn_id={self.conn_id}", headers)
        await self.send(1, {"conn_init": {"version": 1}})
        await self._wait_live()
        self.note("渲染侧就绪，等试验页推驱动视频")
        await asyncio.wait_for(self.stage_joined.wait(), timeout=40)
        await asyncio.sleep(self.script[0].get("duration", 60) if self.script else 60)

    async def stop(self, reason: str) -> None:
        if self.state in ("stopped",):
            return
        prev = self.state
        self.state = "stopped"
        if self.ws is not None and not self.ws.closed:
            try:
                await self.send(5, {"hangup": {"hangup_reason": "client_hangup"}})
            except Exception:  # noqa: BLE001
                pass
            await self.ws.close()
        if self.live.get("id"):
            await asyncio.sleep(3)
            try:
                live = (await self.vidu.get_live(self.live["id"])).get("live") or {}
                self.billing = {"billed_seconds": live.get("billed_seconds"), "credits_cost": live.get("credits_cost"),
                                "close_reason": live.get("close_reason")}
            except Exception as error:  # noqa: BLE001
                self.billing = {"error": str(error)[:200]}
        self.note(f"已停止（{reason}，之前状态 {prev}）计费 {self.billing}")
        if self.room.broadcaster and self.room.broadcaster.state == "live":
            await self.room.broadcaster.stop("试验结束")
