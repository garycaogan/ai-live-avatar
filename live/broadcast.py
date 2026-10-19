"""Push the live room to YouTube (RTMP) or a local file without OBS.

The stage page forwards the avatar's raw RTC audio/video (MediaRecorder webm chunks)
over a WebSocket. Here Pillow draws the overlay (AI label, countdown, current command,
subtitle, queue) as a transparent RGBA frame twice a second, and ffmpeg composites and encodes.
Page timers in a backgrounded browser window are throttled, so nothing timing-critical
runs in the page.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = "/System/Library/Fonts/STHeiti Medium.ttc"
FONT_REG = "/System/Library/Fonts/Hiragino Sans GB.ttc"
KIND = {"chat": "聊天", "action": "动作", "garment": "换装", "outfit": "换装", "object": "道具", "background": "换背景", "reset": "复原", "gift_thanks": "礼物"}
STATS = re.compile(r"frame=\s*(\d+).*?fps=\s*([\d.]+).*?bitrate=\s*([\d.]+\w+/s|N/A).*?speed=\s*([\d.]+x|N/A)")


class OverlayRenderer:
    def __init__(self, width: int, height: int, box_top: float = 0.14, box_height: float = 0.47, panel_path: Path | None = None):
        self.w, self.h = width, height
        self.s = width / 1080
        self.box_top = round(height * box_top) // 2 * 2
        self.box_bottom = self.box_top + round(height * box_height) // 2 * 2
        self._fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
        self.panel_path = panel_path
        self._panel: tuple[float, dict] | None = None

    def panel(self) -> dict | None:
        """Test notes shown like the Douyin reference room; the same file feeds static/stage.html."""
        if self.panel_path and not self.panel_path.exists():  # the shipped example until the user writes their own
            example = self.panel_path.with_name(self.panel_path.stem + ".example.json")
            if example.exists():
                self.panel_path = example
        if not self.panel_path or not self.panel_path.exists():
            return None
        mtime = self.panel_path.stat().st_mtime
        if not self._panel or self._panel[0] != mtime:
            try:
                self._panel = (mtime, json.loads(self.panel_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                return self._panel[1] if self._panel else None
        return self._panel[1]

    def _mark(self, d: ImageDraw.ImageDraw, cx: float, cy: float, ok: bool) -> None:
        # ✓ and ✕ are missing from the system CJK fonts, so the marks are drawn.
        s, r = self.s, round(11 * self.s)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 102, 89, 255) if ok else (255, 255, 255, 52))
        w = max(2, round(2.2 * s))
        if ok:
            d.line([(cx - 4.5 * s, cy + 0.2 * s), (cx - 1.5 * s, cy + 3.2 * s), (cx + 4.5 * s, cy - 3.2 * s)], fill="white", width=w, joint="curve")
        else:
            for a, b in (((-3.5, -3.5), (3.5, 3.5)), ((3.5, -3.5), (-3.5, 3.5))):
                d.line([(cx + a[0] * s, cy + a[1] * s), (cx + b[0] * s, cy + b[1] * s)], fill="white", width=w)

    def _draw_panels(self, d: ImageDraw.ImageDraw, snap: dict, box: bool) -> None:
        p = self.panel()
        if not p:
            return
        s, W = self.s, self.w
        width, padx, pady, lh = round(340 * s), round(20 * s), round(18 * s), round(36 * s)
        tips_width = round(312 * s)
        f_item, f_head, f_title = self.font(22), self.font(22, True), self.font(22, True)
        hints = p.get("outfit_hint") or {}
        hint = hints.get(snap.get("profile") or "") or hints.get("default", "")
        inner = width - 2 * padx

        def items_height(items, cols):
            return ((len(items) + cols - 1) // cols) * lh

        def draw_items(x, y, items, cols, ok):
            col_w = (inner - round(10 * s) * (cols - 1)) / cols
            for i, text in enumerate(items):
                cx = x + (i % cols) * (col_w + round(10 * s))
                cy = y + (i // cols) * lh + lh / 2
                self._mark(d, cx + round(11 * s), cy, ok)
                d.text((cx + round(30 * s), cy), text, font=f_item, fill=(255, 255, 255, 255 if ok else 184), anchor="lm")

        card = (6, 25, 36, 168)
        # Left: title, what works, what does not yet.
        can, cannot = [t for t in p.get("can") or [] if t], [t for t in p.get("cannot") or [] if t]
        title_lines = self._wrap(d, p.get("title", ""), f_title, inner - round(28 * s), 2) if p.get("title") else []
        title_h = round((14 + 30 * len(title_lines)) * s) if title_lines else 0
        head_h = round(32 * s)
        body_h = title_h + (round(12 * s) if title_lines else 0) + head_h + items_height(can, 2) + round(8 * s) + head_h + items_height(cannot, 2)
        x0 = round(24 * s) if box else round(48 * s)
        y0 = self.box_top + round(24 * s) if box else round(250 * s)
        d.rounded_rectangle((x0, y0, x0 + width, y0 + body_h + 2 * pady), radius=round(16 * s), fill=card)
        y = y0 + pady
        if title_lines:
            d.rounded_rectangle((x0 + padx, y, x0 + width - padx, y + title_h), radius=round(12 * s), fill=(0, 102, 89, 235))
            for i, line in enumerate(title_lines):
                d.text((x0 + padx + round(14 * s), y + round((7 + 30 * i) * s)), line, font=f_title, fill="white")
            y += title_h + round(12 * s)
        d.text((x0 + padx, y), p.get("can_title", ""), font=f_head, fill=(233, 255, 252, 255))
        draw_items(x0 + padx, y + head_h, can, 2, True)
        y += head_h + items_height(can, 2) + round(8 * s)
        d.text((x0 + padx, y), p.get("cannot_title", ""), font=f_head, fill=(233, 255, 252, 255))
        draw_items(x0 + padx, y + head_h, cannot, 2, False)

        # Right: how to write danmaku that work.
        good = [t.replace("{outfit_hint}", hint) for t in p.get("good") or [] if t]
        good = [t for t in good if t]
        bad = [t for t in p.get("bad") or [] if t]
        body_h = head_h + items_height(good, 1) + items_height(bad, 1)
        inner = tips_width - 2 * padx
        x1 = W - (round(24 * s) if box else round(48 * s)) - tips_width
        y1 = self.box_top + round(24 * s) if box else round(236 * s)
        d.rounded_rectangle((x1, y1, x1 + tips_width, y1 + body_h + 2 * pady), radius=round(16 * s), fill=card)
        y = y1 + pady
        d.text((x1 + padx, y), p.get("tips_title", ""), font=f_head, fill=(233, 255, 252, 255))
        draw_items(x1 + padx, y + head_h, good, 1, True)
        draw_items(x1 + padx, y + head_h + items_height(good, 1), bad, 1, False)

    def font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        px = max(10, round(size * self.s))
        key = (FONT_BOLD if bold else FONT_REG, px)
        if key not in self._fonts:
            self._fonts[key] = ImageFont.truetype(key[0], px)
        return self._fonts[key]

    def _wrap(self, draw: ImageDraw.ImageDraw, text: str, font, max_w: int, max_lines: int) -> list[str]:
        lines, line = [], ""
        for ch in text:
            if draw.textlength(line + ch, font=font) > max_w:
                lines.append(line)
                line = ch
                if len(lines) == max_lines:
                    break
            else:
                line += ch
        if line and len(lines) < max_lines:
            lines.append(line)
        if len(lines) == max_lines and "".join(lines) != text:
            lines[-1] = lines[-1][:-1] + "…"
        return lines

    def render(self, snap: dict, subtitle: str) -> bytes:
        s, W, H = self.s, self.w, self.h
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        pad = round(48 * s)
        card = (6, 25, 36, 190)
        brand = (0, 102, 89, 235)

        def pill(x, y, text, size, fill, bold=True, color=(255, 255, 255, 255)):
            f = self.font(size, bold)
            tw = d.textlength(text, font=f)
            ph, pw = round(size * 1.9 * s), tw + round(40 * s)
            d.rounded_rectangle((x, y, x + pw, y + ph), radius=round(16 * s), fill=fill)
            d.text((x + round(20 * s), y + ph / 2), text, font=f, fill=color, anchor="lm")
            return y + ph

        y = pill(pad, round(56 * s), "● AI 实时生成画面", 30, brand)
        pill(pad, y + round(10 * s), f"虚拟主播 {snap.get('avatar_name', '')}", 26, card, bold=False, color=(255, 255, 255, 215))

        r = round(75 * s)
        cx, cy = W - pad - r, round(56 * s) + r
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=card)
        nxt = snap.get("next_in")
        d.text((cx, cy - round(10 * s)), "--" if nxt is None else str(nxt), font=self.font(56, True), fill="white", anchor="mm")
        d.text((cx, cy + round(36 * s)), "下一条指令", font=self.font(20), fill=(255, 255, 255, 190), anchor="mm")

        # Full layout: overlays sit on the lower part of the video. Box layout: the video is a box near the
        # top, so the command card hugs its lower edge, subtitles follow, and the swap notice goes inside it.
        box = (snap.get("broadcast") or {}).get("layout") == "box"
        self._draw_panels(d, snap, box)
        card_top = self.box_bottom + round(16 * s) if box else round(1060 * s)
        sub_top, sub_lines = (self.box_bottom + round(200 * s), 2) if box else (round(1250 * s), 3)
        pill_top = self.box_bottom - round(100 * s) if box else round(1470 * s)

        cur = snap.get("current")
        if cur and cur.get("executed_at") and time.time() - cur["executed_at"] < 15:
            top = card_top
            text = f"@{cur['user']}：{cur.get('text') or KIND.get(cur['kind'], '')}"
            f = self.font(34, True)
            lines = self._wrap(d, text, f, W - 2 * pad - round(56 * s), 2)
            hgt = round((40 + 48 * len(lines)) * s) + round(24 * s)
            d.rounded_rectangle((pad, top, W - pad, top + hgt), radius=round(16 * s), fill=brand)
            d.text((pad + round(28 * s), top + round(14 * s)), "正在执行", font=self.font(24), fill=(233, 255, 252, 255))
            for i, line in enumerate(lines):
                d.text((pad + round(28 * s), top + round((46 + 48 * i) * s)), line, font=f, fill="white")

        if subtitle:
            f = self.font(42, True)
            lines = self._wrap(d, subtitle, f, W - 2 * round(60 * s), sub_lines)
            for i, line in enumerate(lines):
                d.text((W / 2, sub_top + round(60 * i * s)), line, font=f, fill="white", anchor="mm",
                       stroke_width=max(2, round(4 * s)), stroke_fill=(0, 0, 0, 230))

        swap = snap.get("swapping")
        if swap:
            # Symbols like ✦ are missing from the system CJK fonts, so the leading mark is drawn.
            text = f"正在换上{swap.get('to', '')} · @{swap.get('user', '')}"
            f = self.font(30, True)
            tw = d.textlength(text, font=f)
            dot, gap = round(9 * s), round(14 * s)
            ph, pw = round(64 * s), tw + 2 * dot + gap + round(56 * s)
            x0, y0 = (W - pw) / 2, pill_top
            d.rounded_rectangle((x0, y0, x0 + pw, y0 + ph), radius=ph / 2, fill=(255, 255, 255, 225))
            dx, dy = x0 + round(28 * s) + dot, y0 + ph / 2
            d.ellipse((dx - dot, dy - dot, dx + dot, dy + dot), fill=(0, 102, 89, 255 if int(time.time() * 2) % 2 else 120))
            d.text((dx + dot + gap, dy), text, font=f, fill=(0, 102, 89, 255), anchor="lm")

        queue = sorted(snap.get("queue") or [], key=lambda c: (-c["gift"], c["created_at"]))
        bottom = H - round(210 * s)
        items = queue[:3]
        hgt = round((70 + 44 * max(1, len(items))) * s)
        d.rounded_rectangle((pad, bottom - hgt, W - pad, bottom), radius=round(16 * s), fill=card)
        title = f"弹幕排队中 · {len(queue)} 条" if queue else "发条弹幕跟我聊聊，或者让我做个动作"
        d.text((pad + round(28 * s), bottom - hgt + round(20 * s)), title, font=self.font(26, True), fill=(233, 255, 252, 255))
        for i, c in enumerate(items):
            yy = bottom - hgt + round((66 + 44 * i) * s)
            who = c["user"][:8]
            d.text((pad + round(28 * s), yy), who, font=self.font(28, True), fill=(233, 255, 252, 255))
            x2 = pad + round(28 * s) + d.textlength(who, font=self.font(28, True)) + round(12 * s)
            body = (c.get("text") or KIND.get(c["kind"], ""))[:18] + ("（道具准备中）" if c["status"] == "preparing" else "") 
            d.text((x2, yy), body, font=self.font(28), fill="white")
        # No "gifts jump the queue" here: Douyin treats that as inducing tips.
        d.text((W / 2, H - round(120 * s)), f"每 {snap.get('interval_seconds', 20)} 秒执行一条弹幕｜低俗、引流内容不执行",
               font=self.font(24), fill=(255, 255, 255, 190), anchor="mm")
        return img.tobytes()  # raw RGBA: works with minimal ffmpeg builds that lack a PNG decoder


class Broadcaster:
    """One long-lived encoder, fed at a steady frame rate by a clock thread.

    Every stage-page push (one per Vidu session) is decoded by its own short-lived ffmpeg into
    raw frames and PCM, queued behind a small jitter buffer and played out by the clock. When a
    push ends (an outfit swap restarts the Vidu session), the clock keeps sending the last frame
    and silence, so the RTMP stream never drops and the overlay can show the transition.
    """

    PREBUFFER = 8          # frames queued before playback (re)starts, absorbs 500 ms chunk bursts
    MAX_QUEUE = 75         # ~3 s at 25 fps; older frames are dropped to bound latency

    def __init__(self, room, root: Path, cfg: dict):
        self.room = room
        self.root = root
        self.cfg = cfg
        self.target = "off"
        self.state = "idle"
        self.error = ""
        self.stats: dict = {}
        self.started_at: float | None = None
        self.output: str = ""
        self.segments = 0
        self.renderer = OverlayRenderer(cfg["width"], cfg["height"], cfg.get("box_top", 0.14), cfg.get("box_height", 0.47),
                                        panel_path=root / "assets" / "stage_panel.json")
        w, h = cfg["width"], cfg["height"]
        self.frame_size = w * h * 3 // 2
        self.audio_chunk = (44100 // cfg["fps"]) * 4
        self._black = bytes([16]) * (w * h) + bytes([128]) * (w * h // 2)
        self._frames: deque = deque()
        self._audio = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._encoder: subprocess.Popen | None = None
        self._audio_w: int | None = None
        self._overlay_w: int | None = None
        self._threads: list[threading.Thread] = []
        self._overlay_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None

    @property
    def receiving(self) -> bool:
        return self._ws is not None

    # ---------- status ----------
    def youtube_key(self) -> str | None:
        path = Path(self.cfg["youtube_key_file"]).expanduser()
        return path.read_text().strip() if path.exists() else None

    def public(self) -> dict:
        return {"target": self.target, "state": self.state, "error": self.error, "stats": self.stats,
                "since": self.started_at, "output": self.output, "youtube_key": bool(self.youtube_key()),
                "segments": self.segments, "receiving": self.receiving, "layout": self.cfg.get("layout", "full")}

    def _redact(self, text: str) -> str:
        key = self.youtube_key()
        return text.replace(key, "[stream-key]") if key else text

    def _notify(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self.room.broadcast, "state")

    def _log(self, level: str, message: str, **data) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: self.room.log(level, message, **data))

    def _destination(self) -> tuple[list[str], str]:
        if self.target == "youtube":
            key = self.youtube_key()
            if not key:
                raise RuntimeError("没有 YouTube 推流码（~/.deepsea/youtube/stream.key）")
            return ["-f", "flv", f"{self.cfg['rtmp_base'].rstrip('/')}/{key}"], "YouTube"
        folder = self.root / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"broadcast-{time.strftime('%Y%m%d-%H%M%S')}.flv"
        return ["-f", "flv", str(path)], str(path.relative_to(self.root))

    # ---------- encoder ----------
    def _start_encoder(self) -> None:
        c = self.cfg
        dest_args, label = self._destination()
        audio_r, self._audio_w = os.pipe()
        overlay_r, self._overlay_w = os.pipe()
        size = f"{c['width']}x{c['height']}"
        args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-stats_period", "2",
                "-probesize", "32", "-analyzeduration", "0", "-thread_queue_size", "64", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", size, "-framerate", str(c["fps"]), "-i", "pipe:0",
                "-probesize", "32", "-analyzeduration", "0", "-thread_queue_size", "256", "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", f"pipe:{audio_r}",
                "-probesize", "32", "-analyzeduration", "0", "-thread_queue_size", "16", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", size, "-framerate", "2", "-i", f"pipe:{overlay_r}",
                "-filter_complex", "[0:v][2:v]overlay=0:0:eof_action=repeat:repeatlast=1[v]",
                "-map", "[v]", "-map", "1:a",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                "-g", str(c["fps"] * 2), "-keyint_min", str(c["fps"] * 2), "-sc_threshold", "0",
                "-b:v", c["video_bitrate"], "-maxrate", c["video_bitrate"], "-bufsize", c["video_bufsize"],
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", *dest_args]
        self._encoder = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(audio_r, overlay_r), bufsize=0)
        os.close(audio_r)
        os.close(overlay_r)
        self._stop.clear()
        with self._lock:
            self._frames.clear()
            self._audio.clear()
        self.state, self.error, self.stats, self.started_at, self.output, self.segments = "live", "", {}, time.time(), label, 0
        # One writer thread per pipe: while ffmpeg opens and probes its inputs one by one, a single
        # thread writing video then audio deadlocks as soon as one pipe fills up.
        self._vq, self._aq = queue.Queue(maxsize=50), queue.Queue(maxsize=200)
        workers = [(self._clock_loop, ()), (self._encoder_stderr_loop, ()),
                   (_pipe_writer, (self._vq, self._encoder.stdin.fileno())), (_pipe_writer, (self._aq, self._audio_w))]
        for target, args in workers:
            t = threading.Thread(target=target, args=args, daemon=True)
            t.start()
            self._threads.append(t)
        self._overlay_task = asyncio.ensure_future(self._overlay_loop())
        self._log("info", f"开始推流到 {label}（常驻编码，切换会话不断流）")

    def _clock_loop(self) -> None:
        """Play frames at a fixed rate. When input stops (an outfit swap restarts the Vidu session), loop the
        last few seconds back and forth so she keeps moving slightly instead of freezing, then crossfade
        into the new session's frames so the outfit changes smoothly rather than cutting."""
        fps = self.cfg["fps"]
        period = 1 / fps
        silence = bytes(self.audio_chunk)
        last = self._black
        ring: deque = deque(maxlen=fps * 3)
        playing, gap, loop_idx, loop_dir = False, 0, 0, -1
        fade_from, fade_left, fade_len = None, 0, max(1, fps // 2)
        t0, n = time.perf_counter(), 0
        enc = self._encoder
        while not self._stop.is_set():
            with self._lock:
                if not playing and len(self._frames) >= self.PREBUFFER:
                    playing = True
                    if gap > fps // 3 and ring:
                        fade_from, fade_left = last, fade_len
                    gap = 0
                if playing and not self._frames:
                    playing = False
                    loop_idx, loop_dir = len(ring) - 1, -1
                if playing:
                    frame = self._frames.popleft()
                    ring.append(frame)
                    chunk = bytes(self._audio[:self.audio_chunk])
                    del self._audio[:self.audio_chunk]
                    chunk += silence[len(chunk):]
                else:
                    frame, chunk = None, silence
                while len(self._frames) > self.MAX_QUEUE:
                    self._frames.popleft()
                max_audio = self.audio_chunk * (len(self._frames) + self.PREBUFFER)
                if len(self._audio) > max_audio:
                    del self._audio[:len(self._audio) - max_audio]
            if frame is not None:
                if fade_left:
                    w = 1 - fade_left / (fade_len + 1)
                    frame = _blend(fade_from, frame, w)
                    fade_left -= 1
                last = frame
            else:
                gap += 1
                if len(ring) > fps:  # idle loop: ping-pong through the last seconds of real motion
                    loop_idx += loop_dir
                    if loop_idx <= 0 or loop_idx >= len(ring) - 1:
                        loop_dir = -loop_dir
                        loop_idx = max(0, min(len(ring) - 1, loop_idx))
                    last = ring[loop_idx]
            if enc.poll() is not None:
                break
            # Never block the clock, and never let a full video queue starve the audio: while ffmpeg opens
            # its inputs one by one it reads no video, and if audio stops too it waits forever (the
            # recording then stays a second long). Each pipe drops its own tick when full.
            try:
                self._vq.put_nowait(last)
            except queue.Full:
                pass
            try:
                self._aq.put_nowait(chunk)
            except queue.Full:
                pass
            n += 1
            delay = t0 + n * period - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -1:
                t0, n = time.perf_counter(), 0   # fell far behind (machine stall): resync instead of bursting

    def _encoder_stderr_loop(self) -> None:
        buf = b""
        tail = []
        enc = self._encoder
        while True:
            chunk = enc.stderr.read(1024)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = self._redact(raw.decode("utf-8", "replace")).strip()
                if not line:
                    continue
                m = STATS.search(line)
                if m:
                    self.stats = {"frames": int(m.group(1)), "fps": float(m.group(2)), "bitrate": m.group(3), "speed": m.group(4)}
                    self._notify()
                else:
                    tail.append(line)
                    tail[:] = tail[-8:]
                    self._log("warn", "ffmpeg", line=line[:300])
        code = enc.wait()
        if self.state == "live":
            self.state = "error" if code not in (0, 255) else "ended"
            self.error = (tail[-1] if tail else f"ffmpeg 退出码 {code}")[:300] if self.state == "error" else ""
            self._log("error" if self.state == "error" else "info", "编码进程退出", code=code, error=self.error)
            self._notify()

    async def _overlay_loop(self) -> None:
        # Paced by the wall clock: ffmpeg waits for this input to keep up with the video.
        loop = asyncio.get_running_loop()
        t0, n = loop.time(), 0
        try:
            while not self._stop.is_set():
                snap = self.room.snapshot()
                subtitle = self.room.subtitle if time.time() - getattr(self.room, "subtitle_at", 0) < 10 else ""
                frame = await loop.run_in_executor(None, self.renderer.render, snap, subtitle)
                await loop.run_in_executor(None, _write_all, self._overlay_w, frame)
                n += 1
                delay = t0 + n * 0.5 - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
        except (BrokenPipeError, OSError):
            pass

    async def stop(self, reason: str = "手动停止") -> None:
        self.target = "off"
        if self._ws is not None and not self._ws.closed:
            await self._ws.close(message=reason.encode()[:120])
        if self._encoder is None:
            return
        self._stop.set()
        enc = self._encoder
        for q in (self._vq, self._aq):
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        await asyncio.sleep(0.3)
        for fd in (self._audio_w, self._overlay_w):
            try:
                os.close(fd)
            except (OSError, TypeError):
                pass
        try:
            enc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.get_running_loop().run_in_executor(None, _wait_or_kill, enc, 8)
        if self._overlay_task:
            self._overlay_task.cancel()
        self._encoder = None
        if self.state == "live":
            self.state = "ended"
        self.room.log("info", f"推流结束：{reason}")
        self.room.broadcast("state")

    # ---------- one push per Vidu session ----------
    async def ingest(self, ws, hello: dict) -> None:
        """Decode one stage push (webm) into the shared frame queue."""
        self._loop = asyncio.get_running_loop()
        if self.target == "off":
            await ws.close(message=b"broadcast off")
            return
        if self._ws is not None and not self._ws.closed:
            await ws.close(message=b"another stage is already pushing")
            return
        if self._encoder is None or self._encoder.poll() is not None:
            self._start_encoder()
        self._ws = ws
        self.segments += 1
        c = self.cfg
        has_audio = bool(hello.get("hasAudio"))
        audio_r, audio_w = os.pipe()
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts", "-f", "matroska", "-i", "pipe:0",
                "-map", "0:v:0", "-vf", self._video_filter(),
                "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1"]
        if has_audio:
            args += ["-map", "0:a:0", "-ar", "44100", "-ac", "2", "-f", "s16le", f"pipe:{audio_w}"]
        dec = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(audio_w,), bufsize=0)
        os.close(audio_w)
        readers = [threading.Thread(target=self._read_video, args=(dec,), daemon=True),
                   threading.Thread(target=self._read_audio, args=(audio_r,), daemon=True)]
        for t in readers:
            t.start()
        self.room.log("info", f"推流端接入第 {self.segments} 段", mime=hello.get("mime"), audio=has_audio)
        self.room.broadcast("state")
        loop = asyncio.get_running_loop()
        reason = "舞台页断开"
        try:
            async for msg in ws:
                if msg.type.name == "BINARY":
                    await loop.run_in_executor(None, _write_all, dec.stdin.fileno(), msg.data)
                if dec.poll() is not None:
                    reason = "解码进程退出"
                    break
        except (ConnectionResetError, BrokenPipeError, OSError) as error:
            reason = f"解码写入失败：{error}"
        finally:
            try:
                dec.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            await loop.run_in_executor(None, _wait_or_kill, dec, 5)
            err = dec.stderr.read().decode("utf-8", "replace").strip()[-300:]
            self._ws = None
            if not ws.closed:
                await ws.close()
            self.room.log("info", f"推流端第 {self.segments} 段结束：{reason}", **({"error": err} if err else {}))
            self.room.broadcast("state")

    def _video_filter(self) -> str:
        """full: the avatar fills the 9:16 frame. box: like the reference Douyin room, a landscape box in
        the upper part of the screen cropped to face and shoulders, a little darker, on a dark page."""
        c = self.cfg
        W, H, fps = c["width"], c["height"], c["fps"]
        if c.get("layout") == "box":
            box_h = int(H * c.get("box_height", 0.47)) // 2 * 2
            top = int(H * c.get("box_top", 0.14)) // 2 * 2
            cy = c.get("box_crop_top", 0.10)
            ba = W / box_h
            # A portrait stream gives a band near the top (face and shoulders); a landscape stream from a
            # landscape base photo is cropped at the sides to the box shape and kept whole vertically.
            crop = (f"crop=w='if(gt(a,{ba:.4f}),ih*{ba:.4f},iw)':h='if(gt(a,{ba:.4f}),ih,iw/{ba:.4f})'"
                    f":x='(iw-ow)/2':y='if(gt(a,{ba:.4f}),0,min(ih*{cy},ih-oh))'")
            return (f"{crop},scale={W}:{box_h},"
                    f"eq=brightness=-0.05:saturation=0.9:contrast=1.04,"
                    f"pad={W}:{H}:0:{top}:color=0x0b141c,fps={fps}")
        return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps}"

    def _read_video(self, dec: subprocess.Popen) -> None:
        size = self.frame_size
        out = dec.stdout
        while True:
            frame = _read_exact(out, size)
            if frame is None:
                break
            with self._lock:
                self._frames.append(frame)

    def _read_audio(self, fd: int) -> None:
        try:
            while True:
                data = os.read(fd, 65536)
                if not data:
                    break
                with self._lock:
                    self._audio.extend(data)
        finally:
            os.close(fd)


def _blend(a: bytes, b: bytes, w: float) -> bytes:
    """Mix two raw yuv420p frames; w is the weight of b."""
    fa = np.frombuffer(a, dtype=np.uint8).astype(np.float32)
    fb = np.frombuffer(b, dtype=np.uint8).astype(np.float32)
    return (fa * (1 - w) + fb * w).astype(np.uint8).tobytes()


def _read_exact(stream, size: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _wait_or_kill(proc: subprocess.Popen, timeout: float) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _pipe_writer(q: queue.Queue, fd: int) -> None:
    try:
        while True:
            item = q.get()
            if item is None:
                break
            _write_all(fd, item)
    except (BrokenPipeError, OSError, ValueError):
        pass


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]
