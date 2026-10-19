# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
"""Set up and inspect OBS for the AI live room through OBS's built-in WebSocket (v5).

    uv run tools/obs_control.py setup          # vertical 1080x1920 canvas, scene "AI 直播间" with the layers
    uv run tools/obs_control.py setup douyin [--camera 名字]
                                                # landscape 1296x1080 canvas for Douyin, scene "抖音直播间":
                                                # the picture fills the frame, the host's camera sits bottom-left
    uv run tools/obs_control.py shot out.png    # screenshot of what the current scene shows right now
    uv run tools/obs_control.py status          # scene items, audio level of the avatar layer, stream/record state
    uv run tools/obs_control.py vcam start|stop # OBS virtual camera, for feeding the picture into 抖音直播伴侣

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
# Douyin: the reference room streams landscape, so Douyin shows the picture in a box above its own comments.
# The frame is that box (1.2:1); the host's camera is a square in the bottom-left corner, about a quarter of the height.
DOUYIN_SCENE, DOUYIN_CANVAS, CAMERA_SIZE = "抖音直播间", (1296, 1080), 254
CAMERA_KINDS = ("macos-avcapture", "av_capture_input_v2", "av_capture_input")  # newest first; whichever this OBS has
VIRTUAL_CAMERAS = ("obs virtual", "virtual camera", "虚拟")  # never pick a virtual camera: OBS would film its own output


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


async def prepare_scene(obs: OBS, scene: str, canvas: tuple[int, int]):
    """Set the canvas, empty (or create) the scene and make it current. Returns add(name, kind, settings) -> item id."""
    status = await obs.call("GetStreamStatus")
    if status.get("outputActive"):
        raise SystemExit("OBS 正在推流，先停掉再重新配置")
    if (await obs.call("GetVirtualCamStatus")).get("outputActive"):
        raise SystemExit("OBS 虚拟摄像头开着（直播伴侣可能正在用），先 vcam stop 再重新配置")
    w, h = canvas
    await obs.call("SetVideoSettings", {"baseWidth": w, "baseHeight": h, "outputWidth": w, "outputHeight": h,
                                        "fpsNumerator": 30, "fpsDenominator": 1})
    scenes = [s["sceneName"] for s in (await obs.call("GetSceneList"))["scenes"]]
    if scene in scenes:  # empty the scene but keep the inputs: OBS frees a removed browser source's name only much later
        for item in (await obs.call("GetSceneItemList", {"sceneName": scene}))["sceneItems"]:
            await obs.call("RemoveSceneItem", {"sceneName": scene, "sceneItemId": item["sceneItemId"]}, ok_codes=(100, 600))
    else:
        await obs.call("CreateScene", {"sceneName": scene})
    await obs.call("SetCurrentProgramScene", {"sceneName": scene})

    async def add(name: str, kind: str, settings: dict) -> int:
        # Inputs are shared by every scene. Reuse one that already has this name (the avatar layer stays a single
        # browser source, so two scenes never compete for the one RTC seat); recreate it only if its kind differs.
        inputs = {i["inputName"]: i["inputKind"] for i in (await obs.call("GetInputList"))["inputs"]}
        if inputs.get(name) == kind:
            await obs.call("SetInputSettings", {"inputName": name, "inputSettings": settings, "overlay": True})
            return (await obs.call("CreateSceneItem", {"sceneName": scene, "sourceName": name, "sceneItemEnabled": True}))["sceneItemId"]
        if name in inputs:
            await obs.call("RemoveInput", {"inputName": name}, ok_codes=(100, 600))
            for _ in range(40):
                if name not in [i["inputName"] for i in (await obs.call("GetInputList"))["inputs"]]:
                    break
                await asyncio.sleep(0.25)
            else:
                raise SystemExit(f"OBS 里已有一个叫“{name}”的{inputs[name]}来源，删不掉。在 OBS 里手动删掉或改名后再跑")
        return (await obs.call("CreateInput", {"sceneName": scene, "inputName": name, "inputKind": kind,
                                               "inputSettings": settings, "sceneItemEnabled": True}))["sceneItemId"]
    return add


async def setup() -> None:
    async with aiohttp.ClientSession() as http:
        layers = await layer_info(http)
        obs = await OBS.connect(http)
        w, h = CANVAS
        add = await prepare_scene(obs, SCENE, CANVAS)

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
        # OBS pushes this scene itself, so the voice only needs to reach OBS's mix (the Douyin scene also plays it out)
        await obs.call("SetInputAudioMonitorType", {"inputName": "数字人画面", "monitorType": "OBS_MONITORING_TYPE_NONE"})
        overlay = layers["overlay"]
        await add("弹幕叠层", "browser_source", {"url": overlay["url"], "width": overlay["width"], "height": overlay["height"],
                                              "shutdown": False, "restart_when_active": False})
        items = (await obs.call("GetSceneItemList", {"sceneName": SCENE}))["sceneItems"]
        print("场景已配置：", " → ".join(i["sourceName"] for i in sorted(items, key=lambda i: i["sceneItemIndex"])))
        await obs.ws.close()


async def add_camera(obs: OBS, add, wanted: str | None) -> str:
    """Add the host's camera input and point it at a real camera. Returns a line saying which one."""
    kinds = (await obs.call("GetInputKindList", {"unversioned": False}))["inputKinds"]
    kind = next((k for k in CAMERA_KINDS if k in kinds), None)
    if kind is None:
        return "没找到视频采集设备这类来源，跳过真人摄像头"
    await add("真人摄像头", kind, {})
    try:
        items = (await obs.call("GetInputPropertiesListPropertyItems", {"inputName": "真人摄像头", "propertyName": "device"}))["propertyItems"]
    except RuntimeError as error:
        return f"读不到摄像头列表（{kind}）：{error}"
    real = [i for i in items if i.get("itemEnabled", True) and i.get("itemValue")
            and not any(v in i["itemName"].lower() for v in VIRTUAL_CAMERAS)]
    names = "、".join(i["itemName"] for i in real) or "无"
    pick = next((i for i in real if wanted and wanted.lower() in i["itemName"].lower()), None) if wanted else (real[0] if real else None)
    if pick is None:
        return f"真人摄像头没选设备（可用：{names}）。用 --camera 指定名字里的一段再跑一次"
    settings = {"device": pick["itemValue"], "device_name": pick["itemName"]}
    if kind.startswith("av_capture_input"):
        settings |= {"use_preset": True, "preset": "AVCaptureSessionPreset1280x720"}
    await obs.call("SetInputSettings", {"inputName": "真人摄像头", "inputSettings": settings})
    return f"真人摄像头：{pick['itemName']}（可用：{names}）"


