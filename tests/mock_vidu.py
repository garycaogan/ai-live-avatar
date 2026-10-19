# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
"""A stand-in for the Vidu realtime API, following the published protocol.

It checks auth, the create-live body and every control frame, answers conn_init
with NOT_READY once and then success, echoes avatar transcripts for text (or says
the next line queued in app[REPLIES]), and acks prompt_operation. No RTC media.
Run standalone for manual UI work:

    uv run tests/mock_vidu.py --port 28991
"""
from __future__ import annotations

import argparse
import json
import time

from aiohttp import WSMsgType, web

API_KEY = "vda_test_key"
FRAMES, LIVES, ERRORS = web.AppKey("frames", list), web.AppKey("lives", dict), web.AppKey("errors", list)
SOCKETS = web.AppKey("sockets", set)
REPLIES = web.AppKey("replies", list)  # lines she says next, in order, instead of the echo


def build_mock() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app[FRAMES], app[LIVES], app[ERRORS], app[SOCKETS], app[REPLIES] = [], {}, [], set(), []

    def authed(request) -> bool:
        return request.headers.get("Authorization") == f"Token {API_KEY}"

    async def create_avatar(request):
        if not authed(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        body = await request.json()
        if not str(body.get("image_uri", "")).startswith("data:image/"):
            return web.json_response({"message": "bad image_uri"}, status=400)
        return web.json_response({"id": "avatar-001", "status": "created"})

    async def get_avatar(request):
        return web.json_response({"id": request.match_info["id"], "status": "success"})

    async def create_live(request):
        if not authed(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        body = await request.json()
        avatar = body.get("avatar") or {}
        problems = []
        if body.get("model") not in ("vidu-s1", "vidu-s2"):
            problems.append("model")
        if not avatar.get("persona"):
            problems.append("avatar.persona")
        if bool(avatar.get("id")) == bool(avatar.get("image_uri")):
            problems.append("avatar.id xor avatar.image_uri")
        if problems:
            return web.json_response({"message": f"invalid: {problems}"}, status=400)
        live_id = str(9007199254740993 + len(app[LIVES]))  # > 2^53 on purpose: must stay a string
        app[LIVES][live_id] = {"body": body, "created": time.time(), "ready_at": None, "model": body["model"]}
        return web.json_response({
            "live": {"id": live_id, "status": "waiting", "live_duration": 7200, "call_mode": body.get("call_mode")},
            "recording": {"enabled": bool(body.get("enable_recording")), "video_url": ""},
            "rtc": {"app_id": "mock-app", "channel_id": f"live-{live_id}", "user_id": f"user-{live_id}",
                    "token": "bW9jay10b2tlbg==", "token_expire_at": str(int(time.time()) + 3600), "provider": "artc"},
        })

    async def get_live(request):
        live = app[LIVES].get(request.match_info["id"])
        if not live:
            return web.json_response({"message": "not found"}, status=404)
        billed = int(time.time() - live["ready_at"]) if live["ready_at"] else 0
        return web.json_response({"live": {"id": request.match_info["id"], "status": "on_live", "billed_seconds": billed,
                                           "credits_cost": int(billed * 1.5)}})

    async def control(request):
        if not authed(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        live_id = request.query.get("live_id")
        live = app[LIVES].get(live_id)
        if not live:
            return web.json_response({"message": "unknown live"}, status=404)
        ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, autoping=False)
        await ws.prepare(request)
        app[SOCKETS].add(ws)
        attempts = 0
        async for msg in ws:
            if msg.type == WSMsgType.PING:
                live["pings"] = live.get("pings", 0) + 1
                await ws.pong(msg.data)
                continue
            if msg.type != WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            app[FRAMES].append(frame)
            if frame.get("live_id") != live_id or not isinstance(frame.get("seq_id"), int):
                app[ERRORS].append(f"bad envelope {frame}")
            kind, payload = frame.get("type"), frame.get("payload") or {}
            if kind == 1:
                attempts += 1
                ok = attempts >= 2
                if ok:
                    live["ready_at"] = time.time()
                await ws.send_json({"type": 2, "live_id": live_id, "payload": {"conn_init_ack": {
                    "success": ok, "error_code": "" if ok else "NOT_READY", "error_msg": "" if ok else "live sip endpoint not ready"}}})
            elif kind in (99, 100, 101, 102):
                key = {99: "text_msg", 100: "text_tts", 101: "text_action", 102: "text_dialog"}[kind]
                content = (payload.get(key) or {}).get("content")
                if not content:
                    app[ERRORS].append(f"empty {key}")
                if kind in (99, 102):
                    line = app[REPLIES].pop(0) if app[REPLIES] else f"（数字人）好的：{content[:20]}"
                    await ws.send_json({"type": 10, "live_id": live_id, "payload": {"transcript": {"text": line}}})
            elif kind == 11:
                op = payload.get("prompt_operation") or {}
                images = op.get("images") or []
                error = ""
                if live["model"] != "vidu-s2":
                    error = "PROMPT_OP_MODEL_NOT_SUPPORTED"
                elif op.get("op_type", "switch") == "switch" and not (1 <= len(images) <= 3):
                    error = "PROMPT_OP_PARAM_INVALID"
                elif any(not i.get("image_id") for i in images) or len({i.get("image_id") for i in images}) != len(images):
                    error = "PROMPT_OP_PARAM_INVALID"
                elif op.get("op_type", "switch") == "switch" and any(not str(i.get("image_uri", "")).startswith(("data:image/", "http", "ssupload:")) for i in images):
                    error = "PROMPT_OP_PARAM_INVALID"
                await ws.send_json({"type": 12, "live_id": live_id, "payload": {"prompt_operation_ack": {
                    "success": not error, "error_code": error, "error_msg": ""}}})
            elif kind == 97:
                update = payload.get("session_update") or {}
                voice = (update.get("avatar") or {}).get("voice")
                ok = voice is None or bool(voice)
                await ws.send_json({"type": 98, "live_id": live_id, "payload": {"session_update_ack": {
                    "success": ok, "error_code": "" if ok else "PARAM_INVALID"}}})
            elif kind == 5:
                await ws.close()
                break
        return ws

    async def drop(_):
        """Kill every control socket without a close handshake, like a network blip."""
        for ws in list(app[SOCKETS]):
            if ws._writer is not None and ws._writer.transport is not None:
                ws._writer.transport.abort()
        app[SOCKETS].clear()
        return web.json_response({"dropped": True})

    async def hangup(request):
        """Send a force-hangup frame on every control socket, like Vidu ending the show."""
        body = await request.json() if request.can_read_body else {}
        for ws in list(app[SOCKETS]):
            if not ws.closed:
                await ws.send_json({"type": 6, "payload": {"hangup": {"hangup_reason": body.get("reason", "ai_output_closed")}}})
        return web.json_response({"hangup": True})

    async def frames(_):
        return web.json_response({"frames": app[FRAMES], "errors": app[ERRORS], "lives": list(app[LIVES])})

    app.add_routes([
        web.post("/live/v1/avatars", create_avatar), web.get("/live/v1/avatars/{id}", get_avatar),
        web.post("/live/s_avatar/realtime", create_live), web.get("/live/v1/lives/{id}", get_live),
        web.get("/live/ws/live/connect", control), web.get("/_frames", frames), web.post("/_drop", drop),
        web.post("/_hangup", hangup),
    ])
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=28991)
    args = parser.parse_args()
    print(f"mock Vidu on http://127.0.0.1:{args.port}  (API key: {API_KEY})")
    web.run_app(build_mock(), host="127.0.0.1", port=args.port, print=None)
