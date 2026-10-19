"""Vidu S2-Avatar realtime API (https://platform.vidu.cn/vidu-stream/doc).

REST creates the live session; one WebSocket carries control signals (text,
actions, reference-image switches, hangup); audio/video flows over RTC, which the
stage page joins in the browser. The API key never leaves this process.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
import uuid
from typing import Awaitable, Callable

import aiohttp

ENV_BASES = {"cn": "https://api.vidu.cn", "ovs": "https://api.vidu.com"}

# Control signal types from the realtime parameter reference.
CONN_INIT, CONN_INIT_ACK, CALL_HANGUP, FORCE_HANGUP, AUDIO_INTERRUPTED = 1, 2, 5, 6, 7
USER_TRANSCRIPT, AVATAR_TRANSCRIPT, PROMPT_OPERATION, PROMPT_OPERATION_ACK = 9, 10, 11, 12
SESSION_UPDATE, SESSION_UPDATE_ACK = 97, 98
TEXT_MSG, TEXT_TTS, TEXT_ACTION, TEXT_DIALOG, ERROR = 99, 100, 101, 102, 103


class ViduError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Vidu HTTP {status}: {message}")
        self.status = status


class ViduClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str, env: str = "cn", base_url: str | None = None):
        self.http = session
        self.api_key = api_key
        self.base = (base_url or ENV_BASES[env]).rstrip("/")
        self.ws_base = self.base.replace("https://", "wss://").replace("http://", "ws://")

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Token {self.api_key}"}

    def redact(self, text: str) -> str:
        return text.replace(self.api_key, "[REDACTED]") if self.api_key else text

    async def request(self, method: str, path: str, body: dict | None = None) -> dict:
        async with self.http.request(method, self.base + path, json=body, headers=self.headers,
                                     timeout=aiohttp.ClientTimeout(total=60)) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise ViduError(resp.status, self.redact(text)[:500])
            return json.loads(text) if text.strip() else {}

    async def upload_avatar(self, image_uri: str, name: str) -> dict:
        return await self.request("POST", "/live/v1/avatars", {"image_uri": image_uri, "name": name})

    async def get_avatar(self, avatar_id: str) -> dict:
        return await self.request("GET", f"/live/v1/avatars/{avatar_id}")

    async def create_live(self, body: dict) -> dict:
        return await self.request("POST", "/live/s_avatar/realtime", body)

    async def get_live(self, live_id: str) -> dict:
        return await self.request("GET", f"/live/v1/lives/{live_id}")

    async def get_recording(self, live_id: str) -> dict:
        return await self.request("GET", f"/live/v1/lives/{live_id}/recording")


class ControlChannel:
    """The App WebSocket for one live session."""

    def __init__(self, client: ViduClient, live_id: str, on_message: Callable[[dict], Awaitable[None]],
                 on_closed: Callable[[str], Awaitable[None]], frame_log: Callable[[str, dict], None] | None = None,
                 conn_id: str | None = None):
        self.client = client
        self.live_id = str(live_id)
        self.conn_id = conn_id or f"live-avatar-{uuid.uuid4().hex[:12]}"
        self.on_message = on_message
        self.on_closed = on_closed
        self.frame_log = frame_log or (lambda direction, frame: None)
        self.seq = itertools.count(1)
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self.pings = 0
        self.ready = asyncio.Event()

    async def connect(self) -> None:
        url = f"{self.client.ws_base}/live/ws/live/connect?live_id={self.live_id}&conn_id={self.conn_id}"
        # Keepalive without a timeout. Vidu drops a client that sends nothing for 15 s, so we ping every
        # 8 s. We do not use aiohttp's heartbeat: it closes the socket when a PONG is 10 s late, and Vidu
        # can stay busy longer than that while applying a large reference-image switch, which then
        # ends the whole live (close_reason=client_reconnect_timeout).
        self.ws = await self.client.http.ws_connect(url, headers=self.client.headers, heartbeat=None, autoping=True,
                                                    max_msg_size=32 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read())
        self._keepalive = asyncio.create_task(self._ping_loop())

    async def _ping_loop(self, every: float = 8.0) -> None:
        try:
            while self.ws is not None and not self.ws.closed:
                await asyncio.sleep(every)
                if self.ws.closed:
                    break
                await self.ws.ping()
                self.pings += 1
        except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
            pass

    async def _read(self) -> None:
        reason = "closed"
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        frame = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    self.frame_log("in", frame)
                    if frame.get("type") == CONN_INIT_ACK:
                        ack = (frame.get("payload") or {}).get("conn_init_ack", {})
                        if ack.get("success"):
                            self.ready.set()
                        elif ack.get("error_code") == "NOT_READY":
                            asyncio.get_running_loop().call_later(3, lambda: asyncio.ensure_future(self._retry_init()))
                    await self.on_message(frame)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    reason = f"ws {msg.type.name.lower()}"
                    break
        except Exception as error:  # noqa: BLE001 - surface any reader failure as a closed channel
            reason = f"ws reader error: {error}"
        finally:
            if self.ws is not None and self.ws.close_code is not None:
                reason = f"{reason} (code {self.ws.close_code})"
            await self.on_closed(reason)

    async def send(self, signal_type: int, payload: dict) -> dict:
        if self.ws is None or self.ws.closed:
            raise RuntimeError("控制通道未连接")
        frame = {"type": signal_type, "live_id": self.live_id, "conn_id": self.conn_id,
                 "seq_id": next(self.seq), "payload": payload}
        self.frame_log("out", frame)
        await self.ws.send_str(json.dumps(frame, ensure_ascii=False))
        return frame

    async def _retry_init(self) -> None:
        if not self.ready.is_set() and self.ws is not None and not self.ws.closed:
            await self.conn_init()

    async def conn_init(self) -> None:
        await self.send(CONN_INIT, {"conn_init": {"version": 1}})

    async def text(self, signal_type: int, content: str) -> dict:
        key = {TEXT_MSG: "text_msg", TEXT_TTS: "text_tts", TEXT_ACTION: "text_action", TEXT_DIALOG: "text_dialog"}[signal_type]
        body = {"msg_id": f"m-{uuid.uuid4().hex[:10]}", "content": content}
        if signal_type == TEXT_MSG:
            body["timestamp"] = int(time.time() * 1000)
        return await self.send(signal_type, {key: body})

    async def switch_images(self, images: list[dict]) -> dict:
        return await self.send(PROMPT_OPERATION, {"prompt_operation": {"op_type": "switch", "images": images}})

    async def remove_images(self, image_ids: list[str] | None = None) -> dict:
        op: dict = {"op_type": "remove"}
        if image_ids:
            op["images"] = [{"image_id": i} for i in image_ids[:3]]
        return await self.send(PROMPT_OPERATION, {"prompt_operation": op})

    async def update_session(self, fields: dict) -> dict:
        """session_update (type 97): change voice, vad or llm settings mid-session; qwen_omni voices only."""
        return await self.send(SESSION_UPDATE, {"session_update": fields})

    async def interrupt(self) -> None:
        await self.send(AUDIO_INTERRUPTED, {})

    async def hangup(self, reason: str = "user_end") -> None:
        try:
            await self.send(CALL_HANGUP, {"hangup": {"hangup_reason": reason}})
        finally:
            await self.close()

    async def close(self) -> None:
        if self._keepalive:
            self._keepalive.cancel()
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
