// YouTube live chat -> live room danmaku, through the signed-in ego lite browser.
//   (echo 'globalThis.BRIDGE={video:"VIDEO_ID",minutes:12,space:19};'; cat tools/youtube_chat_bridge.js) | ego-browser nodejs
// ego-browser scripts do not see the caller's environment, so settings come from globalThis.BRIDGE.
// Reads the public chat popout every 2 s and posts new messages to /api/danmaku
// (Super Chats count as gifts). Nothing is ever typed into YouTube.
const CFG = globalThis.BRIDGE || {};
const SERVER = CFG.server || "http://127.0.0.1:28900";
const CHANNEL = CFG.channel;
const MINUTES = Number(CFG.minutes || 12);
const SPACE = CFG.space ? Number(CFG.space) : null;

const task = SPACE ? await taskSpace(SPACE) : await taskSpace("YouTube chat bridge");
const page = await task.newPage();
let videoId = CFG.video;
if (!videoId) {
  await page.goto(`https://www.youtube.com/channel/${CHANNEL}/live`);
  await page.waitForTimeout(5000);
  const url = await page.url();
  videoId = new URL(url).searchParams.get("v") || await page.evaluate(() => document.querySelector('link[rel="canonical"]')?.href?.split("v=")[1]);
}
if (!videoId) { console.log("找不到正在直播的视频"); process.exit(1); }
await page.goto(`https://www.youtube.com/live_chat?is_popout=1&v=${videoId}`);
await page.waitForTimeout(5000);
// Switch "Top chat" to "Live chat" so every message is visible.
await page.evaluate(() => {
  const btn = document.querySelector("yt-sort-filter-sub-menu-renderer #label, #view-selector #label");
  if (btn) btn.click();
});
await page.waitForTimeout(800);
await page.evaluate(() => {
  const items = [...document.querySelectorAll("tp-yt-paper-listbox a, yt-dropdown-menu a, tp-yt-paper-item")];
  const live = items.find(a => /实时聊天|Live chat/i.test(a.innerText));
  if (live) live.click();
});
console.log({ taskSpaceId: task.spaceId, page: page.label, videoId });

const read = () => page.evaluate(() => [...document.querySelectorAll("yt-live-chat-text-message-renderer, yt-live-chat-paid-message-renderer")].map(el => ({
  id: el.id,
  user: el.querySelector("#author-name")?.innerText?.trim() || "观众",
  text: el.querySelector("#message")?.innerText?.trim() || "",
  paid: el.tagName.toLowerCase().includes("paid") ? (el.querySelector("#purchase-amount")?.innerText || "1") : "",
})));
const seen = new Set((await read()).map(m => m.id));   // skip history from before the bridge started
const end = Date.now() + MINUTES * 60_000;
let forwarded = 0;
while (Date.now() < end) {
  await page.waitForTimeout(2000);
  let msgs;
  try { msgs = await read(); } catch { continue; }
  for (const m of msgs) {
    if (!m.id || seen.has(m.id)) continue;
    seen.add(m.id);
    const gift = m.paid ? (parseFloat(m.paid.replace(/[^\d.]/g, "")) || 1) : 0;
    try {
      const r = await fetch(`${SERVER}/api/danmaku`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user: m.user, text: m.text, gift, source: "youtube" }) }).then(x => x.json());
      forwarded++;
      console.log(new Date().toLocaleTimeString(), m.user, m.text, gift ? `[SC ${m.paid}]` : "", r.accepted ? `→ ${r.command.kind}` : `✗ ${r.category}`);
    } catch (e) { console.log("转发失败", e.message); }
  }
}
console.log(`桥接结束，共转发 ${forwarded} 条`);
await page.close();