async def setup_douyin(camera: str | None) -> None:
    async with aiohttp.ClientSession() as http:
        layers = await layer_info(http)
        obs = await OBS.connect(http)
        w, h = DOUYIN_CANVAS
        add = await prepare_scene(obs, DOUYIN_SCENE, DOUYIN_CANVAS)
        scene = DOUYIN_SCENE

        async def place(item_id: int, transform: dict) -> None:
            await obs.call("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item_id, "sceneItemTransform": transform})

        # bottom to top: background, the digital human, the host's camera, the transparent text layer. The background
        # and text layer are separate inputs from the vertical scene's; the avatar layer is shared.
        await add("抖音背景", "color_source_v3", {"color": 0xFF1C140B, "width": w, "height": h})
        avatar = layers["avatar"]
        aw, ah = avatar["width"], avatar["height"]
        aid = await add("数字人画面", "browser_source", {
            "url": avatar["url"], "width": aw, "height": ah,
            "reroute_audio": True, "shutdown": False, "restart_when_active": False})
        if aw > ah:  # the 1280x720 layer letterboxes a 1.2:1 picture: crop the bars, then fill the frame height
            crop = max(0, round((aw - ah * BOX_ASPECT) / 2))
            scale = h / ah
            await place(aid, {"positionX": round((w - (aw - 2 * crop) * scale) / 2), "positionY": 0,
                              "scaleX": scale, "scaleY": scale, "cropLeft": crop, "cropRight": crop})
        else:  # portrait picture: fit the height, centred, background shows at the sides
            scale = h / ah
            await place(aid, {"positionX": round((w - aw * scale) / 2), "positionY": 0, "scaleX": scale, "scaleY": scale})
        # Nothing records OBS's own mix on this route (直播伴侣 does the pushing), so also play the voice out loud.
        await obs.call("SetInputAudioMonitorType", {"inputName": "数字人画面", "monitorType": "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"})
        cam_id = None
        camera_line = await add_camera(obs, add, camera)
        if camera_line.startswith("真人摄像头："):
            cam_id = next(i["sceneItemId"] for i in (await obs.call("GetSceneItemList", {"sceneName": scene}))["sceneItems"]
                          if i["sourceName"] == "真人摄像头")
            await asyncio.sleep(1.5)  # the camera reports its size once frames arrive (after macOS camera permission)
            t = (await obs.call("GetSceneItemTransform", {"sceneName": scene, "sceneItemId": cam_id}))["sceneItemTransform"]
            sw, sh = int(t.get("sourceWidth") or 0), int(t.get("sourceHeight") or 0)
            corner = {"positionX": 0, "positionY": h - CAMERA_SIZE, "alignment": 5}  # alignment 5: top-left
            if sw and sh:  # centre square crop, scaled to the corner size
                side = min(sw, sh)
                await place(cam_id, corner | {"boundsType": "OBS_BOUNDS_NONE", "scaleX": CAMERA_SIZE / side, "scaleY": CAMERA_SIZE / side,
                                              "cropLeft": (sw - side) // 2, "cropRight": (sw - side) // 2,
                                              "cropTop": (sh - side) // 2, "cropBottom": (sh - side) // 2})
            else:  # no frames yet: let OBS fill and clip the square itself; rerun setup later for an exact crop
                await place(cam_id, corner | {"boundsType": "OBS_BOUNDS_SCALE_OUTER", "boundsAlignment": 0, "cropToBounds": True,
                                              "boundsWidth": CAMERA_SIZE, "boundsHeight": CAMERA_SIZE})
                camera_line += "；还没收到画面（可能在等 macOS 摄像头授权），授权后再跑一次 setup douyin"
        else:  # keep the scene consistent: no half-configured camera item
            for i in (await obs.call("GetSceneItemList", {"sceneName": scene}))["sceneItems"]:
                if i["sourceName"] == "真人摄像头":
                    await obs.call("RemoveSceneItem", {"sceneName": scene, "sceneItemId": i["sceneItemId"]})
        overlay = layers.get("overlay_douyin") or {**layers["overlay"], "url": layers["overlay"]["url"] + "&layout=douyin"}
        await add("抖音叠层", "browser_source", {"url": overlay["url"], "width": w, "height": h,
                                              "shutdown": False, "restart_when_active": False})
        items = (await obs.call("GetSceneItemList", {"sceneName": scene}))["sceneItems"]
        print("场景已配置：", " → ".join(i["sourceName"] for i in sorted(items, key=lambda i: i["sceneItemIndex"])))
        print(camera_line)
        await obs.ws.close()


async def current_scene(obs: OBS) -> str:
    data = await obs.call("GetCurrentProgramScene")
    return data.get("sceneName") or data.get("currentProgramSceneName") or SCENE


async def vcam(action: str) -> None:
    async with aiohttp.ClientSession() as http:
        obs = await OBS.connect(http)
        if action in ("start", "stop"):
            await obs.call("StartVirtualCam" if action == "start" else "StopVirtualCam", ok_codes=(100, 500))
            await asyncio.sleep(1)
        print(json.dumps({"virtual_camera": (await obs.call("GetVirtualCamStatus")).get("outputActive")}))
        await obs.ws.close()


async def shot(path: str) -> None:
    async with aiohttp.ClientSession() as http:
        obs = await OBS.connect(http)
        scene = await current_scene(obs)
        width = 648 if scene == DOUYIN_SCENE else 540
        data = await obs.call("GetSourceScreenshot", {"sourceName": scene, "imageFormat": "png", "imageWidth": width})
        Path(path).write_bytes(base64.b64decode(data["imageData"].split(",", 1)[1]))
        print("截图已保存", path)
        await obs.ws.close()


async def status(seconds: float = 6) -> None:
    async with aiohttp.ClientSession() as http:
        obs = await OBS.connect(http, events=1 << 16)  # InputVolumeMeters
        scene = await current_scene(obs)
        items = (await obs.call("GetSceneItemList", {"sceneName": scene}))["sceneItems"]
        stream = await obs.call("GetStreamStatus")
        record = await obs.call("GetRecordStatus")
        virtual = await obs.call("GetVirtualCamStatus")
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
        print(json.dumps({"scene": scene, "scene_items": [i["sourceName"] for i in items], "streaming": stream.get("outputActive"),
                          "recording": record.get("outputActive"), "virtual_camera": virtual.get("outputActive"),
                          "avatar_audio_peak_db": round(peak, 1)}, ensure_ascii=False))
        await obs.ws.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "setup" and len(sys.argv) > 2 and sys.argv[2] == "douyin":
        cam = sys.argv[sys.argv.index("--camera") + 1] if "--camera" in sys.argv[:-1] else None
        asyncio.run(setup_douyin(cam))
    elif cmd == "setup":
        asyncio.run(setup())
    elif cmd == "vcam":
        asyncio.run(vcam(sys.argv[2] if len(sys.argv) > 2 else "status"))
    elif cmd == "shot":
        asyncio.run(shot(sys.argv[2] if len(sys.argv) > 2 else "obs-scene.png"))
    else:
        asyncio.run(status())
