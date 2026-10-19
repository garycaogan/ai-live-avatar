# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
"""Set up and inspect OBS for the AI live room through OBS's built-in WebSocket (v5).

    uv run tools/obs_control.py setup          # vertical 1080x1920 canvas, scene "AI 直播间" with the layers
    uv run tools/obs_control.py shot out.png    # screenshot of what the scene shows right now
    uv run tools/obs_control.py status          # scene items, audio level of the avatar layer, stream/record state

The password lives in ~/.deepsea/obs/ws.password and is never printed. This script never starts
streaming or recording; OBS's own buttons do that.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
import uuid
from pathlib import Path

import aiohttp

URL = "ws://127.0.0.1:4455"
SERVER = "http://127.0.0.1:28900"
SCENE = "AI 直播间"
CANVAS = (1080, 1920)
BOX_TOP, BOX_ASPECT = 268, 1.2  # same box as the built-in layout: a landscape picture cropped to 1.2:1 near the top


class OBS:
    def __init__(self, ws):
        self.ws = ws
        self.events: list[dict] = []

    @classmethod
    async def connect(cls, http: aiohttp.ClientSession, events: int = 0) -> "OBS":
        ws = await http.ws_connect(URL)
        hello = (await ws.receive_json())["d"]
        identify = {"rpcVersion": 1, "eventSubscriptions": events}
        auth = hello.get("authentication")
        if auth:
            password = (Path.home() / ".deepsea/obs/ws.password").read_text().strip()
            secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        await ws.send_json({"op": 1, "d": identify})
        msg = await ws.receive_json()
        if msg.get("op") != 2:
            raise RuntimeError(f"OBS 拒绝了连接：{msg}")
        return cls(ws)

    async def call(self, request_type: str, data: dict | None = None, ok_codes=(100,)) -> dict:
        rid = uuid.uuid4().hex
        await self.ws.send_json({"op": 6, "d": {"requestType": request_type, "requestId": rid, "requestData": data or {}}})
        while True:
            msg = await self.ws.receive_json()
            if msg.get("op") == 5:
                self.events.append(msg["d"])
                continue
            if msg.get("op") == 7 and msg["d"]["requestId"] == rid:
                status = msg["d"]["requestStatus"]
                if status["code"] not in ok_codes:
                    raise RuntimeError(f"{request_type} 失败：{status.get('code')} {status.get('comment', '')}")
                return msg["d"].get("responseData") or {}


async def layer_info(http: aiohttp.ClientSession) -> dict:
    async with http.get(SERVER + "/api/obs") as resp:
        return {layer["key"]: layer for layer in (await resp.json())["layers"]}


async def setup() -> None:
    async with aiohttp.ClientSession() as http:
        layers = await layer_info(http)
        obs = await OBS.connect(http)
        status = await obs.call("GetStreamStatus")
        if status.get("outputActive"):
            raise SystemExit("OBS 正在推流，先停掉再重新配置")
        w, h = CANVAS
        await obs.call("SetVideoSettings", {"baseWidth": w, "baseHeight": h, "outputWidth": w, "outputHeight": h,
                                            "fpsNumerator": 30, "fpsDenominator": 1})
        scenes = [s["sceneName"] for s in (await obs.call("GetSceneList"))["scenes"]]
        if SCENE in scenes:
            for item in (await obs.call("GetSceneItemList", {"sceneName": SCENE}))["sceneItems"]:
                await obs.call("RemoveInput", {"inputName": item["sourceName"]}, ok_codes=(100, 600))
        else:
            await obs.call("CreateScene", {"sceneName": SCENE})
        await obs.call("SetCurrentProgramScene", {"sceneName": SCENE})

        async def add(name: str, kind: str, settings: dict) -> int:
            existing = [i["inputName"] for i in (await obs.call("GetInputList"))["inputs"]]
            if name in existing:
                await obs.call("RemoveInput", {"inputName": name})
            return (await obs.call("CreateInput", {"sceneName": SCENE, "inputName": name, "inputKind": kind,
                                                   "inputSettings": settings, "sceneItemEnabled": True}))["sceneItemId"]

        # bottom to top: background, the digital human, the transparent text layer
        await add("背景", "color_source_v3", {"color": 0xFF1C140B, "width": w, "height": h})  # #0b141c, ABGR
        avatar = layers["avatar"]
        aid = await add("数字人画面", "browser_source", {
            "url": avatar["url"], "width": avatar["width"], "height": avatar["height"],
            "reroute_audio": True, "shutdown": False, "restart_when_active": False})
        aw, ah = avatar["width"], avatar["height"]
        if aw > ah:  # landscape picture: crop the sides to the box shape and fit it to the canvas width
            crop = round((aw - ah * BOX_ASPECT) / 2)
            scale = w / (aw - 2 * crop)
            transform = {"positionX": 0, "positionY": BOX_TOP, "scaleX": scale, "scaleY": scale, "cropLeft": crop, "cropRight": crop}
        else:  # portrait picture: fill the canvas
            scale = max(w / aw, h / ah)
            transform = {"positionX": 0, "positionY": 0, "scaleX": scale, "scaleY": scale}
        await obs.call("SetSceneItemTransform", {"sceneName": SCENE, "sceneItemId": aid, "sceneItemTransform": transform})
        overlay = layers["overlay"]
        await add("弹幕叠层", "browser_source", {"url": overlay["url"], "width": overlay["width"], "height": overlay["height"],
                                              "shutdown": False, "restart_when_active": False})
        items = (await obs.call("GetSceneItemList", {"sceneName": SCENE}))["sceneItems"]
        print("场景已配置：", " → ".join(i["sourceName"] for i in sorted(items, key=lambda i: i["sceneItemIndex"])))
        await obs.ws.close()


async def shot(path: str) -> None:
    async with aiohttp.ClientSession() as http:
        obs = await OBS.connect(http)
        data = await obs.call("GetSourceScreenshot", {"sourceName": SCENE, "imageFormat": "png", "imageWidth": 540})
        Path(path).write_bytes(base64.b64decode(data["imageData"].split(",", 1)[1]))
        print("截图已保存", path)
        await obs.ws.close()


async def status(seconds: float = 6) -> None:
    async with aiohttp.ClientSession() as http:
        obs = await OBS.connect(http, events=1 << 16)  # InputVolumeMeters
        items = (await obs.call("GetSceneItemList", {"sceneName": SCENE}))["sceneItems"]
        stream = await obs.call("GetStreamStatus")
        record = await obs.call("GetRecordStatus")
        peak = -200.0
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            try:
                msg = await asyncio.wait_for(obs.ws.receive_json(), timeout=1)
            except asyncio.TimeoutError:
                continue
            d = msg.get("d") or {}
            if d.get("eventType") == "InputVolumeMeters":
                for inp in d["eventData"]["inputs"]:
                    if inp["inputName"] == "数字人画面" and inp["inputLevelsMul"]:
                        import math
                        level = max(ch[1] for ch in inp["inputLevelsMul"])  # peak, linear
                        if level > 0:
                            peak = max(peak, 20 * math.log10(level))
        print(json.dumps({"scene_items": [i["sourceName"] for i in items], "streaming": stream.get("outputActive"),
                          "recording": record.get("outputActive"), "avatar_audio_peak_db": round(peak, 1)}, ensure_ascii=False))
        await obs.ws.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "setup":
        asyncio.run(setup())
    elif cmd == "shot":
        asyncio.run(shot(sys.argv[2] if len(sys.argv) > 2 else "obs-scene.png"))
    else:
        asyncio.run(status())
