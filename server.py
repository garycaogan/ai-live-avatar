# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9", "pillow>=10", "numpy>=1.26"]
# ///
"""AI live-avatar prototype server.

    uv run server.py                 # http://127.0.0.1:28900  (console + stage)
    uv run server.py --vidu-base http://127.0.0.1:28991   # point at tests/mock_vidu.py

Binds to loopback only. The Vidu and Evolink keys are read from files and never sent
to the browser; the stage page only receives the per-session RTC join token.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path

import aiohttp
from aiohttp import web

from live.assets import AssetLibrary, AssetResolver, EvolinkGenerator
from live.broadcast import Broadcaster
from live.moderation import Moderator
from live.queue import POLICIES
from live.session import LiveRoom
from live.trials import Trial
from live.workbench import Workbench
from live.workbench import add_routes as add_workbench_routes

ROOT = Path(__file__).resolve().parent
ROOM = web.AppKey("room", LiveRoom)
HTTP = web.AppKey("http", aiohttp.ClientSession)
WB = web.AppKey("workbench", Workbench)

DEFAULTS = {
    "vidu": {"env": "cn", "base_url": None, "api_key_file": "~/.deepsea/vidu/api.key", "model": "vidu-s2",
             "call_mode": "video", "rtc_provider": "artc", "max_tokens": 80, "enable_recording": True,
             "credit_price_rmb": 0.03125, "credits_per_second": 1.5},
    "avatar_profile": "xiaoshen",  # falls back to the first profile when absent
    "avatars": {
        "xiaoshen": {"name": "小深", "image": "assets/avatars/xiaoshen.jpg", "voice": "", "persona_file": "persona.md",
                     "greeting_instruction": "用一句话热情地跟直播间观众打招呼，告诉大家可以发弹幕指挥你换衣服、换背景、拿东西"},
    },
    "schedule": {"interval_seconds": 20, "policy": "random_recent", "max_age_seconds": 180, "max_queue": 200,
                 "prop_hold_seconds": 10,
                 "idle_prompt_after_seconds": 45},
    "safety": {"max_session_minutes": 30, "budget_credits": 3000, "stage_join_timeout_seconds": 90},
    "generator": {"enabled": True, "api_key_file": "~/.deepsea/evolink/api.key", "model": "gpt-image-2.5-sunburst",
                  "resolution": "1K", "quality": "medium", "timeout_seconds": 180},
    "broadcast": {"youtube_key_file": "~/.deepsea/youtube/stream.key", "rtmp_base": "rtmp://a.rtmp.youtube.com/live2",
                  "width": 720, "height": 1280, "fps": 25, "video_bitrate": "3000k", "video_bufsize": "6000k",
                  "layout": "full", "box_height": 0.47, "box_top": 0.14, "box_crop_top": 0.10},
    "server": {"host": "127.0.0.1", "port": 28900},
}


def deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        merged[key] = deep_merge(merged[key], value) if isinstance(value, dict) and isinstance(merged.get(key), dict) else value
    return merged


def read_key(env_name: str, path: str | None) -> str | None:
    if os.environ.get(env_name):
        return os.environ[env_name].strip()
    if path:
        file = Path(path).expanduser()
        if file.exists():
            return file.read_text().strip() or None
    return None


def merge_profiles(cfg: dict) -> dict:
    """Avatar profiles live next to their images in assets/avatars/profiles.json. The workbench edits
    that file, so it wins over the built-in defaults."""
    profiles_file = ROOT / "assets" / "avatars" / "profiles.json"
    if profiles_file.exists():
        cfg["avatars"] = {**cfg["avatars"], **json.loads(profiles_file.read_text(encoding="utf-8"))}
    return cfg


def load_config(args) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    config_path = Path(args.config) if args.config else ROOT / "config.json"
    if config_path.exists():
        cfg = deep_merge(cfg, json.loads(config_path.read_text(encoding="utf-8")))
    merge_profiles(cfg)
    if args.vidu_base:
        cfg["vidu"]["base_url"] = args.vidu_base
    if args.port:
        cfg["server"]["port"] = args.port
    if args.no_generator:
        cfg["generator"]["enabled"] = False
    if args.interval:
        cfg["schedule"]["interval_seconds"] = args.interval
    return cfg


def json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def build_app(cfg: dict, vidu_key: str | None, evolink_key: str | None,
              assets_dir: Path | None = None, work_dir: Path | None = None) -> web.Application:
    app = web.Application(client_max_size=300 * 1024 * 1024)  # workbench uploads (videos, voice samples)

    async def on_startup(app):
        http = aiohttp.ClientSession()
        gcfg = cfg["generator"]
        generator = None
        if gcfg["enabled"] and evolink_key:
            generator = EvolinkGenerator(evolink_key, gcfg["model"], gcfg["resolution"], gcfg["quality"], gcfg["timeout_seconds"])
        resolver = AssetResolver(AssetLibrary(assets_dir or ROOT / "assets"), generator)
        app[HTTP] = http
        app[ROOM] = LiveRoom(cfg, ROOT, http, vidu_key, resolver, Moderator.from_file(ROOT / "moderation.json"), work_dir)
        app[ROOM].broadcaster = Broadcaster(app[ROOM], work_dir or ROOT, cfg["broadcast"])
        app[WB] = Workbench(ROOT, assets_dir or ROOT / "assets", app[ROOM], http, evolink_key)
        app[WB].resume_jobs()

    async def on_cleanup(app):
        room: LiveRoom = app[ROOM]
        await room.broadcaster.stop("服务关闭")
        await room.stop("服务关闭")
        await app[HTTP].close()

    async def no_cache_static(request, response):
        # The pages change often while we iterate; a cached workbench.js shows yesterday's features.
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"

    app.on_response_prepare.append(no_cache_static)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_):
        raise web.HTTPFound("/workbench")

    @routes.get("/console")
    async def console(_):
        return web.FileResponse(ROOT / "static" / "console.html")

    @routes.get("/workbench")
    async def workbench(_):
        page = ROOT / "static" / "workbench.html"
        if not page.exists():
            return web.Response(text="工作台页面还没生成", content_type="text/plain", charset="utf-8")
        return web.FileResponse(page, headers={"Cache-Control": "no-cache"})

    @routes.get("/stage")
    async def stage(_):
        return web.FileResponse(ROOT / "static" / "stage.html")

    @routes.get("/avatar.jpg")
    async def avatar(request):
        room: LiveRoom = request.app[ROOM]
        key = request.query.get("profile") or room.profile
        profile = room.cfg["avatars"].get(key) or room.avatar_cfg
        return web.FileResponse(ROOT / profile["image"], headers={"Cache-Control": "no-cache"})

    @routes.get("/api/state")
    async def state(request):
        return web.json_response(request.app[ROOM].snapshot())

    @routes.get("/api/events")
    async def events(request):
        room: LiveRoom = request.app[ROOM]
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        queue: asyncio.Queue = asyncio.Queue()
        room.subscribers.add(queue)
        try:
            await resp.write(f"event: state\ndata: {json.dumps(room.snapshot(), ensure_ascii=False)}\n\n".encode())
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
                    continue
                await resp.write(f"event: {msg['event']}\ndata: {json.dumps(msg['data'], ensure_ascii=False)}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            room.subscribers.discard(queue)
        return resp

    @routes.post("/api/session/start")
    async def start(request):
        body = await request.json() if request.can_read_body else {}
        try:
            snap = await request.app[ROOM].start(body.get("model"), bool(body.get("dry_run")), body.get("profile"))
        except Exception as error:  # noqa: BLE001
            return json_error(400, str(error))
        return web.json_response(snap)

    @routes.post("/api/session/stop")
    async def stop(request):
        return web.json_response(await request.app[ROOM].stop("手动下播"))

    @routes.post("/api/danmaku")
    async def danmaku(request):
        body = await request.json()
        result = await request.app[ROOM].ingest(str(body.get("user", "")), str(body.get("text", "")),
                                                   float(body.get("gift") or 0), str(body.get("source", "api")))
        return web.json_response(result)

    @routes.post("/api/stage")
    async def stage_event(request):
        body = await request.json()
        await request.app[ROOM].stage_event(str(body.get("event", "")), str(body.get("detail", "")))
        return web.json_response({"ok": True})

    @routes.post("/api/stage/claim")
    async def stage_claim(request):
        body = await request.json()
        try:
            rtc = request.app[ROOM].claim_stage(str(body.get("client_id", "")), str(body.get("role", "")))
        except PermissionError as error:
            return json_error(409, str(error))
        return web.json_response({"rtc": rtc, "live_id": request.app[ROOM].live["id"]})

    @routes.post("/api/stage/heartbeat")
    async def stage_heartbeat(request):
        body = await request.json()
        return web.json_response({"owner": request.app[ROOM].stage_heartbeat(str(body.get("client_id", "")))})

    @routes.post("/api/stage/release")
    async def stage_release(request):
        body = json.loads(await request.text() or "{}")  # sendBeacon may not set a JSON content type
        request.app[ROOM].release_stage(str(body.get("client_id", "")), str(body.get("why", "页面关闭")))
        return web.json_response({"ok": True})

    @routes.post("/api/manual")
    async def manual(request):
        body = await request.json()
        try:
            await request.app[ROOM].manual(str(body.get("kind")), str(body.get("content", "")))
        except Exception as error:  # noqa: BLE001
            return json_error(400, str(error))
        return web.json_response({"ok": True})

    @routes.post("/api/session/voice")
    async def session_voice(request):
        body = await request.json()
        try:
            await request.app[ROOM].set_voice(str(body.get("voice", "")))
        except Exception as error:  # noqa: BLE001
            return json_error(400, str(error))
        return web.json_response({"voice": request.app[ROOM].voice_override})

    @routes.post("/api/next")
    async def next_now(request):
        request.app[ROOM].kick()
        return web.json_response({"ok": True})

    @routes.post("/api/settings")
    async def settings(request):
        body = await request.json()
        room: LiveRoom = request.app[ROOM]
        if "interval_seconds" in body:
            room.cfg["schedule"]["interval_seconds"] = max(5, min(120, int(body["interval_seconds"])))
        if body.get("policy") in POLICIES:
            room.queue.policy = body["policy"]
        if "idle_prompt_after_seconds" in body:
            room.cfg["schedule"]["idle_prompt_after_seconds"] = max(0, int(body["idle_prompt_after_seconds"]))
        room.broadcast("state")
        return web.json_response(room.snapshot())

    @routes.post("/api/broadcast")
    async def broadcast_target(request):
        body = await request.json()
        room: LiveRoom = request.app[ROOM]
        if body.get("layout") in ("full", "box"):
            room.broadcaster.cfg["layout"] = body["layout"]  # applies from the next stage push
            if "target" not in body:
                room.broadcast("state")
                return web.json_response(room.broadcaster.public())
        target = body.get("target")
        if target not in ("off", "file", "youtube"):
            return json_error(400, "target 只能是 off、file 或 youtube")
        if target == "off":
            await room.broadcaster.stop("手动停止推流")
        else:
            if target == "youtube" and not room.broadcaster.youtube_key():
                return json_error(400, "没有 YouTube 推流码")
            if room.output_mode != "builtin":
                try:
                    room.set_output_mode("builtin")
                except PermissionError as error:
                    return json_error(409, str(error))
            if room.broadcaster.target not in ("off", target):
                await room.broadcaster.stop(f"切换推流目标到 {target}")
            room.broadcaster.target = target
            room.log("info", f"推流目标设为 {target}，舞台页（?broadcast=1）会自动开始推")
        room.broadcast("state")
        return web.json_response(room.broadcaster.public())

    @routes.post("/api/output")
    async def output(request):
        body = await request.json()
        room: LiveRoom = request.app[ROOM]
        mode = body.get("mode")
        try:
            if mode == "builtin":
                target = body.get("target") or (room.broadcaster.target if room.broadcaster.target != "off" else "file")
                if target not in ("file", "youtube"):
                    return json_error(400, "内置推流的目标只能是 file 或 youtube")
                if target == "youtube" and not room.broadcaster.youtube_key():
                    return json_error(400, "没有 YouTube 推流码")
                if room.output_mode != "builtin":
                    room.set_output_mode("builtin")
                if room.broadcaster.target not in ("off", target):
                    await room.broadcaster.stop(f"切换推流目标到 {target}")
                room.broadcaster.target = target
            else:
                room.set_output_mode(mode)
                if room.broadcaster.target != "off" or room.broadcaster.state == "live":
                    await room.broadcaster.stop("切换输出方式")
            if body.get("layout") in ("full", "box"):
                room.broadcaster.cfg["layout"] = body["layout"]
        except PermissionError as error:
            return json_error(409, str(error))
        except ValueError as error:
            return json_error(400, str(error))
        room.broadcast("state")
        return web.json_response({"output_mode": room.output_mode, "stage_role": room.OUTPUT_ROLES[room.output_mode],
                                  "broadcast": room.broadcaster.public()})

    @routes.get("/api/obs")
    async def obs_info(request):
        room: LiveRoom = request.app[ROOM]
        origin = f"http://{request.host}"
        w, h = 1280, 720
        try:
            from PIL import Image
            with Image.open(ROOT / room.avatar_cfg["image"]) as img:
                if img.height > img.width:
                    w, h = 720, 1280
        except Exception:  # noqa: BLE001
            pass
        layers = [
            {"key": "avatar", "name": "数字人画面", "url": f"{origin}/stage?obs=1&role=obs&layer=avatar", "width": w, "height": h,
             "note": "只有数字人和她的声音，没有任何字。尺寸跟底图方向走，现在的角色是" + ("竖屏" if h > w else "横屏") + "。"},
            {"key": "overlay", "name": "弹幕叠层", "url": f"{origin}/stage?overlay=1", "width": 1080, "height": 1920,
             "note": "透明背景，只有排队弹幕、正在执行、字幕和倒计时，不占入会名额，可以随便摆。"},
            {"key": "all", "name": "画面加叠层", "url": f"{origin}/stage?obs=1&role=obs&layer=all", "width": 1080, "height": 1920,
             "note": "数字人和叠层合在一起的竖屏，不想自己摆图层时用它。和数字人画面二选一，不要同时加。"},
            {"key": "overlay_douyin", "name": "抖音横屏叠层", "url": f"{origin}/stage?overlay=1&layout=douyin", "width": 1296, "height": 1080,
             "note": "抖音横屏开播用：画面就是 1.2:1 的框，说明卡在上面两角，字幕在下方，左下角留给真人摄像头。"},
        ]
        tips = [
            "OBS 要装在这台 Mac 上：服务只在本机监听，别的电脑打不开这些地址。",
            "先在推流页把输出方式切到“用 OBS”，再启动数字人；同一场只能有一个画面入会，这时工作台的预览只显示提示。",
            "数字人图层勾选“通过 OBS 控制音频”，不然声音进不了推流。取消“当不可见时关闭源”和“场景变为活动状态时刷新浏览器”，不然切场景会断开。",
            "真人出镜：在 OBS 里加视频采集设备，放在数字人图层旁边或者下面，叠层放最上面。",
            "推流、录像、码率都用 OBS 自己的设置，工作台不管。",
        ]
        return web.json_response({"layers": layers, "tips": tips, "same_machine_only": True, "output_mode": room.output_mode})

    @routes.get("/api/broadcast/ingest")
    async def broadcast_ingest(request):
        room: LiveRoom = request.app[ROOM]
        ws = web.WebSocketResponse(max_msg_size=32 * 1024 * 1024)
        await ws.prepare(request)
        try:
            first = await ws.receive(timeout=10)
            hello = json.loads(first.data) if first.type == aiohttp.WSMsgType.TEXT else {}
        except (asyncio.TimeoutError, ValueError):
            await ws.close(message=b"expected hello")
            return ws
        try:
            await room.broadcaster.ingest(ws, hello)
        except Exception as error:  # noqa: BLE001
            room.broadcaster.state, room.broadcaster.error = "error", str(error)[:300]
            room.log("error", "推流启动失败", error=str(error)[:300])
            room.broadcast("state")
            if not ws.closed:
                await ws.close()
        return ws

    @routes.get("/api/logs")
    async def logs(request):
        return web.json_response(list(request.app[ROOM].logs)[-int(request.query.get("n", 200)):])

    @routes.post("/api/safety")
    async def safety(request):
        body = await request.json()
        room: LiveRoom = request.app[ROOM]
        for key in ("max_session_minutes", "budget_credits"):
            if key in body:
                room.cfg["safety"][key] = max(1, int(body[key]))
        room.log("info", "费用上限已调整", **room.cfg["safety"])
        room.broadcast("state")
        return web.json_response(room.cfg["safety"])

    @routes.post("/api/wardrobe/prewarm")
    async def wardrobe_prewarm(request):
        body = await request.json() if request.can_read_body else {}
        room: LiveRoom = request.app[ROOM]
        if body.get("profile") in room.cfg["avatars"] and room.status not in ("creating", "connecting", "waiting_stage", "live"):
            room.profile = body["profile"]
        return web.json_response(await room._prewarm_wardrobe(require_live=False))

    @routes.post("/api/trial/start")
    async def trial_start(request):
        body = await request.json()
        room: LiveRoom = request.app[ROOM]
        if not room.vidu:
            return json_error(400, "缺少 Vidu API Key")
        old = getattr(room, "trial", None)
        if old and old.state not in ("stopped", "error"):
            return json_error(409, "已有试验在跑")
        trial = Trial(room, body["mode"], ROOT / body["image"], body.get("driver_url"), body.get("script"))
        room.trial = trial
        if body.get("record", True):
            room.broadcaster.target = "file"
        await trial.start()
        return web.json_response(trial.public())

    @routes.get("/api/trial/state")
    async def trial_state(request):
        trial = getattr(request.app[ROOM], "trial", None)
        return web.json_response(trial.public() if trial else {"state": "none"})

    @routes.post("/api/trial/joined")
    async def trial_joined(request):
        trial = getattr(request.app[ROOM], "trial", None)
        if trial:
            trial.stage_joined.set()
            trial.note("试验页已入会")
        return web.json_response({"ok": True})

    @routes.post("/api/trial/stop")
    async def trial_stop(request):
        trial = getattr(request.app[ROOM], "trial", None)
        if trial:
            await trial.stop("手动停止")
        return web.json_response(trial.public() if trial else {"state": "none"})

    @routes.get("/trial")
    async def trial_page(_):
        return web.FileResponse(ROOT / "static" / "trial.html")

    @routes.get("/media/{name}")
    async def media(request):
        path = (ROOT / "assets" / "trial" / request.match_info["name"]).resolve()
        if not str(path).startswith(str((ROOT / "assets" / "trial").resolve())) or not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    @routes.get("/api/recording")
    async def recording(request):
        room: LiveRoom = request.app[ROOM]
        if not (room.vidu and room.live):
            return json_error(400, "没有可查询的会话")
        try:
            return web.json_response(await room.vidu.get_recording(room.live["id"]))
        except Exception as error:  # noqa: BLE001
            return json_error(502, str(error))

    app.add_routes(routes)
    add_workbench_routes(app, WB)
    app.router.add_static("/static", ROOT / "static")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="config.json path (default: ./config.json if present)")
    parser.add_argument("--port", type=int)
    parser.add_argument("--vidu-base", help="override Vidu base URL, e.g. a local mock")
    parser.add_argument("--no-generator", action="store_true", help="disable image generation for new props")
    parser.add_argument("--interval", type=int, help="seconds between executed danmaku commands")
    args = parser.parse_args()
    cfg = load_config(args)
    vidu_key = read_key("VIDU_API_KEY", cfg["vidu"]["api_key_file"])
    evolink_key = read_key("EVOLINK_API_KEY", cfg["generator"]["api_key_file"]) if cfg["generator"]["enabled"] else None
    host, port = cfg["server"]["host"], cfg["server"]["port"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("出于安全考虑只允许监听本机地址")
    print(f"工作台  http://{host}:{port}/workbench")
    print(f"控制台  http://{host}:{port}/console")
    print(f"舞台页  http://{host}:{port}/stage   （OBS 浏览器源用 /stage?obs=1）")
    print(f"Vidu Key：{'已读取' if vidu_key else '未找到（只能空跑）'}；生图：{'开启' if evolink_key else '关闭'}；"
          f"Vidu 地址：{cfg['vidu'].get('base_url') or cfg['vidu']['env']}")
    web.run_app(build_app(cfg, vidu_key, evolink_key), host=host, port=port, print=None)


if __name__ == "__main__":
    main()
