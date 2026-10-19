"""End to end against tests/mock_vidu.py: console API -> queue -> Vidu control frames.

    uv run --no-project --with aiohttp python -m unittest tests.test_e2e -v
"""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures import ensure_default_avatars  # noqa: E402

ensure_default_avatars()
import server  # noqa: E402
from mock_vidu import API_KEY, ERRORS, FRAMES, LIVES, REPLIES, build_mock  # noqa: E402


async def serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


class EndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="live-avatar-test-"))
        assets = self.tmp / "assets"
        (assets / "library").mkdir(parents=True)
        shutil.copy(ROOT / "assets/avatars/xiaoshen.jpg", assets / "library/cap.jpg")
        shutil.copy(ROOT / "assets/avatars/xiaoshen.jpg", assets / "library/beach.jpg")
        shutil.copy(ROOT / "assets/avatars/xiaoshen.jpg", assets / "library/water.jpg")
        (assets / "library.json").write_text(json.dumps({"items": [
            {"kind": "garment", "name": "黑色棒球帽", "aliases": ["棒球帽"], "file": "library/cap.jpg"},
            {"kind": "background", "name": "海边日落", "aliases": ["海边"], "file": "library/beach.jpg"},
            {"kind": "object", "name": "水", "aliases": ["一杯水"], "file": "library/water.jpg"},
        ]}, ensure_ascii=False))
        self.mock_app = build_mock()
        self.mock_runner, mock_url = await serve(self.mock_app)
        self.mock_url = mock_url
        cfg = copy.deepcopy(server.DEFAULTS)
        cfg["vidu"]["base_url"] = mock_url
        cfg["schedule"]["interval_seconds"] = 1
        cfg["schedule"]["policy"] = "fifo"  # deterministic order for assertions
        cfg["schedule"]["idle_prompt_after_seconds"] = 0
        cfg["schedule"]["speech_seconds_per_char"] = 0  # the mock echoes every line back; the speech tests set a real rate
        cfg["generator"]["enabled"] = False
        cfg["safety"]["live_visual_edits"] = True  # most tests exercise the redraw path; one checks it stays off by default
        app = server.build_app(cfg, API_KEY, None, assets_dir=assets, work_dir=self.tmp)
        self.runner, self.url = await serve(app)
        self.room = app[server.ROOM]
        self.http = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.http.close()
        await self.runner.cleanup()
        await self.mock_runner.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def post(self, path, body=None):
        async with self.http.post(self.url + path, json=body or {}) as resp:
            return resp.status, await resp.json()

    async def wait_for(self, predicate, timeout=12.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.1)
        return False

    def frames(self, kind=None):
        return [f for f in self.mock_app[FRAMES] if kind is None or f["type"] == kind]

    def sent_lines(self):
        """(send time, content) of every text_msg, from the timestamp the room puts on it."""
        return [(f["payload"]["text_msg"]["timestamp"] / 1000, f["payload"]["text_msg"]["content"]) for f in self.frames(99)]

    async def test_full_show(self):
        status, snap = await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertEqual(status, 200, snap)
        live = self.mock_app[LIVES][snap["live_id"]]
        self.assertEqual(live["body"]["avatar"]["id"], "avatar-001")  # uploaded once, then referenced by id
        self.assertTrue(live["body"]["audio"]["enable_transcription"])
        self.assertTrue(live["body"]["avatar"]["persona"].startswith("你叫小深"))  # {name} filled from the profile
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"), self.room.status)
        self.assertEqual(len(self.frames(1)), 2)  # NOT_READY once, then ready
        self.assertIsInstance(snap["live_id"], str)

        self.room.output_mode = "obs"  # the output mode decides who gets the single seat
        status, preview = await self.post("/api/stage/claim", {"client_id": "preview-x", "role": "preview"})
        self.assertEqual(status, 409, preview)
        self.assertIn("OBS", preview["error"])
        self.room.output_mode = "local"
        status, claim = await self.post("/api/stage/claim", {"client_id": "page-a"})
        self.assertEqual(status, 200, claim)
        self.assertEqual(claim["rtc"]["provider"], "artc")
        self.assertNotIn("token", json.dumps(snap.get("rtc_ready")))  # credentials are not broadcast to every page
        status, other = await self.post("/api/stage/claim", {"client_id": "page-b"})
        self.assertEqual(status, 409, other)  # a second page must not join with the same identity
        self.assertTrue((await self.post("/api/stage/heartbeat", {"client_id": "page-a"}))[1]["owner"])
        await self.post("/api/stage", {"event": "joined"})
        self.assertEqual(self.room.status, "live")

        _, blocked = await self.post("/api/danmaku", {"user": "坏人", "text": "站起来看下身材"})
        self.assertFalse(blocked["accepted"])
        for user, text in [("阿明", "你是真人吗"), ("小红", "戴上一个棒球帽"), ("老王", "去海边吧"), ("小李", "跳个舞"), ("小周", "换回来")]:
            _, result = await self.post("/api/danmaku", {"user": user, "text": text})
            self.assertTrue(result["accepted"], result)

        self.assertTrue(await self.wait_for(lambda: len(self.room.history) >= 5, timeout=15), [c.public() for c in self.room.history])
        texts = json.dumps(self.frames(), ensure_ascii=False)
        self.assertNotIn("身材", texts)
        self.assertIn("【弹幕】阿明：你是真人吗", texts)
        self.assertEqual(self.frames(101)[0]["payload"]["text_action"]["content"], "开心地跳一小段舞")

        switches = [f["payload"]["prompt_operation"] for f in self.frames(11) if f["payload"]["prompt_operation"].get("op_type") == "switch"]
        self.assertTrue(switches)
        kinds_seen = {img["kind"] for op in switches for img in op["images"]}
        self.assertEqual(kinds_seen, {"garment", "background"})
        both = [op for op in switches if len(op["images"]) == 2]
        self.assertTrue(both, "a later switch should keep the earlier garment/background")
        self.assertTrue(all(img["image_uri"].startswith("data:image/jpeg;base64,") for op in switches for img in op["images"]))
        removes = [f for f in self.frames(11) if f["payload"]["prompt_operation"].get("op_type") == "remove"]
        self.assertEqual(len(removes), 1)

        self.assertTrue(await self.wait_for(lambda: all(c.acked_at for c in self.room.history if c.kind in ("garment", "background", "reset"))))
        self.assertTrue(all(c.status == "done" for c in self.room.history), [c.public() for c in self.room.history])
        self.assertTrue(self.room.subtitle.startswith("（数字人）"))
        self.assertEqual(self.mock_app[ERRORS], [])

        await self.post("/api/stage/release", {"client_id": "page-a", "why": "测试"})
        self.assertIsNone(self.room.stage_owner)
        self.assertTrue(any("舞台页断开" in e["message"] for e in self.room.logs))
        _, snap = await self.post("/api/session/stop")
        self.assertEqual(self.room.status, "ended")
        self.assertEqual(len(self.frames(5)), 1)
        log_files = list((self.tmp / "logs").glob("live-*.jsonl"))
        self.assertEqual(len(log_files), 1)
        self.assertNotIn("base64,", log_files[0].read_text())  # frame log never stores image payloads

    async def test_long_line_is_not_cut_off_by_the_next_danmaku(self):
        self.room.cfg["schedule"]["speech_seconds_per_char"] = 0.23
        line = "臣亮言：先帝创业未半而中道崩殂，今天下三分，益州疲弊"
        talk = len(line) * 0.23  # about 6 s, against a 1 s interval
        self.mock_app[REPLIES].append(line)
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        await self.post("/api/danmaku", {"user": "阿明", "text": "背一下出师表"})
        await self.post("/api/danmaku", {"user": "小红", "text": "讲个笑话"})
        self.assertTrue(await self.wait_for(lambda: len(self.frames(99)) >= 2, timeout=talk + 10))
        (first_at, first), (second_at, second) = self.sent_lines()[:2]
        self.assertIn("出师表", first)
        self.assertIn("讲个笑话", second)
        # Her line arrives after ours was sent, so this is a floor: nothing goes out while she is still talking.
        self.assertGreaterEqual(second_at - first_at, talk + 0.5 - 0.01)
        self.assertLess(second_at - first_at, talk + 3)  # and the show moves on once she is done
        await self.post("/api/danmaku", {"user": "老王", "text": "唱首歌"})
        await self.post("/api/next")  # the operator's "run next now" still cuts in
        self.assertTrue(await self.wait_for(lambda: len(self.frames(99)) >= 3, timeout=2))
        self.assertIn("唱首歌", self.sent_lines()[2][1])
        await self.post("/api/session/stop")

    async def test_warmup_waits_too_and_a_long_estimate_is_capped(self):
        sched = self.room.cfg["schedule"]
        sched["speech_seconds_per_char"], sched["speech_max_seconds"], sched["idle_prompt_after_seconds"] = 0.23, 3, 1
        self.mock_app[REPLIES].append("先帝创业未半而中道崩殂" * 11)  # 121 characters, 28 s by length alone
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        await self.post("/api/danmaku", {"user": "阿明", "text": "背一下出师表"})
        self.assertTrue(await self.wait_for(lambda: len(self.frames(99)) >= 2, timeout=15))
        (first_at, first), (second_at, second) = self.sent_lines()[:2]
        self.assertIn("出师表", first)
        self.assertIn("【暖场】", second)
        self.assertGreaterEqual(second_at - first_at, 3 + 0.5 - 0.01)  # the warm-up waited for the capped estimate
        self.assertLess(second_at - first_at, 8)  # and not for the 28 s the length suggests
        await self.post("/api/session/stop")

    async def test_prop_is_put_down_after_use(self):
        self.room.cfg["schedule"]["prop_hold_seconds"] = 0.3
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        await self.post("/api/stage/claim", {"client_id": "page-a"})
        await self.post("/api/stage", {"event": "joined"})
        _, result = await self.post("/api/danmaku", {"user": "阿杰", "text": "喝口水吧"})
        self.assertEqual(result["command"]["item"], "水")  # not 口水

        def removes():
            return [f for f in self.frames(11) if f["payload"]["prompt_operation"].get("op_type") == "remove"]
        self.assertTrue(await self.wait_for(lambda: removes(), timeout=15))
        switch = next(f for f in self.frames(11) if f["payload"]["prompt_operation"].get("op_type") == "switch")
        self.assertEqual(removes()[0]["payload"]["prompt_operation"]["images"][0]["image_id"],
                         switch["payload"]["prompt_operation"]["images"][0]["image_id"])
        actions = [f["payload"]["text_action"]["content"] for f in self.frames(101)]
        self.assertTrue(any("喝一口" in a for a in actions), actions)
        self.assertTrue(await self.wait_for(lambda: any("放到一边" in f["payload"]["text_action"]["content"] for f in self.frames(101))))
        self.assertNotIn("object", self.room.visual)
        self.assertEqual(self.room.history[0].status, "done")  # the drop's ack did not mark the drink failed
        await self.post("/api/session/stop")

    async def test_voice_switch_mid_show(self):
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        status, body = await self.post("/api/session/voice", {"voice": "sd_anan"})
        self.assertEqual(status, 200, body)
        frame = self.frames(97)[-1]
        self.assertEqual(frame["payload"]["session_update"], {"avatar": {"voice": "sd_anan"}})
        self.assertTrue(await self.wait_for(lambda: any("会话配置更新回执" in e["message"] for e in self.room.logs)))
        self.assertEqual(self.room.snapshot()["voice"], "sd_anan")
        self.assertEqual(self.room._create_body({"avatar_id": "a"})["avatar"]["voice"], "sd_anan")  # an outfit swap keeps it
        status, _ = await self.post("/api/session/voice", {"voice": " "})
        self.assertEqual(status, 400)
        await self.post("/api/session/stop")

    async def test_output_modes_decide_the_stage_seat(self):
        status, body = await self.post("/api/output", {"mode": "obs"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["output_mode"], body["stage_role"]), ("obs", "obs"))
        async with self.http.get(self.url + "/api/obs") as resp:
            info = await resp.json()
        self.assertEqual([l["key"] for l in info["layers"]], ["avatar", "overlay", "all", "overlay_douyin"])
        self.assertIn("role=obs", info["layers"][0]["url"])
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        status, _ = await self.post("/api/output", {"mode": "local"})
        self.assertEqual(status, 409)  # no switching while the digital human runs
        status, _ = await self.post("/api/stage/claim", {"client_id": "workbench", "role": "preview"})
        self.assertEqual(status, 409)
        status, claim = await self.post("/api/stage/claim", {"client_id": "obs-source", "role": "obs"})
        self.assertEqual(status, 200, claim)
        await self.post("/api/session/stop")
        status, body = await self.post("/api/output", {"mode": "builtin", "target": "file"})
        self.assertEqual((status, body["output_mode"], body["broadcast"]["target"]), (200, "builtin", "file"))
        status, body = await self.post("/api/output", {"mode": "local"})
        self.assertEqual((body["output_mode"], body["broadcast"]["target"]), ("local", "off"))
        status, _ = await self.post("/api/output", {"mode": "cloud"})
        self.assertEqual(status, 400)

    async def test_s1_turns_visual_requests_into_chat(self):
        await self.post("/api/session/start", {"model": "vidu-s1"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        _, result = await self.post("/api/danmaku", {"user": "小红", "text": "戴上一个棒球帽"})
        self.assertEqual(result["command"]["kind"], "chat")
        self.assertTrue(await self.wait_for(lambda: len(self.room.history) >= 1))
        self.assertEqual(self.frames(11), [])
        await self.post("/api/session/stop")

    async def test_profile_switch(self):
        self.room.cfg["avatars"]["pilot"] = {"name": "小翼", "image": "assets/avatars/pilot.jpg", "voice": "", "persona_file": "persona.md"}
        status, body = await self.post("/api/session/start", {"model": "vidu-s2", "profile": "nobody"})
        self.assertEqual(status, 400)
        self.assertIn("没有这个形象", body["error"])
        status, snap = await self.post("/api/session/start", {"model": "vidu-s2", "profile": "pilot"})
        self.assertEqual(status, 200, snap)
        self.assertEqual(snap["avatar_name"], "小翼")
        self.assertTrue(self.mock_app[LIVES][snap["live_id"]]["body"]["avatar"]["persona"].startswith("你叫小翼"))
        await self.post("/api/session/stop")

    async def test_control_channel_reconnects_after_drop(self):
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        first = self.room.channel
        async with self.http.post(self.mock_url + "/_drop") as resp:
            self.assertEqual(resp.status, 200)
        self.assertTrue(await self.wait_for(lambda: self.room.channel is not first and self.room.channel.ready.is_set(), timeout=20))
        self.assertEqual(self.room.status, "waiting_stage")
        self.assertEqual(self.room.channel.conn_id, first.conn_id)
        _, result = await self.post("/api/danmaku", {"user": "阿明", "text": "你好"})
        self.assertTrue(await self.wait_for(lambda: any(c.text == "你好" for c in self.room.history)))
        self.assertTrue(any("已重连" in e["message"] for e in self.room.logs))
        await self.post("/api/session/stop")

    async def test_background_and_prop_redraws_are_off_by_default(self):
        self.room.cfg["safety"]["live_visual_edits"] = False
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        _, result = await self.post("/api/danmaku", {"user": "老王", "text": "去海边吧"})
        self.assertEqual(result["command"]["kind"], "chat")
        self.assertTrue(await self.wait_for(lambda: any(c.text == "去海边吧" for c in self.room.history)))
        self.assertFalse(self.frames(11), "no reference-image switch may reach Vidu")
        await self.post("/api/session/stop")

    async def test_vidu_hangup_restarts_the_show_when_enabled(self):
        await self.post("/api/settings", {"auto_recover": True})
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        first = self.room.live["id"]
        async with self.http.post(self.mock_url + "/_hangup", json={"reason": "ai_output_closed"}) as resp:
            self.assertEqual(resp.status, 200)
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage" and self.room.live["id"] != first, timeout=20))
        self.assertTrue(any("自动重新开播" in e["message"] for e in self.room.logs))
        await self.post("/api/session/stop")

    async def test_vidu_hangup_stays_down_without_auto_recover_or_on_credit(self):
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        async with self.http.post(self.mock_url + "/_hangup", json={"reason": "ai_output_closed"}):
            pass
        self.assertTrue(await self.wait_for(lambda: self.room.status == "ended"))
        await asyncio.sleep(4)
        self.assertEqual(self.room.status, "ended")  # auto_recover is off by default
        await self.post("/api/settings", {"auto_recover": True})
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        async with self.http.post(self.mock_url + "/_hangup", json={"reason": "credit_insufficient"}):
            pass
        self.assertTrue(await self.wait_for(lambda: self.room.status == "ended"))
        await asyncio.sleep(4)
        self.assertEqual(self.room.status, "ended")  # out of credits would only fail again

    async def test_outfit_change_hands_over_to_a_new_session(self):
        wardrobe = self.tmp / "outfits.json"
        shutil.copy(ROOT / "assets/avatars/xiaoshen.jpg", self.tmp / "jacket.jpg")
        wardrobe.write_text(json.dumps({"items": [
            {"name": "白色T恤", "aliases": ["白T"], "file": str(ROOT / "assets/avatars/xiaoshen.jpg"), "base": True},
            {"name": "银色飞行夹克", "aliases": ["银色夹克", "夹克"], "file": str(self.tmp / "jacket.jpg")},
        ]}, ensure_ascii=False))
        self.room.cfg["avatars"]["xiaoshen"]["outfits"] = str(wardrobe)
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        first_live = self.room.live["id"]
        await self.post("/api/stage/claim", {"client_id": "page-a"})
        await self.post("/api/stage", {"event": "joined"})
        _, result = await self.post("/api/danmaku", {"user": "小红", "text": "穿上银色夹克"})
        self.assertEqual((result["command"]["kind"], result["command"]["item"]), ("outfit", "银色飞行夹克"))
        self.assertTrue(await self.wait_for(lambda: self.room.live["id"] != first_live, timeout=15))
        self.assertTrue(self.room.swapping)
        status, claim = await self.post("/api/stage/claim", {"client_id": "page-a"})  # same page rejoins the new channel
        self.assertEqual(status, 200)
        await self.post("/api/stage", {"event": "joined"})
        self.assertTrue(await self.wait_for(lambda: not self.room.swapping, timeout=10))
        self.assertEqual(self.room.outfit, "银色飞行夹克")
        self.assertEqual(self.room.status, "live")
        self.assertTrue(await self.wait_for(lambda: any(f.get("live_id") == first_live and f["type"] == 5 for f in self.mock_app[FRAMES])))
        new_live = self.room.live["id"]
        second = self.mock_app[LIVES][new_live]["body"]["avatar"]
        self.assertNotIn("银色飞行夹克", second["greeting_instruction"])  # no outfit talk until the stage shows it
        self.assertTrue(await self.wait_for(lambda: any(
            f.get("live_id") == new_live and f["type"] == 99 and "银色飞行夹克" in f["payload"]["text_msg"]["content"]
            for f in self.mock_app[FRAMES])))
        _, again = await self.post("/api/danmaku", {"user": "阿明", "text": "换回来"})
        self.assertEqual(again["command"]["item"], "白色T恤")
        await self.post("/api/session/stop")

    async def test_keepalive_pings_during_silence(self):
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "waiting_stage"))
        self.room.channel._keepalive.cancel()
        self.room.channel._keepalive = asyncio.create_task(self.room.channel._ping_loop(every=0.3))
        live = self.mock_app[LIVES][self.room.live["id"]]
        self.assertTrue(await self.wait_for(lambda: live.get("pings", 0) >= 3, timeout=5))
        await self.post("/api/session/stop")

    async def test_budget_guard_stops_session(self):
        self.room.cfg["safety"]["budget_credits"] = 0
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "ended", timeout=25))
        self.assertIn("预算", self.room.end_reason)

    async def test_unwatched_session_stops_itself(self):
        self.room.cfg["safety"]["stage_join_timeout_seconds"] = 0  # nobody joins: stop at the first watchdog tick
        await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertTrue(await self.wait_for(lambda: self.room.status == "ended", timeout=25))
        self.assertIn("没有画面入会", self.room.end_reason)
        self.assertIn("工作台的预览", self.room.end_reason)

    async def test_bad_key_reports_error(self):
        self.room.vidu.api_key = "vda_wrong"
        status, body = await self.post("/api/session/start", {"model": "vidu-s2"})
        self.assertEqual(status, 400)
        self.assertIn("401", body["error"])
        self.assertNotIn("vda_wrong", body["error"])
        self.assertEqual(self.room.status, "error")


if __name__ == "__main__":
    unittest.main()
