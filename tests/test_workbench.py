"""Workbench API against local fakes of Evolink and Vidu (no network, no cost).

    uv run --no-project --with aiohttp --with pillow --with numpy python -m unittest tests.test_workbench -v
"""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
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
from live import router  # noqa: E402
from live.assets import AssetLibrary, AssetResolver  # noqa: E402
from live.moderation import Moderator  # noqa: E402
from live.session import LiveRoom, pick_idle_topic  # noqa: E402
from live.vidu import ViduClient  # noqa: E402
from live.workbench import Workbench, add_routes  # noqa: E402
from test_e2e import serve  # noqa: E402

JPG = (ROOT / "assets/avatars/xiaoshen.jpg").read_bytes()


def fake_providers(calls: list) -> web.Application:
    app = web.Application(client_max_size=50 * 1024 * 1024)
    tasks: dict[str, str] = {}

    async def create(request):
        body = await request.json()
        calls.append((request.path, body))
        task_id = f"task-{len(tasks) + 1}"
        tasks[task_id] = "video" if "videos" in request.path else "image"
        return web.json_response({"id": task_id, "status": "pending"})

    async def task(request):
        kind = tasks[request.match_info["id"]]
        base = f"http://{request.host}"
        result = f"{base}/out.mp4" if kind == "video" else f"{base}/out.png"
        return web.json_response({"id": request.match_info["id"], "status": "completed", "progress": 100,
                                  "results": [result], "usage": {"cost": {"cny": 0.5 if kind == "image" else 4.09}}})

    async def out_png(_):
        return web.Response(body=JPG, content_type="image/png")

    async def out_mp4(_):
        return web.Response(body=b"\x00\x00\x00\x18ftypmp42fake", content_type="video/mp4")

    async def upload(request):
        reader = await request.multipart()
        part = await reader.next()
        data = await part.read()
        calls.append(("upload", {"name": part.filename, "bytes": len(data), "ua": request.headers.get("User-Agent", "")}))
        return web.json_response({"data": {"file_url": f"http://{request.host}/uploaded/{part.filename}"}})

    async def chat(request):
        calls.append(("chat", await request.json()))
        return web.json_response({"choices": [{"message": {"content": "诶，你们来啦，今天想看什么？"}}]})

    async def voices(_):
        return web.json_response({"voices": [{"voice": "tasha_bright"}]})

    async def clone(request):
        body = await request.json()
        calls.append(("clone", body))
        return web.json_response({"voice": body["voice"]})

    app.router.add_post("/v1/images/generations", create)
    app.router.add_post("/v1/videos/generations", create)
    app.router.add_get("/v1/tasks/{id}", task)
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/out.png", out_png)
    app.router.add_get("/out.mp4", out_mp4)
    app.router.add_post("/upload", upload)
    app.router.add_get("/live/v1/voices", voices)
    app.router.add_post("/live/v1/voices/clone", clone)
    return app


class WorkbenchAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="workbench-test-"))
        t = self.tmp
        (t / "assets/avatars/t").mkdir(parents=True)
        (t / "assets/avatars/candidates").mkdir(parents=True)
        for name in ("base.jpg", "jacket.jpg"):
            (t / "assets/avatars/t" / name).write_bytes(JPG)
        (t / "assets/avatars/candidates/gpt_desk.jpg").write_bytes(JPG)
        (t / "assets/avatars/t/outfits.json").write_text(json.dumps({"items": [
            {"name": "吊带裙", "aliases": [], "file": "assets/avatars/t/base.jpg", "base": True},
            {"name": "银色夹克", "aliases": ["夹克"], "file": "assets/avatars/t/jacket.jpg"},
        ]}, ensure_ascii=False), encoding="utf-8")
        profiles = {"tasha": {"name": "Tasha", "note": "", "image": "assets/avatars/t/base.jpg", "voice": "tasha_bright",
                              "persona_file": "persona_tasha.md", "greeting_instruction": "打个招呼",
                              "outfits": "assets/avatars/t/outfits.json"},
                    "other": {"name": "小翼", "image": "assets/avatars/t/base.jpg", "voice": "", "persona_file": "persona_tasha.md"}}
        (t / "assets/avatars/profiles.json").write_text(json.dumps(profiles, ensure_ascii=False), encoding="utf-8")
        (t / "persona_tasha.md").write_text("你叫{name}，晚上开直播陪大家聊天。说话短，像朋友随口聊。\n", encoding="utf-8")  # shared persona file
        shutil.copy(ROOT / "persona.md", t / "persona.md")

        self.calls: list = []
        self.fake_runner, self.fake_url = await serve(fake_providers(self.calls))
        self.http = aiohttp.ClientSession()
        cfg = copy.deepcopy(server.DEFAULTS)
        cfg["avatars"] = copy.deepcopy(profiles)
        cfg["avatar_profile"] = "tasha"
        room = LiveRoom(cfg, t, self.http, None, AssetResolver(AssetLibrary(t / "assets"), None),
                        Moderator.from_file(ROOT / "moderation.json"), t)
        room.vidu = ViduClient(self.http, "vidu-test", base_url=self.fake_url)
        self.room = room
        self.saved_actions = list(router.ACTIONS)
        wb = Workbench(t, t / "assets", room, self.http, "evolink-test")
        wb.api, wb.files_api = self.fake_url + "/v1", self.fake_url + "/upload"
        wb.poll_seconds = {"video": 0.05, "image": 0.05}
        self.wb = wb
        key = web.AppKey("wb", Workbench)
        app = web.Application(client_max_size=50 * 1024 * 1024)
        app[key] = wb
        add_routes(app, key)
        self.runner, self.url = await serve(app)

    async def asyncTearDown(self):
        router.ACTIONS[:] = self.saved_actions
        await self.http.close()
        await self.runner.cleanup()
        await self.fake_runner.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def call(self, method: str, path: str, status: int = 200, **kw):
        async with self.http.request(method, self.url + path, **kw) as resp:
            data = await resp.json() if resp.content_type == "application/json" else await resp.read()
            self.assertEqual(resp.status, status, data)
            return data

    async def wait_job(self, job_id: str) -> dict:
        for _ in range(100):
            jobs = (await self.call("GET", "/api/media/jobs"))["jobs"]
            job = next(j for j in jobs if j["id"] == job_id)
            if job["status"] in ("done", "failed"):
                return job
            await asyncio.sleep(0.05)
        self.fail("job did not finish")

    async def gallery_id(self, source: str = "候选底图") -> str:
        items = (await self.call("GET", "/api/media/gallery"))["items"]
        return next(i["id"] for i in items if i["source"] == source)

    async def test_profile_edit_writes_own_persona_and_motions(self):
        listing = await self.call("GET", "/api/profiles")
        self.assertEqual(listing["current"], "tasha")
        self.assertEqual({p["key"] for p in listing["profiles"]}, {"tasha", "other"})
        detail = await self.call("GET", "/api/profiles/tasha")
        self.assertEqual(len(detail["outfits"]), 2)
        self.assertFalse(detail["custom_idle_motions"])

        motions = [{"name": "托腮", "content": "单手托腮看弹幕", "weight": 2}, {"content": "低头笑一下"}]
        detail = await self.call("PUT", "/api/profiles/tasha", json={
            "name": "Tasha", "persona_text": "你叫 Tasha，说话轻快。" * 3, "idle_motions": motions, "voice": "Sunnybobi"})
        self.assertTrue(detail["custom_idle_motions"])
        self.assertEqual(detail["idle_motions"][1]["name"], "低头笑一下")
        self.assertTrue((self.tmp / "personas/tasha.md").read_text(encoding="utf-8").startswith("你叫 Tasha"))
        saved = json.loads((self.tmp / "assets/avatars/profiles.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["tasha"]["persona_file"], "personas/tasha.md")
        self.assertEqual(saved["other"]["persona_file"], "persona_tasha.md")  # shared file untouched
        self.assertEqual(saved["tasha"]["voice"], "Sunnybobi")
        body = self.room._create_body({"avatar_id": "a1"})
        self.assertEqual([m["content"] for m in body["idle_motion"]["motions"]], ["单手托腮看弹幕", "低头笑一下"])
        self.assertIn("说话轻快", body["avatar"]["persona"])

        await self.call("PUT", "/api/profiles/tasha", 400, json={"persona_text": "太短"})
        await self.call("PUT", "/api/profiles/tasha", 400, json={"idle_motions": [{"content": "x" * 100}]})
        reset = await self.call("PUT", "/api/profiles/tasha", json={"idle_motions": []})
        self.assertFalse(reset["custom_idle_motions"])

    async def test_idle_topics_rotate_without_repeats(self):
        detail = await self.call("GET", "/api/profiles/tasha")
        self.assertFalse(detail["custom_idle_topics"])
        topics = [f"说说第{i}件小事" for i in range(9)]
        detail = await self.call("PUT", "/api/profiles/tasha", json={"idle_topics": "\n".join(topics + ["", topics[0], "说说第9件小事。"])})
        self.assertTrue(detail["custom_idle_topics"])
        self.assertEqual(detail["idle_topics"], topics + ["说说第9件小事"])  # blanks, repeats and the full stop dropped
        await self.call("PUT", "/api/profiles/tasha", 400, json={"idle_topics": ["x" * 61]})
        used: set = set()
        picks: list[str] = []
        for _ in range(len(topics) * 10):
            picks.append(pick_idle_topic(topics, used, picks[-1] if picks else ""))
        for start in range(0, len(picks), len(topics)):  # each round uses every topic exactly once
            self.assertEqual(sorted(picks[start:start + len(topics)]), sorted(topics))
        self.assertTrue(all(a != b for a, b in zip(picks, picks[1:])))  # no repeat across a round boundary
        reset = await self.call("PUT", "/api/profiles/tasha", json={"idle_topics": []})
        self.assertFalse(reset["custom_idle_topics"])

    async def test_new_profile_from_gallery_image(self):
        await self.call("PUT", "/api/profiles/Bad-Key", 400, json={"name": "x"})
        await self.call("PUT", "/api/profiles/xiaoyu", 400, json={"name": "小雨"})
        detail = await self.call("PUT", "/api/profiles/xiaoyu", json={
            "name": "小雨", "base_gallery_id": await self.gallery_id(), "base_outfit_name": "白T恤"})
        self.assertEqual(detail["outfits"][0]["name"], "白T恤")
        self.assertTrue(detail["outfits"][0]["base"])
        self.assertIn("xiaoyu", self.room.cfg["avatars"])
        saved = json.loads((self.tmp / "assets/avatars/profiles.json").read_text(encoding="utf-8"))
        self.assertTrue((self.tmp / saved["xiaoyu"]["image"]).exists())
        self.assertTrue((self.tmp / saved["xiaoyu"]["persona_file"]).exists())
        async with self.http.get(self.url + detail["image_url"]) as resp:
            self.assertEqual(resp.status, 200)

    async def test_wardrobe_add_rename_delete_and_base(self):
        gid = await self.gallery_id()
        detail = await self.call("POST", "/api/profiles/tasha/outfits", json={"name": "白T恤", "aliases": "T恤，白色T恤", "gallery_id": gid})
        tee = next(o for o in detail["outfits"] if o["name"] == "白T恤")
        self.assertEqual(tee["aliases"], ["T恤", "白色T恤"])
        await self.call("POST", "/api/profiles/tasha/outfits", 400, json={"name": "白T恤", "gallery_id": gid})
        detail = await self.call("PATCH", "/api/profiles/tasha/outfits/白T恤", json={"new_name": "纯白T恤", "aliases": ["白T"]})
        self.assertIn("纯白T恤", [o["name"] for o in detail["outfits"]])
        self.assertEqual(self.room.wardrobe().find("白T")["name"], "纯白T恤")  # the live room sees the edit
        await self.call("DELETE", "/api/profiles/tasha/outfits/吊带裙", 400)
        detail = await self.call("DELETE", "/api/profiles/tasha/outfits/纯白T恤")
        self.assertEqual(len(detail["outfits"]), 2)

        old = self.room.avatar_cfg["image"]
        detail = await self.call("POST", "/api/profiles/tasha/base", json={"gallery_id": gid, "outfit_name": "卫衣"})
        self.assertNotEqual(self.room.avatar_cfg["image"], old)
        base = next(o for o in detail["outfits"] if o["base"])
        self.assertEqual(base["name"], "卫衣")
        self.assertEqual(self.room.wardrobe().base()["file"], self.room.avatar_cfg["image"])

    async def test_image_job_with_reference_lands_in_gallery(self):
        gid = await self.gallery_id()
        job = (await self.call("POST", "/api/media/image", json={
            "model": "nano-banana-2.1", "prompt": "她坐在书桌前", "ref_ids": [gid], "size": "9:16", "photo_real": True}))["job"]
        done = await self.wait_job(job["id"])
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["cost_cny"], 0.5)
        path, body = next(c for c in self.calls if c[0] == "/v1/images/generations")
        self.assertEqual(body["model"], "gemini-nano-banana-2.1")
        self.assertNotIn("quality", body)
        self.assertTrue(body["image_urls"][0].startswith("data:image/jpeg;base64,"))
        self.assertIn("NOT CG", body["prompt"])
        items = (await self.call("GET", "/api/media/gallery?kind=image"))["items"]
        self.assertEqual(items[0]["id"], done["result_id"])
        async with self.http.get(self.url + items[0]["url"]) as resp:
            self.assertEqual(resp.status, 200)
        async with self.http.get(self.url + items[0]["thumb_url"]) as resp:  # the wall loads small copies
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.content_type, "image/jpeg")
        await self.call("POST", "/api/media/image", 400, json={"model": "dall-e", "prompt": "x"})
        await self.call("POST", "/api/media/image", 404, json={"prompt": "x", "ref_ids": ["nope"]})

    async def test_outfit_generate_and_regenerate(self):
        job = (await self.call("POST", "/api/profiles/tasha/outfits/generate", json={
            "name": "白T恤", "description": "a plain white cotton T-shirt", "model": "gpt-image-2.5"}))["job"]
        self.assertEqual((await self.wait_job(job["id"]))["status"], "done")
        tee = next(i for i in self.room.wardrobe().items if i["name"] == "白T恤")
        self.assertTrue((self.tmp / tee["file"]).exists())
        _, body = [c for c in self.calls if c[0] == "/v1/images/generations"][-1]
        self.assertIn("a plain white cotton T-shirt", body["prompt"])
        self.assertEqual(body["quality"], "high")

        old_files = {i["name"]: i["file"] for i in self.room.wardrobe().items}
        jobs = (await self.call("POST", "/api/profiles/tasha/outfits/regenerate", json={"model": "gpt-image-2.5"}))["jobs"]
        self.assertEqual(len(jobs), 2)  # every outfit except the base
        for j in jobs:
            self.assertEqual((await self.wait_job(j["id"]))["status"], "done")
        new_files = {i["name"]: i["file"] for i in self.room.wardrobe().items}
        self.assertEqual(new_files["吊带裙"], old_files["吊带裙"])
        self.assertNotEqual(new_files["银色夹克"], old_files["银色夹克"])
        self.assertEqual(len(self.room.wardrobe().items), 3)

    async def test_video_reference_job_uploads_image_and_audio(self):
        wav = self.tmp / "voice.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=220:d=8", str(wav)], check=True)
        form = aiohttp.FormData()
        form.add_field("file", wav.read_bytes(), filename="voice.wav", content_type="audio/wav")
        audio = await self.call("POST", "/api/media/upload", data=form)
        self.assertEqual(audio["kind"], "audio")
        gid = await self.gallery_id()
        await self.call("POST", "/api/media/video", 400, json={"mode": "image", "prompt": "x"})
        await self.call("POST", "/api/media/video", 400, json={"mode": "image", "prompt": "x", "image_id": gid, "audio_id": audio["id"]})
        job = (await self.call("POST", "/api/media/video", json={
            "model": "seedance-2.0", "mode": "reference", "prompt": "她笑着说话", "image_id": gid, "audio_id": audio["id"],
            "duration": 8, "resolution": "480p"}))["job"]
        done = await self.wait_job(job["id"])
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["cost_cny"], 4.09)
        _, body = next(c for c in self.calls if c[0] == "/v1/videos/generations")
        self.assertEqual(body["model"], "seedance-2.0-reference-to-video")
        self.assertTrue(body["image_urls"][0].startswith(self.fake_url + "/uploaded/"))
        self.assertTrue(body["audio_urls"][0].endswith(".wav"))
        self.assertTrue(body["generate_audio"])
        uploads = [c[1] for c in self.calls if c[0] == "upload"]
        self.assertTrue(all("Mozilla" in u["ua"] for u in uploads))
        video = (await self.call("GET", "/api/media/gallery?kind=video"))["items"][0]
        self.assertEqual(video["id"], done["result_id"])
        await self.call("DELETE", f"/api/media/gallery/{video['id']}")
        self.assertFalse((self.tmp / "assets" / video["file"]).exists())

    async def test_actions_table_drives_the_classifier(self):
        actions = (await self.call("GET", "/api/actions"))["actions"]
        self.assertTrue(any("跳舞" in a["keywords"] for a in actions))
        new = actions + [{"keywords": ["后空翻"], "action": "假装要做后空翻，最后只是跳了一下"}]
        await self.call("PUT", "/api/actions", json={"actions": new})
        command = router.classify("小王", "来个后空翻")
        self.assertEqual(command.kind, "action")
        self.assertEqual(command.action_text, "假装要做后空翻，最后只是跳了一下")
        self.assertTrue((self.tmp / "actions.json").exists())
        reloaded = Workbench(self.tmp, self.tmp / "assets", self.room, self.http, None)
        self.assertIn(["后空翻"], [a["keywords"] for a in reloaded.actions_public()])
        await self.call("PUT", "/api/actions", 400, json={"actions": []})

    async def test_voice_preview_and_clone(self):
        wav = self.tmp / "sample.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=220:d=12", "-ar", "16000", str(wav)], check=True)

        def form(**fields):
            f = aiohttp.FormData()
            f.add_field("file", wav.read_bytes(), filename="sample.wav", content_type="audio/wav")
            for k, v in fields.items():
                f.add_field(k, str(v))
            return f

        audio = await self.call("POST", "/api/voices/preview", data=form(speed=1.2, pitch=2))
        out = self.tmp / "preview.wav"
        out.write_bytes(audio)
        seconds = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                                       capture_output=True, text=True).stdout)
        self.assertAlmostEqual(seconds, 10.0, delta=0.3)

        voices = await self.call("GET", "/api/voices")
        self.assertEqual(voices["cloned"], [{"voice": "tasha_bright"}])
        self.assertEqual(voices["used_by"]["tasha_bright"], ["tasha"])
        self.assertTrue(any(v["voice"] == "Sunnybobi" for v in voices["system"]))

        await self.call("POST", "/api/voices/clone", 400, data=form(name="中文名"))
        result = await self.call("POST", "/api/voices/clone", data=form(name="tasha_v3", speed=1.1, pitch=1))
        self.assertEqual(result["voice"], "tasha_v3")
        self.assertEqual(result["text"], "诶，你们来啦，今天想看什么？")  # transcribed because no text was given
        _, clone = next(c for c in self.calls if c[0] == "clone")
        self.assertEqual(clone["provider"], "qwen_omni")
        self.assertTrue(clone["audio_url"].startswith(self.fake_url + "/uploaded/"))
        self.assertEqual(clone["text"], result["text"])
        result = await self.call("POST", "/api/voices/clone", data=form(name="tasha_v4", text="我自己写的台词"))
        self.assertEqual(result["text"], "我自己写的台词")
        self.assertEqual(len([c for c in self.calls if c[0] == "chat"]), 1)

    async def test_voice_samples_are_listed_per_voice(self):
        wav = self.tmp / "clip.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=330:d=3", str(wav)], check=True)
        form = aiohttp.FormData()
        form.add_field("file", wav.read_bytes(), filename="clip.wav", content_type="audio/wav")
        form.add_field("voice", "sd_anan")
        form.add_field("label", "安安直播片段 1")
        item = await self.call("POST", "/api/voices/samples", data=form)
        self.assertEqual(item["voice"], "sd_anan")
        voices = await self.call("GET", "/api/voices")
        self.assertEqual(voices["samples"]["sd_anan"][0]["label"], "安安直播片段 1")
        self.assertEqual(voices["samples"]["sd_anan"][0]["sample_kind"], "live")
        renamed = await self.call("PATCH", f"/api/media/gallery/{item['id']}", json={"label": "改个名"})
        self.assertEqual(renamed["prompt"], "改个名")
        bad = aiohttp.FormData()
        bad.add_field("file", wav.read_bytes(), filename="clip.wav", content_type="audio/wav")
        await self.call("POST", "/api/voices/samples", 400, data=bad)

    async def test_curated_library_folds_old_voices(self):
        (self.tmp / "assets/voices.json").write_text(json.dumps({"library": [
            {"voice": "tasha_bright", "name": "测试音色", "desc": "", "source_score": 8, "live_score": 7}]}, ensure_ascii=False), encoding="utf-8")
        voices = await self.call("GET", "/api/voices")
        self.assertEqual([v["voice"] for v in voices["library"]], ["tasha_bright"])
        self.assertEqual(voices["cloned"], [])  # the library voice is not listed again among the old ones
        self.assertEqual(voices["old_count"], len(voices["system"]))

    async def test_files_route_stays_inside_assets(self):
        async with self.http.get(self.url + "/files/avatars/t/base.jpg") as resp:
            self.assertEqual(resp.status, 200)
        for bad in ("/files/../persona.md", "/files/%2e%2e/persona.md", "/files/.send/x.jpg", "/files/avatars/../../persona.md"):
            async with self.http.get(self.url + bad) as resp:
                self.assertIn(resp.status, (403, 404), bad)

    async def test_jobs_resume_after_restart(self):
        job = self.wb._new_job("image", "恢复测试", "gpt-image-2.5", source="生图")
        job.update(status="running", task_id="task-99")
        self.wb._save_jobs()
        # the fake only knows tasks it created, so make one first
        await self.call("POST", "/api/media/image", json={"prompt": "占位", "model": "gpt-image-2.5"})
        job["task_id"] = "task-1"
        self.wb._save_jobs()
        fresh = Workbench(self.tmp, self.tmp / "assets", self.room, self.http, "evolink-test")
        fresh.api, fresh.poll_seconds = self.fake_url + "/v1", {"video": 0.05, "image": 0.05}
        fresh.resume_jobs()
        for _ in range(100):
            resumed = next(j for j in fresh.jobs if j["id"] == job["id"])
            if resumed["status"] == "done":
                break
            await asyncio.sleep(0.05)
        self.assertEqual(resumed["status"], "done")


if __name__ == "__main__":
    unittest.main()
