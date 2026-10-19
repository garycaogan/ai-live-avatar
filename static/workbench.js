/* AI 直播工作台 — 原生 JS，无构建、无外部依赖。接口约定见 WORKBENCH_API.md。 */
"use strict";
(() => {
  // ---------------------------------------------------------------- basics
  const $ = (id) => document.getElementById(id);
  const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const SVG_NS = "http://www.w3.org/2000/svg";
  const LS = {
    get(key) { try { return window.localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { window.localStorage.setItem(key, value); } catch { /* 隐私模式等 */ } },
  };
  const PROPS = new Set(["value", "checked", "disabled", "hidden", "selected", "muted", "controls", "playsInline", "required", "multiple"]);

  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    if (props) {
      for (const [key, val] of Object.entries(props)) {
        if (val == null || val === false) continue;
        if (key === "class") node.className = val;
        else if (key === "text") node.textContent = val;
        else if (key === "dataset") Object.assign(node.dataset, val);
        else if (key.startsWith("on") && typeof val === "function") node.addEventListener(key.slice(2), val);
        else if (PROPS.has(key)) node[key] = val;
        else node.setAttribute(key, val === true ? "" : String(val));
      }
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      node.append(kid instanceof Node ? kid : String(kid));
    }
    return node;
  }
  function icon(name, cls = "ico") {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", cls);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }
  const clear = (node) => { while (node.firstChild) node.firstChild.remove(); return node; };
  const enc = encodeURIComponent;

  function parseList(text) {
    const seen = new Set();
    return String(text || "").split(/[,，、;；\n]+/).map((s) => s.trim()).filter((s) => s && !seen.has(s) && seen.add(s));
  }
  function toDate(value) {
    if (value == null || value === "") return null;
    if (typeof value === "number") return new Date(value > 1e12 ? value : value * 1000);
    const n = Number(value);
    if (!Number.isNaN(n) && /^\d+(\.\d+)?$/.test(String(value))) return toDate(n);
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const pad = (n) => String(n).padStart(2, "0");
  function fmtClock(value) { const d = toDate(value); return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` : "-"; }
  function fmtWhen(value) {
    const d = toDate(value);
    if (!d) return "";
    const now = new Date();
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    return d.toDateString() === now.toDateString() ? `今天 ${hm}` : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  }
  function fmtDur(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }
  const money = (v) => `¥${(Number(v) || 0).toFixed(2)}`;
  const clip = (text, n) => { text = String(text || ""); return text.length > n ? `${text.slice(0, n)}…` : text; };

  // ---------------------------------------------------------------- constants
  const TABS = {
    live: { title: "直播间", desc: "选角色、启动数字人、看预览，再发几条弹幕测测她的反应" },
    output: { title: "推流", desc: "画面最后去哪：本地看、交给 OBS，或者内置推到 YouTube / 录文件" },
    profiles: { title: "角色", desc: "虚拟主播的名字、人设、音色和底图" },
    wardrobe: { title: "衣柜", desc: "角色的整套衣服。观众点换装时，用对应底图重开一场会话", ctx: true },
    scene: { title: "场景", desc: "长相不变换个场景，生成新的底图候选", ctx: true },
    actions: { title: "动作", desc: "没人说话时的待机小动作，和弹幕关键词触发的动作", ctx: true },
    voice: { title: "声音", desc: "给角色挑一个音色，或者用一段录音克隆新的", ctx: true },
    media: { title: "素材", desc: "生成图片和视频、上传文件，所有素材都收在素材库", ctx: true },
    settings: { title: "设置", desc: "密钥和生成服务是否就绪、接口状态，以及其它页面入口" },
  };
  const ACTIVE = new Set(["creating", "connecting", "waiting_stage", "live", "ending"]);
  const ON_AIR = new Set(["waiting_stage", "live"]);
  // 这几个状态下输出方式不能切（服务端也会回 409）
  const RUNNING = new Set(["creating", "connecting", "waiting_stage", "live"]);
  // 直播间统一说“启动 / 停止数字人”，不用服务端的“开播 / 下播”，免得和推流混在一起
  const RUN_LABEL = { idle: "未启动", creating: "正在创建", connecting: "正在连接", waiting_stage: "等画面入会", live: "运行中", ending: "正在停止", ended: "已停止", error: "出错了" };
  const OUT_MODES = { local: "只在本地看", obs: "用 OBS", builtin: "内置推流" };
  const STAGE_ROLE = { preview: "直播间的预览", obs: "OBS 里的数字人图层", broadcast: "推流舞台页" };
  const KIND = { chat: "聊天", action: "动作", garment: "换装", outfit: "整套换装", object: "道具", background: "换背景", reset: "复原", gift_thanks: "礼物答谢" };
  const CMD_STATUS = { ready: "待执行", preparing: "准备中", executing: "执行中", done: "已执行", failed: "失败", expired: "已过期" };
  const BC_STATE = { idle: ["未推流", ""], starting: ["正在启动", "tone-warn"], waiting: ["等画面", "tone-warn"], live: ["推流中", "tone-live"], ended: ["已结束", ""], error: ["出错", "tone-err"] };
  const JOB_STATUS = { queued: ["排队中", ""], running: ["生成中", "tone-live"], done: ["完成", "tone-ok"], failed: ["失败", "tone-err"] };
  const JOB_KIND = { image: "图片", video: "视频", scene: "场景底图", outfit: "衣服", upload: "上传" };
  const MEDIA_KIND = { image: "图片", video: "视频", audio: "音频" };
  const SAMPLES = ["你好呀", "你是真人吗", "换件衣服", "穿上卫衣", "戴上一个棒球帽", "拿出一只烤鸭", "背景换成海边日落", "跳个舞", "比个心", "把头发染成雾霾蓝", "换回来", "加我微信"];
  const SCENES = [
    ["深夜书桌台灯", "深夜的书桌前，一盏暖黄色台灯是唯一光源，背后是书架和被夜色压暗的窗户，像手机前置摄像头随手拍，画面偏暗、有一点颗粒感"],
    ["卧室床边串灯", "卧室床边，墙上挂着暖色小串灯，床头有抱枕和毛绒玩偶，昏暗柔和的氛围光，像手机随手拍的日常画面"],
    ["窗边清晨", "清晨的窗边，自然光从侧面照进来，浅色纱帘，桌上一杯咖啡，画面干净明亮，皮肤有真实质感"],
    ["咖啡店角落", "咖啡店靠墙的角落座位，背景是虚化的吧台和暖色吊灯，木桌上有一杯拿铁，环境生活化但不杂乱"],
    ["显示器冷光暗房", "关着灯的房间，只有电脑显示器的冷蓝光打在脸上，背景是模糊的键盘和桌面，暗调但真实，不要赛博朋克特效"],
  ];

  // ---------------------------------------------------------------- app state
  const S = {
    tab: "live",
    state: null,
    stateAt: 0,
    elapsed: 0,
    nextAt: null,
    connected: false,
    sigs: {},
    profiles: null,
    profilesErr: null,
    profilesCurrent: null,
    sel: null,
    detail: new Map(),
    detailErr: new Map(),
    inflight: new Map(),
    voices: null,
    voicesErr: null,
    actions: null,
    actionsErr: null,
    gallery: null,
    galleryErr: null,
    galKind: "all",
    galSearch: "",
    galSize: "big",
    jobs: null,
    jobsErr: null,
    jobsById: new Map(),
    jobMeta: new Map(),
    jobTimer: null,
    newProfile: null,
    pfFilledFor: null,
    pfDirty: false,
    wdEditing: null,
    prewarm: null,
    idleRows: null,
    idleFor: null,
    idleDirty: false,
    topicFor: null,
    topicDirty: false,
    kwRows: null,
    kwDirty: false,
    schedDirty: false,
    safeDirty: false,
    giRefs: [],
    regenNudge: null,
    clGallery: null,
    spent24h: null,
    gvImage: null,
    gvAudio: null,
    bcPending: false,
    outPending: false,
    obs: null,
    obsErr: null,
    liveProfile: null,
    logKeys: new Set(),
    previewOff: LS.get("wb.previewOff") === "1",
  };

  // ---------------------------------------------------------------- api
  class ApiError extends Error {
    constructor(message, status, path) { super(message); this.status = status; this.path = path; this.missing = status === 404 || status === 405; }
  }
  async function request(method, path, body, { form = false, raw = false } = {}) {
    const opts = { method, headers: { Accept: "application/json" } };
    if (form) opts.body = body;
    else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    let resp;
    try { resp = await fetch(path, opts); }
    catch { throw new ApiError("连不上本机服务，确认 server.py 还在运行", 0, path); }
    if (!resp.ok) {
      let msg = "";
      try { const data = await resp.clone().json(); msg = (data && data.error) || ""; } catch { /* 非 JSON 错误页 */ }
      const bare = path.split("?")[0];
      if (!msg && (resp.status === 404 || resp.status === 405)) msg = `这个接口还没上线：${method} ${bare}`;
      throw new ApiError(msg || `请求失败（HTTP ${resp.status}）`, resp.status, path);
    }
    if (raw) return resp;
    if (resp.status === 204) return {};
    return resp.json().catch(() => ({}));
  }
  const api = {
    get: (p) => request("GET", p),
    post: (p, b) => request("POST", p, b ?? {}),
    put: (p, b) => request("PUT", p, b ?? {}),
    patch: (p, b) => request("PATCH", p, b ?? {}),
    del: (p) => request("DELETE", p),
    form: (p, fd) => request("POST", p, fd, { form: true }),
  };

  // ---------------------------------------------------------------- feedback
  function toast(message, type = "ok", action = null) {
    const region = type === "error" ? $("toastAlert") : $("toastStatus");
    const node = el("div", { class: `toast toast-${type}` },
      icon(type === "error" ? "alert" : type === "info" ? "alert" : "check"),
      el("span", { text: message }),
      action ? el("button", { class: "toast-act", type: "button", text: action.label, onclick: () => { node.remove(); action.run(); } }) : null,
      el("button", { class: "toast-x", type: "button", "aria-label": "关闭提示", onclick: () => node.remove() }, icon("close")));
    region.append(node);
    while (region.children.length > 3) region.firstElementChild.remove();
    setTimeout(() => node.remove(), action ? 14000 : type === "error" ? 9000 : 4500);
  }
  function showMsg(container, text, { type = "error", retry, retryLabel = "重试" } = {}) {
    clear(container);
    if (!text) return;
    const box = el("div", { class: `alert alert-${type}`, role: type === "error" ? "alert" : "status" },
      icon("alert"), el("div", { class: "alert-body", text }));
    if (retry) box.append(el("button", { class: "btn btn-sm", type: "button", onclick: retry, text: retryLabel }));
    container.append(box);
  }
  function setFormMsg(node, text, type = "") {
    node.textContent = text || "";
    node.className = `form-msg${type ? ` form-msg-${type}` : ""}`;
  }
  function missingText(err, what) {
    if (err && err.missing) return `${what}的接口还没上线（${err.message.replace("这个接口还没上线：", "")}），后端接好后点重试。`;
    return `${what}加载失败：${err ? err.message : "未知错误"}`;
  }
  async function withBusy(btn, fn) {
    const wasDisabled = btn ? btn.disabled : false;
    if (btn) { btn.classList.add("is-busy"); btn.disabled = true; btn.setAttribute("aria-busy", "true"); }
    try { return await fn(); }
    finally {
      if (btn) { btn.classList.remove("is-busy"); btn.disabled = wasDisabled; btn.removeAttribute("aria-busy"); }
      applyGates();
    }
  }

  // ---------------------------------------------------------------- dialogs
  function openDialog(dialog) {
    const before = document.activeElement;
    dialog.returnValue = "";
    dialog.showModal();
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => {
        if (before && typeof before.focus === "function" && document.contains(before)) before.focus({ preventScroll: true });
        resolve(dialog.returnValue);
      }, { once: true });
    });
  }
  async function confirmBox({ title, message, ok = "确定", danger = false }) {
    $("confirmTitle").textContent = title;
    $("confirmMsg").textContent = message || "";
    const okBtn = $("confirmOk");
    okBtn.textContent = ok;
    okBtn.className = `btn ${danger ? "btn-danger-solid" : "btn-primary"}`;
    const wait = openDialog($("confirmDialog"));
    (danger ? $("confirmCancel") : okBtn).focus();
    return (await wait) === "ok";
  }

  // 素材库选择器
  const picker = { kind: "image", multiple: false, max: 0, chosen: new Set() };
  function renderPicker() {
    const grid = clear($("pickerGrid"));
    clear($("pickerMsg"));
    if (S.galleryErr && !S.gallery) {
      showMsg($("pickerMsg"), missingText(S.galleryErr, "素材库"), { retry: async () => { await loadGallery(); renderPicker(); } });
    }
    const items = (S.gallery || []).filter((it) => picker.kind === "any" || it.kind === picker.kind);
    if (S.gallery && !items.length) {
      grid.append(el("div", { class: "empty" }, el("b", { text: `素材库里还没有${MEDIA_KIND[picker.kind] || "素材"}` }), el("span", { text: "点下面的「上传新文件」，或者先去「素材」页生成。" })));
    }
    for (const it of items) {
      const pressed = picker.chosen.has(it.id);
      const media = it.kind === "audio"
        ? el("span", { class: "pick-audio" }, icon("voice"))
        : it.kind === "video"
          ? el("span", { class: "tile-media" }, el("video", { src: it.url, preload: "metadata", muted: true, playsInline: true }))
          : el("span", { class: "tile-media" }, el("img", { src: it.url, alt: "", loading: "lazy", decoding: "async", width: 160, height: 213 }));
      const btn = el("button", {
        class: "pick-btn", type: "button", "aria-pressed": String(pressed),
        "aria-label": `${MEDIA_KIND[it.kind] || "素材"}：${clip(it.prompt || it.source || it.id, 40)}`,
        onclick: () => {
          if (picker.chosen.has(it.id)) picker.chosen.delete(it.id);
          else {
            if (!picker.multiple) picker.chosen.clear();
            else if (picker.max && picker.chosen.size >= picker.max) { toast(`最多选 ${picker.max} 个`, "error"); return; }
            picker.chosen.add(it.id);
          }
          renderPicker();
          const again = qsa(".pick-btn", $("pickerGrid")).find((b) => b.dataset.id === it.id);
          if (again) again.focus();
        },
      }, media, el("span", { class: "check-badge" }, icon("check")), el("span", { class: "pick-cap", text: clip(it.prompt || it.source || fmtWhen(it.created) || it.id, 30) }));
      btn.dataset.id = it.id;
      grid.append(btn);
    }
    const n = picker.chosen.size;
    $("pickerCount").textContent = n ? `已选 ${n} 个${picker.max ? `（最多 ${picker.max}）` : ""}` : "未选择";
    $("pickerOk").disabled = n === 0;
  }
  async function pickGallery({ kind = "image", multiple = false, max = 0, title, hint = "", selected = [] } = {}) {
    picker.kind = kind; picker.multiple = multiple; picker.max = max; picker.chosen = new Set(selected);
    $("pickerTitle").textContent = title || `从素材库选${MEDIA_KIND[kind] || "素材"}`;
    $("pickerHint").textContent = hint;
    $("pickerFile").accept = kind === "any" ? "image/*,video/*,audio/*" : `${kind}/*`;
    clear($("pickerGrid")).append(el("div", { class: "empty" }, el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { text: "正在读取素材库…" })));
    $("pickerOk").disabled = true;
    const wait = openDialog($("pickerDialog"));
    if (!S.gallery) await loadGallery();
    renderPicker();
    const rv = await wait;
    if (rv !== "ok") return null;
    const byId = new Map((S.gallery || []).map((it) => [it.id, it]));
    return [...picker.chosen].map((id) => byId.get(id)).filter(Boolean);
  }

  // 加入衣柜
  async function outfitDialog({ item, hint }) {
    $("outfitDlgImg").src = item.url || "";
    $("outfitDlgImg").alt = "要加入衣柜的图片";
    $("outfitDlgHint").textContent = hint || "";
    $("outfitDlgName").value = "";
    $("outfitDlgAliases").value = "";
    $("outfitDlgErr").textContent = "";
    $("outfitDlgName").removeAttribute("aria-invalid");
    const wait = openDialog($("outfitDialog"));
    $("outfitDlgName").focus();
    if ((await wait) !== "ok") return null;
    return { name: $("outfitDlgName").value.trim(), aliases: parseList($("outfitDlgAliases").value) };
  }
  $("outfitDlgForm").addEventListener("submit", (e) => {
    if (!e.submitter || e.submitter.value !== "ok") return;
    if (!$("outfitDlgName").value.trim()) {
      e.preventDefault();
      $("outfitDlgErr").textContent = "先给这套衣服起个名字";
      $("outfitDlgName").setAttribute("aria-invalid", "true");
      $("outfitDlgName").focus();
    }
  });

  // Lightbox: the big picture with its name, details and actions; ←/→ browse the list it was opened from.
  const LB = { list: [], idx: 0 };
  function openPreview(item, list) {
    LB.list = list && list.length ? list : [item];
    LB.idx = Math.max(0, LB.list.findIndex((x) => x.id === item.id));
    showPreview();
    openDialog($("previewDialog")).then(() => { clear($("previewBody")); LB.list = []; });
  }
  function showPreview() {
    const it = LB.list[LB.idx];
    if (!it) return;
    const body = clear($("previewBody"));
    $("previewTitle").textContent = `${MEDIA_KIND[it.kind] || "素材"}预览`;
    $("previewCount").textContent = LB.list.length > 1 ? `${LB.idx + 1} / ${LB.list.length}，可以用 ← → 翻` : "";
    $("previewPrev").hidden = $("previewNext").hidden = LB.list.length < 2;
    if (it.kind === "video") body.append(el("video", { src: it.url, controls: true, preload: "metadata", playsInline: true, autoplay: true }));
    else if (it.kind === "audio") body.append(el("audio", { src: it.url, controls: true, preload: "metadata" }));
    else body.append(el("img", { src: it.url, alt: it.prompt || it.name || "预览图", decoding: "async" }));
    const inGallery = !!it.id && (S.gallery || []).some((x) => x.id === it.id);
    $("previewInfo").hidden = !inGallery;
    if (!inGallery) return;
    $("previewName").value = it.prompt || "";
    $("previewName").dataset.id = it.id;
    $("previewMeta").textContent = [it.model, it.cost_cny ? money(it.cost_cny) : "", it.created ? fmtWhen(it.created) : "", it.source].filter(Boolean).join(" · ");
    const full = it.full_prompt || "";
    $("previewPrompt").textContent = full || "（没有记录）";
    $("previewPrompt").parentElement.hidden = !full;
    const acts = clear($("previewActions"));
    if (it.kind === "image") {
      const who = S.sel ? `「${profileName(S.sel)}」` : "";
      const base = el("button", { class: "btn btn-primary", type: "button", text: `设为${who}底图` });
      base.disabled = !S.sel;
      base.addEventListener("click", () => setBase(it));
      const ward = el("button", { class: "btn", type: "button", text: `加入${who}衣柜` });
      ward.disabled = !S.sel;
      ward.addEventListener("click", () => addToWardrobe(it));
      acts.append(base, ward);
      if (!S.sel) acts.append(el("span", { class: "hint", text: "先在右上角选要编辑的角色" }));
    }
    acts.append(el("a", { class: "btn btn-ghost", href: it.url, target: "_blank", rel: "noopener", text: "打开原文件" }));
    const del = el("button", { class: "btn btn-ghost btn-danger-text", type: "button", text: "删除" });
    del.addEventListener("click", async () => {
      const before = S.gallery ? S.gallery.length : 0;
      await deleteGalleryItem(it, del);
      if (S.gallery && S.gallery.length < before) {
        LB.list.splice(LB.idx, 1);
        if (!LB.list.length) { $("previewDialog").close(); return; }
        LB.idx = Math.min(LB.idx, LB.list.length - 1);
        showPreview();
      }
    });
    acts.append(del);
  }
  function stepPreview(d) {
    if (LB.list.length < 2) return;
    LB.idx = (LB.idx + d + LB.list.length) % LB.list.length;
    showPreview();
  }
  $("previewPrev").addEventListener("click", () => stepPreview(-1));
  $("previewNext").addEventListener("click", () => stepPreview(1));
  $("previewDialog").addEventListener("keydown", (e) => {
    if (e.target === $("previewName")) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); stepPreview(-1); }
    if (e.key === "ArrowRight") { e.preventDefault(); stepPreview(1); }
  });
  async function saveItemName() {
    const input = $("previewName");
    const it = (S.gallery || []).find((x) => x.id === input.dataset.id);
    const label = input.value.trim();
    if (!it || !label || label === (it.prompt || "")) return;
    try {
      const r = await api.patch(`/api/media/gallery/${enc(it.id)}`, { label });
      Object.assign(it, r);
      S.sigs.gallery = null;
      if (S.tab === "media") renderGallery();
      toast("名字改好了");
    } catch (err) { toast(`改名失败：${err.message}`, "error"); }
  }
  $("previewName").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); saveItemName(); $("previewName").blur(); } });
  $("previewName").addEventListener("blur", saveItemName);

  // ---------------------------------------------------------------- navigation
  function showTab(name, { focus = false } = {}) {
    if (!TABS[name]) name = "live";
    S.tab = name;
    qsa("[data-panel]").forEach((panel) => { panel.hidden = panel.dataset.panel !== name; });
    qsa(".nav-item").forEach((btn) => {
      if (btn.dataset.tab === name) btn.setAttribute("aria-current", "page");
      else btn.removeAttribute("aria-current");
    });
    $("pageTitle").textContent = TABS[name].title;
    $("pageDesc").textContent = TABS[name].desc;
    $("mobileTab").textContent = TABS[name].title;
    document.title = `${TABS[name].title} · AI 直播工作台`;
    $("ctxWrap").hidden = !TABS[name].ctx;
    LS.set("wb.tab", name);
    if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
    closeMenu();
    if (focus) { window.scrollTo(0, 0); $("pageTitle").focus({ preventScroll: true }); }
    const enter = ENTER[name];
    if (enter) Promise.resolve(enter()).catch((err) => console.error(err));
  }
  function openMenu() {
    $("sidebar").classList.add("is-open");
    $("scrim").hidden = false;
    $("menuBtn").setAttribute("aria-expanded", "true");
    $("menuBtn").setAttribute("aria-label", "关闭导航菜单");
    const current = $("sidebar").querySelector('.nav-item[aria-current="page"]') || $("sidebar").querySelector(".nav-item");
    if (current) setTimeout(() => current.focus(), 30);
  }
  function closeMenu() {
    if (!$("sidebar").classList.contains("is-open")) return;
    $("sidebar").classList.remove("is-open");
    $("scrim").hidden = true;
    $("menuBtn").setAttribute("aria-expanded", "false");
    $("menuBtn").setAttribute("aria-label", "打开导航菜单");
  }
  qsa(".nav-item").forEach((btn) => btn.addEventListener("click", () => {
    const wasMenu = $("sidebar").classList.contains("is-open");
    showTab(btn.dataset.tab, { focus: true });
    if (wasMenu) $("menuBtn").focus({ preventScroll: true });
  }));
  $("menuBtn").addEventListener("click", () => ($("sidebar").classList.contains("is-open") ? closeMenu() : openMenu()));
  $("scrim").addEventListener("click", closeMenu);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("sidebar").classList.contains("is-open")) { closeMenu(); $("menuBtn").focus(); }
  });
  window.addEventListener("hashchange", () => {
    const name = location.hash.slice(1);
    if (TABS[name] && name !== S.tab) showTab(name);
  });

  // ---------------------------------------------------------------- live: state
  // 直播间只管“在本地把数字人跑起来、测它”；画面去哪（本地 / OBS / 内置推流）在推流页。
  function toneOf(status) {
    if (status === "live") return "tone-live";
    if (["creating", "connecting", "waiting_stage", "ending"].includes(status)) return "tone-warn";
    if (status === "error") return "tone-err";
    return "";
  }
  function setChip(node, label, tone, base = "chip") {
    const extra = node.classList.contains("chip-lg") ? " chip-lg" : "";
    node.className = `${base}${extra}${tone ? ` ${tone}` : ""}`;
    const lab = node.querySelector(".label");
    if (lab) lab.textContent = label; else node.textContent = label;
  }
  const isOnAir = () => !!S.state && ON_AIR.has(S.state.status);
  const isActive = () => !!S.state && ACTIVE.has(S.state.status);
  const humanRunning = () => !!S.state && RUNNING.has(S.state.status);
  // 服务端带了 output_mode 才算支持“输出方式”；旧服务没有这个字段，预览按本地处理。
  const outputSupported = () => !!S.state && OUT_MODES[S.state.output_mode] != null;
  const outputMode = () => (outputSupported() ? S.state.output_mode : "local");
  function runLabel(s) {
    const base = RUN_LABEL[s.status] || s.status_label || s.status || "—";
    return `${base}${s.mode === "dry_run" && ACTIVE.has(s.status) ? "（空跑）" : ""}`;
  }

  function applyGates() {
    const s = S.state;
    const active = isActive();
    const onAir = isOnAir();
    // 启动 / 停止是同一个位置上的两个按钮，运行中只露出“停止数字人”
    $("startBtn").hidden = active;
    $("dryBtn").hidden = active;
    $("stopBtn").hidden = !active;
    if ($("startBtn").getAttribute("aria-busy") !== "true") $("startBtn").disabled = !s || !s.has_api_key;
    if ($("dryBtn").getAttribute("aria-busy") !== "true") $("dryBtn").disabled = !s;
    if ($("stopBtn").getAttribute("aria-busy") !== "true") $("stopBtn").disabled = !s || s.status === "ending";
    $("liveProfile").disabled = active;
    $("liveModel").disabled = active;
    if ($("manualBtn").getAttribute("aria-busy") !== "true") $("manualBtn").disabled = !onAir;
    $("manualHint").hidden = onAir;
    $("manualSum").textContent = onAir ? "直接让她说一句话或做个动作" : "启动数字人后才能用";
    qsa("[data-needs-live]").forEach((btn) => {
      if (btn.getAttribute("aria-busy") === "true") return;
      btn.disabled = !onAir;
      btn.title = onAir ? "" : "启动数字人后才能试";
    });
  }

  function profileImage(key) {
    const p = (S.profiles || []).find((x) => x.key === key);
    return (p && p.image_url) || `/avatar.jpg?profile=${enc(key)}`;
  }

  function renderState(s) {
    S.state = s;
    S.stateAt = Date.now();
    S.elapsed = Number(s.elapsed) || 0;
    if (S.tab === "voice") renderVoice();  // live-switch buttons follow the show (cheap: renderVoice skips unchanged lists)
    S.nextAt = s.next_in != null ? Date.now() + s.next_in * 1000 : null;
    const tone = toneOf(s.status);
    const label = runLabel(s);
    setChip($("topStatus"), label, tone);
    setChip($("sessionStatus"), label, tone);
    setChip($("mobileStatus"), label, tone, "status-dot-chip");
    $("sideLive").className = `side-live${tone ? ` ${tone}` : ""}`;
    $("sideStatus").textContent = label;
    $("sideProfile").textContent = [s.avatar_name, s.outfit].filter(Boolean).join(" · ") || "—";

    renderSessionSub();
    $("keyHint").hidden = !!s.has_api_key;
    renderLiveProfiles(s);
    if (ACTIVE.has(s.status) && s.model) $("liveModel").value = s.model;
    applyGates();
    renderMetrics(s);
    renderRunOut();
    applyPreview();

    $("outfitName").textContent = s.outfit || "—";
    const visual = Object.entries(s.visual || {}).map(([k, v]) => `${KIND[k] || k}：${v}`).join("，");
    $("visualText").textContent = visual || "无";
    $("liveId").textContent = s.live_id || "—";
    renderSwap();
    if (s.subtitle) $("subtitleText").textContent = s.subtitle;

    const cur = s.current;
    const now = $("nowText");
    if (cur) {
      now.textContent = `@${cur.user}：${cur.text || KIND[cur.kind] || cur.kind}（${CMD_STATUS[cur.status] || cur.status}）`;
      now.classList.remove("is-idle");
    } else { now.textContent = "暂无"; now.classList.add("is-idle"); }
    renderCountdown();

    renderQueue(s.queue || []);
    renderHistory(s.history || []);
    renderFiltered(s.filtered || []);
    renderSchedule(s);
    if (S.tab === "output") renderOutput();
    if (S.tab === "settings") renderSettings();
    if (S.tab === "wardrobe") renderWardrobeBadges();
  }

  // 状态行下面那句“现在该干什么”
  function renderSessionSub() {
    const s = S.state;
    if (!s) return;
    const mode = outputMode();
    const dest = { local: "画面在下面的预览里看", obs: "画面交给 OBS，先把 OBS 里的浏览器源加好", builtin: "画面交给推流舞台页，先在推流页把它打开" }[mode];
    let sub = `选好角色，点「启动数字人」。${dest}。`;
    if (["ended", "error"].includes(s.status) && s.end_reason) sub = `上一场结束原因：${s.end_reason}。${dest}。`;
    else if (["creating", "connecting"].includes(s.status)) sub = "正在创建 Vidu 会话，通常十几秒。";
    else if (s.status === "waiting_stage") {
      if (mode === "obs") sub = "等 OBS 里的数字人图层入会，入会后才开始计费。OBS 没开或者没加浏览器源，会一直停在这一步。";
      else if (mode === "builtin") sub = "等推流舞台页入会，入会后才开始计费。还没打开的话，去推流页点「打开推流舞台页」。";
      else if (S.previewOff) sub = "预览断开了，没有地方接画面。点预览里的「重新连接预览」。";
      else sub = "等下面的预览入会，入会后才开始计费。";
    } else if (s.status === "live") sub = [s.avatar_name, s.model, s.mode === "dry_run" ? "空跑不计费" : ""].filter(Boolean).join(" · ");
    else if (s.status === "ending") sub = "正在挂断 Vidu 会话…";
    $("sessionSub").textContent = sub;
  }

  function renderRunOut() {
    const link = $("runOut");
    link.hidden = !outputSupported();
    if (link.hidden) return;
    $("runOutText").textContent = OUT_MODES[outputMode()];
    link.setAttribute("aria-label", `画面输出方式：${OUT_MODES[outputMode()]}，去推流页修改`);
  }

  function renderLiveProfiles(s) {
    const sel = $("liveProfile");
    const list = s.profiles || [];
    const sig = list.map((p) => `${p.key}|${p.name}|${p.note}`).join(",");
    if (S.sigs.liveProfiles !== sig) {
      S.sigs.liveProfiles = sig;
      clear(sel);
      for (const p of list) sel.append(el("option", { value: p.key, text: p.name + (p.note ? `（${p.note}）` : "") }));
    }
    const want = ACTIVE.has(s.status) ? s.profile : (S.liveProfile && list.some((p) => p.key === S.liveProfile) ? S.liveProfile : s.profile);
    if (want && sel.value !== want) sel.value = want;
    updateLiveThumb();
    if (!S.profiles && !S.profilesErr) loadProfiles();
  }
  function updateLiveThumb() {
    const key = $("liveProfile").value;
    if (!key) return;
    const src = profileImage(key);
    const img = $("liveThumb");
    if (img.dataset.src !== src) { img.dataset.src = src; img.src = src; }
    const opt = $("liveProfile").selectedOptions[0];
    img.alt = opt ? `${opt.textContent} 的底图` : "角色底图";
  }

  function renderMetrics(s) {
    const billing = s.billing || {};
    const credits = Math.max(Number(billing.credits_cost) || 0, 0);
    const est = Number(billing.estimate_credits) || 0;
    const usedCredits = credits || est;
    $("mElapsed").textContent = fmtDur(S.elapsed);
    $("sideElapsed").textContent = fmtDur(S.elapsed);
    $("mElapsedSub").textContent = s.max_session_minutes ? `/ ${s.max_session_minutes} 分钟` : "";
    $("mBilled").textContent = String(billing.billed_seconds ?? 0);
    $("mCredits").textContent = credits ? String(Math.round(credits * 100) / 100) : (est ? `≈${est}` : "0");
    const rmb = credits ? Number(billing.rmb) || credits * (s.credit_price_rmb || 0) : est * (s.credit_price_rmb || 0);
    $("mRmb").textContent = `${credits || !est ? "" : "≈"}${money(rmb)}`;
    $("sideCost").textContent = money(rmb);
    $("mRmbSub").textContent = s.credit_price_rmb ? `1 积分 ¥${s.credit_price_rmb}` : "—";
    setMeter($("meterTime"), $("meterTimeNum"), s.max_session_minutes ? S.elapsed / (s.max_session_minutes * 60) : 0);
    setMeter($("meterBudget"), $("meterBudgetNum"), s.budget_credits ? usedCredits / s.budget_credits : 0);
  }
  function setMeter(meter, numNode, ratio) {
    const pct = Math.max(0, Math.min(100, Math.round((ratio || 0) * 100)));
    meter.querySelector("i").style.width = `${pct}%`;
    meter.setAttribute("aria-valuenow", String(pct));
    meter.classList.toggle("is-warn", pct >= 70 && pct < 90);
    meter.classList.toggle("is-danger", pct >= 90);
    numNode.textContent = `${pct}%`;
  }
  function renderSwap() {
    const sw = S.state && S.state.swapping;
    $("swapBanner").hidden = !sw;
    if (!sw) return;
    const since = toDate(sw.since);
    const secs = since ? Math.max(0, Math.round((Date.now() - since.getTime()) / 1000)) : 0;
    $("swapText").textContent = `正在换上「${sw.to}」${sw.user ? `，@${sw.user} 点的` : ""}，已等 ${secs} 秒（通常 5 到 6 秒，没预上传的底图要一分钟左右）`;
  }
  function renderCountdown() {
    const n = S.nextAt ? Math.max(0, Math.round((S.nextAt - Date.now()) / 1000)) : null;
    $("nextIn").textContent = n == null ? "下一条 —" : `下一条 ${n} 秒后`;
  }

  // 排队、执行记录、已拦截都用同一种两行的条目：谁说的 + 标签，下面是内容
  function cmdItem(c, { time = false } = {}) {
    const head = el("div", { class: "cmd-head" },
      time ? el("time", { class: "cmd-time", text: c.executed_at ? fmtClock(c.executed_at) : "—" }) : el("span", { class: "cmd-time", text: `#${c.id}` }),
      el("span", { class: "cmd-user", text: `@${c.user || "观众"}` }),
      el("span", { class: "cmd-tags" },
        c.gift > 0 ? el("span", { class: "tag tag-warn", text: `礼物 ${c.gift}` }) : null,
        el("span", { class: "tag", text: KIND[c.kind] || c.kind || "-" }),
        el("span", { class: `st st-${c.status}`, text: CMD_STATUS[c.status] || c.status || "-" })));
    const extra = [c.item || c.asset, c.note].filter(Boolean).join(" · ");
    return el("li", { class: "cmd" }, head,
      el("p", { class: "cmd-text", text: c.text || KIND[c.kind] || "" }),
      extra ? el("p", { class: "cmd-note", text: extra }) : null);
  }
  function renderQueue(queue) {
    const sig = JSON.stringify(queue.map((c) => [c.id, c.status, c.gift, c.kind]));
    $("qCount").textContent = String(queue.length);
    if (S.sigs.queue === sig) return;
    S.sigs.queue = sig;
    clear($("queueList")).append(...queue.map((c) => cmdItem(c)));
    $("queueEmpty").hidden = queue.length > 0;
  }
  function renderHistory(history) {
    const sig = JSON.stringify(history.map((c) => [c.id, c.status, c.note, c.asset, c.item]));
    $("hCount").textContent = String(history.length);
    if (S.sigs.history === sig) return;
    S.sigs.history = sig;
    clear($("histList")).append(...history.map((c) => cmdItem(c, { time: true })));
    $("histEmpty").hidden = history.length > 0;
  }
  function renderFiltered(list) {
    const sig = JSON.stringify(list.map((x) => [x.t, x.user, x.text]));
    $("fCount").textContent = String(list.length);
    $("filtSum").textContent = list.length ? `最近一条：${clip(`${list[0].category}，${list[0].reason}`, 28)}` : "没有被拦截的弹幕";
    if (S.sigs.filtered === sig) return;
    S.sigs.filtered = sig;
    clear($("filtList")).append(...list.map((x) => el("li", { class: "cmd" },
      el("div", { class: "cmd-head" },
        x.t ? el("time", { class: "cmd-time", text: fmtClock(x.t) }) : null,
        el("span", { class: "cmd-user", text: `@${x.user || "观众"}` }),
        el("span", { class: "cmd-tags" }, el("span", { class: "st st-failed", text: x.category || "已拦截" }))),
      el("p", { class: "cmd-text", text: x.text || "" }),
      x.reason ? el("p", { class: "cmd-note", text: x.reason }) : null)));
    $("filtEmpty").hidden = list.length > 0;
  }

  function renderSchedule(s) {
    const active = document.activeElement;
    if (!S.schedDirty) {
      if (active !== $("interval")) $("interval").value = s.interval_seconds ?? "";
      if (active !== $("policy") && s.policy) $("policy").value = s.policy;
      if (active !== $("idlePrompt") && s.idle_prompt_after_seconds != null) $("idlePrompt").value = s.idle_prompt_after_seconds;
    }
    $("idleHint").textContent = s.idle_prompt_after_seconds != null
      ? "冷场开口填 0 表示关闭。"
      : "冷场开口的当前值没有随状态返回（服务端默认 45 秒），留空保存就不改；填 0 表示关闭。";
    const policyOpt = qsa("option", $("policy")).find((o) => o.value === s.policy);
    $("schedSum").textContent = [s.interval_seconds ? `每 ${s.interval_seconds} 秒执行一条` : "", policyOpt ? policyOpt.textContent : ""].filter(Boolean).join(" · ") || "多久执行一条弹幕、先挑哪条";
    if (!S.safeDirty) {
      if (active !== $("maxMinutes")) $("maxMinutes").value = s.max_session_minutes ?? "";
      if (active !== $("budgetCredits")) $("budgetCredits").value = s.budget_credits ?? "";
    }
    const price = s.credit_price_rmb || 0;
    const budget = Number($("budgetCredits").value) || s.budget_credits || 0;
    $("safeHint").textContent = price ? `预算约 ${money(budget * price)}。时长和预算先到哪个，就自动停止数字人。` : "时长和预算先到哪个，就自动停止数字人。";
    $("safeSum").textContent = [
      s.max_session_minutes ? `单场最长 ${s.max_session_minutes} 分钟` : "",
      s.budget_credits ? `预算 ${s.budget_credits} 积分${price ? `（约 ${money(s.budget_credits * price)}）` : ""}` : "",
    ].filter(Boolean).join(" · ") || "单场时长和预算上限";
  }

  // ---------------------------------------------------------------- live: log & subtitle
  function addLog(entry) {
    if (!entry) return;
    const key = `${entry.t}|${entry.message}`;
    if (S.logKeys.has(key)) return;
    S.logKeys.add(key);
    if (S.logKeys.size > 800) S.logKeys = new Set([...S.logKeys].slice(-400));
    const log = $("log");
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
    const level = entry.level || "info";
    const text = `${entry.message}${entry.data ? ` ${JSON.stringify(entry.data)}` : ""}`;
    log.append(el("div", { class: `log-line lv-${level}` },
      el("time", { text: fmtClock(entry.t) }),
      el("span", { class: "lv", text: level.toUpperCase().slice(0, 5) }),
      el("span", { class: "msg", text })));
    while (log.childElementCount > 400) log.firstElementChild.remove();
    if (nearBottom) log.scrollTop = log.scrollHeight;
    renderLogSum();
  }
  function renderLogSum() {
    const warns = qsa(".lv-warn, .lv-error", $("log")).length;
    $("logSum").textContent = warns ? `${warns} 条警告或错误` : "本机服务在做什么";
  }
  $("logWarnOnly").addEventListener("change", (e) => $("log").classList.toggle("warn-only", e.target.checked));
  $("logClear").addEventListener("click", () => { clear($("log")); renderLogSum(); });

  // 折叠区记住开合（只是个人习惯，存不下也没关系）
  qsa("details[data-fold]").forEach((box) => {
    const key = `wb.fold.${box.dataset.fold}`;
    if (LS.get(key) === "1") box.open = true;
    box.addEventListener("toggle", () => {
      LS.set(key, box.open ? "1" : "0");
      if (box.open && box.dataset.fold === "log") $("log").scrollTop = $("log").scrollHeight;
    });
  });

  // ---------------------------------------------------------------- live: SSE
  function connect() {
    let es;
    try { es = new EventSource("/api/events"); } catch { setTimeout(connect, 3000); return; }
    es.addEventListener("open", () => { S.connected = true; $("connAlert").hidden = true; });
    es.addEventListener("state", (e) => {
      try { renderState(JSON.parse(e.data)); } catch (err) { console.error("state 渲染失败", err); }
    });
    es.addEventListener("log", (e) => { try { addLog(JSON.parse(e.data)); } catch { /* 忽略坏数据 */ } });
    es.addEventListener("subtitle", (e) => {
      try { $("subtitleText").textContent = JSON.parse(e.data).text || "—"; } catch { /* 忽略 */ }
    });
    es.onerror = () => {
      es.close();
      S.connected = false;
      const alert = $("connAlert");
      alert.textContent = "和本机服务的实时连接断了，正在自动重连。页面上的数字可能不是最新的。";
      alert.hidden = false;
      setChip($("topStatus"), "连接断开", "tone-err");
      setTimeout(connect, 2000);
    };
  }

  // ---------------------------------------------------------------- live: preview
  // 预览只在“只在本地看”时入会；交给 OBS 或内置推流时整块换成说明，iframe 卸掉，不去抢入会名额。
  function applyPreview() {
    const frame = $("stageFrame");
    const s = S.state;
    const mode = outputMode();
    const local = !!s && mode === "local";
    const showFrame = local && !S.previewOff;
    if (showFrame) {
      if (frame.getAttribute("src") !== frame.dataset.src) frame.src = frame.dataset.src;
    } else if (frame.hasAttribute("src")) frame.removeAttribute("src");
    frame.hidden = !showFrame;
    $("stageOff").hidden = !(local && S.previewOff);
    $("previewToggle").hidden = !local;
    $("previewToggle").textContent = S.previewOff ? "连接预览" : "断开预览";
    const note = $("stageNote");
    note.hidden = !s || local;
    if (!note.hidden) {
      $("stageNoteTitle").textContent = mode === "obs" ? "画面在 OBS 里" : "画面在推流舞台页里";
      $("stageNoteText").textContent = mode === "obs"
        ? "现在的输出方式是「用 OBS」，数字人只在 OBS 的浏览器源里出画面，这里不占入会名额。"
        : "现在的输出方式是「内置推流」，画面交给推流舞台页，由本机服务录成文件或推到 YouTube。";
    }
  }
  function setPreview(off) {
    S.previewOff = off;
    LS.set("wb.previewOff", off ? "1" : "0");
    applyPreview();
    renderSessionSub();
    toast(off ? "预览已断开，不再占入会名额" : "预览已重新连接", "info");
  }
  $("previewToggle").addEventListener("click", () => setPreview(!S.previewOff));
  $("previewOn").addEventListener("click", () => setPreview(false));

  // ---------------------------------------------------------------- live: controls
  $("liveProfile").addEventListener("change", () => { S.liveProfile = $("liveProfile").value; updateLiveThumb(); });

  async function startSession(dry, btn) {
    $("sessionAlert").hidden = true;
    try {
      await withBusy(btn, () => api.post("/api/session/start", { model: $("liveModel").value, profile: $("liveProfile").value, dry_run: dry }));
      const who = { local: "下面的预览", obs: "OBS 里的数字人图层", builtin: "推流舞台页" }[outputMode()];
      toast(dry ? "空跑演示已开始，不调用 Vidu" : `正在启动数字人，${who}入会后开始计费`);
    } catch (err) {
      $("sessionAlert").textContent = `启动失败：${err.message}`;
      $("sessionAlert").hidden = false;
    }
  }
  $("startBtn").addEventListener("click", () => startSession(false, $("startBtn")));
  $("dryBtn").addEventListener("click", () => startSession(true, $("dryBtn")));
  $("stopBtn").addEventListener("click", async () => {
    const s = S.state || {};
    const dry = s.mode === "dry_run";
    const mode = outputMode();
    let message = dry ? "会结束这场空跑演示。" : "会挂断当前 Vidu 会话，停止计费。";
    if (mode === "builtin") message += "内置推流也会一起停。";
    if (mode === "obs") message += "OBS 里的数字人画面会变空，OBS 自己的推流不受影响，记得在 OBS 里停。";
    const ok = await confirmBox({ title: "停止数字人？", message, ok: "停止数字人", danger: true });
    if (!ok) return;
    try { await withBusy($("stopBtn"), () => api.post("/api/session/stop")); toast("数字人已停止"); }
    catch (err) { $("sessionAlert").textContent = `停止失败：${err.message}`; $("sessionAlert").hidden = false; }
  });
  $("nextBtn").addEventListener("click", async () => {
    try { await withBusy($("nextBtn"), () => api.post("/api/next")); toast("已让下一条立即执行", "info"); }
    catch (err) { toast(err.message, "error"); }
  });

  for (const text of SAMPLES) {
    $("dmSamples").append(el("button", { class: "chip-btn", type: "button", text, onclick: () => sendDanmaku(text) }));
  }
  function renderDmAdv() {
    const user = $("dmUser").value.trim() || "测试观众";
    const gift = Math.max(0, Number($("dmGift").value) || 0);
    $("dmAdvSum").textContent = `以「${user}」发送，${gift ? `送礼物 ${gift}` : "不送礼物"}`;
  }
  $("dmUser").addEventListener("input", renderDmAdv);
  $("dmGift").addEventListener("input", renderDmAdv);
  async function sendDanmaku(preset) {
    const input = $("dmText");
    const content = (preset ?? input.value).trim();
    const gift = Math.max(0, Number($("dmGift").value) || 0);
    if (!content && !gift) { setFormMsg($("dmMsg"), "写点弹幕内容，或者在下面填个礼物值", "warn"); input.focus(); return; }
    if (preset == null) input.value = "";
    try {
      const r = await api.post("/api/danmaku", { user: $("dmUser").value.trim() || "测试观众", text: content, gift, source: "workbench" });
      if (r.accepted === false) setFormMsg($("dmMsg"), `已拦截（${r.category}）：${r.reason}`, "warn");
      else {
        const cmd = r.command || {};
        setFormMsg($("dmMsg"), `已收到「${clip(content || `礼物 ${gift}`, 20)}」，识别为${KIND[cmd.kind] || cmd.kind || "弹幕"}${cmd.note ? `，${cmd.note}` : ""}`, "ok");
      }
    } catch (err) {
      setFormMsg($("dmMsg"), `发送失败：${err.message}`, "error");
      if (preset == null && !input.value) input.value = content;
    }
  }
  $("dmForm").addEventListener("submit", (e) => { e.preventDefault(); sendDanmaku(); });

  $("manualKind").addEventListener("change", () => {
    const noText = ["interrupt", "reset"].includes($("manualKind").value);
    $("manualText").disabled = noText;
  });
  $("manualForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const kind = $("manualKind").value;
    const content = $("manualText").value.trim();
    if (!["interrupt", "reset"].includes(kind) && !content) { toast("先写要发送的内容", "error"); $("manualText").focus(); return; }
    try {
      await withBusy($("manualBtn"), () => api.post("/api/manual", { kind, content }));
      $("manualText").value = "";
      toast("已发送");
    } catch (err) { toast(`发送失败：${err.message}`, "error"); }
  });

  function markDirty(formId, flagId, key) {
    $(formId).addEventListener("input", () => { S[key] = true; $(flagId).hidden = false; });
  }
  markDirty("schedForm", "schedDirty", "schedDirty");
  markDirty("safeForm", "safeDirty", "safeDirty");
  $("budgetCredits").addEventListener("input", () => S.state && renderSchedule(S.state));
  $("schedForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const interval = Number($("interval").value);
    if (!Number.isFinite(interval) || interval < 5 || interval > 120) { toast("执行间隔要在 5 到 120 秒之间", "error"); $("interval").focus(); return; }
    const body = { interval_seconds: Math.round(interval), policy: $("policy").value };
    const idle = $("idlePrompt").value.trim();
    if (idle !== "") {
      const n = Number(idle);
      if (!Number.isFinite(n) || n < 0) { toast("冷场开口秒数不能是负数", "error"); $("idlePrompt").focus(); return; }
      body.idle_prompt_after_seconds = Math.round(n);
    }
    try {
      await withBusy(e.submitter, () => api.post("/api/settings", body));
      S.schedDirty = false; $("schedDirty").hidden = true;
      toast("调度设置已保存");
    } catch (err) { toast(`保存失败：${err.message}`, "error"); }
  });
  $("safeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const minutes = Number($("maxMinutes").value), credits = Number($("budgetCredits").value);
    if (!(minutes >= 1) || !(credits >= 1)) { toast("时长和预算都要大于 0", "error"); return; }
    try {
      await withBusy(e.submitter, () => api.post("/api/safety", { max_session_minutes: Math.round(minutes), budget_credits: Math.round(credits) }));
      S.safeDirty = false; $("safeDirty").hidden = true;
      toast("费用上限已保存，本场立即生效");
    } catch (err) { toast(`保存失败：${err.message}`, "error"); }
  });

  // ---------------------------------------------------------------- 推流：输出方式
  function renderOutput() {
    const s = S.state;
    const supported = outputSupported();
    const mode = supported ? s.output_mode : null;
    const running = humanRunning();
    const msg = $("outMsg");
    if (!s) showMsg(msg, "正在连接本机服务…", { type: "info" });
    else if (!supported) showMsg(msg, "服务端还没更新：现在运行的 server.py 还不认识「输出方式」，切换先用不了。后端更新并重启后，刷新这一页。", { type: "warn", retry: () => location.reload(), retryLabel: "刷新" });
    else if (msg.firstChild) clear(msg);
    $("outLocked").hidden = !(supported && running);
    qsa('input[name="outMode"]').forEach((input) => {
      input.checked = input.value === mode;
      input.disabled = !supported || running;
      input.closest(".mode-card").classList.toggle("is-current", input.value === mode);
    });
    $("outModes").classList.toggle("is-locked", !supported || running);
    $("outModes").setAttribute("aria-busy", S.outPending ? "true" : "false");

    const role = $("outRole");
    role.hidden = !supported;
    if (supported) {
      const who = STAGE_ROLE[s.stage_role] || STAGE_ROLE[{ local: "preview", obs: "obs", builtin: "broadcast" }[mode]];
      const joined = running ? (s.stage_joined ? "已入会，有画面" : "还没入会") : "数字人没启动";
      clear(role).append(el("span", { class: "label-muted", text: "谁来接画面" }), el("b", { text: who }),
        el("span", { class: `chip${running ? (s.stage_joined ? " tone-live" : " tone-warn") : ""}` }, el("span", { class: "dot", "aria-hidden": "true" }), el("span", { class: "label", text: joined })));
    }

    $("outLocal").hidden = mode !== "local";
    $("outBuiltin").hidden = mode !== "builtin";
    // OBS 接法：选了 OBS 就展开成卡片；没选时收起放在下面，方便先看看
    const obsOpen = mode === "obs";
    $("outObs").hidden = !obsOpen;
    $("obsFold").hidden = obsOpen;
    const slot = obsOpen ? $("obsSlot") : $("obsFoldSlot");
    if ($("obsGuide").parentElement !== slot) slot.append($("obsGuide"));
    if (mode === "builtin") renderBroadcast(s.broadcast);
  }

  async function loadObs() {
    if (S.inflight.has("obs")) return S.inflight.get("obs");
    const job = (async () => {
      try { S.obs = await api.get("/api/obs"); S.obsErr = null; } catch (err) { S.obsErr = err; }
      renderObs();
    })();
    S.inflight.set("obs", job);
    try { await job; } finally { S.inflight.delete("obs"); }
  }
  const absUrl = (url) => { try { return new URL(url, location.origin).href; } catch { return String(url || ""); } };
  async function copyText(text, input) {
    try { await navigator.clipboard.writeText(text); return true; }
    catch {
      if (input) input.select();
      try { return document.execCommand("copy"); } catch { return false; }
    }
  }
  function renderObs() {
    const list = clear($("obsLayers"));
    clear($("obsMsg"));
    const d = S.obs;
    if (S.obsErr && !d) {
      const err = S.obsErr;
      showMsg($("obsMsg"), err.missing
        ? "服务端还没更新：还没有 /api/obs 这个接口，OBS 地址暂时拿不到。后端更新并重启后点重试。"
        : `OBS 图层信息没拿到：${err.message}`, { type: err.missing ? "warn" : "error", retry: loadObs });
    } else if (!d) {
      list.append(el("li", { class: "layer layer-loading" }, el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { text: "正在读取图层地址…" })));
    }
    const layers = (d && d.layers) || [];
    if (d && !layers.length) list.append(el("li", { class: "empty", text: "服务端没给图层地址" }));
    for (const layer of layers) {
      const url = absUrl(layer.url);
      const inputId = `obsUrl-${layer.key}`;
      const input = el("input", { id: inputId, readonly: true, value: url, spellcheck: "false", onfocus: (e) => e.target.select() });
      const btn = el("button", { class: "btn", type: "button", "aria-label": `复制${layer.name || ""}的地址` }, icon("copy"), el("span", { text: "复制", "aria-live": "polite" }));
      btn.addEventListener("click", async () => {
        const ok = await copyText(url, input);
        if (!ok) { toast("复制失败，手动选中地址复制", "error"); return; }
        btn.classList.add("is-copied");
        btn.lastElementChild.textContent = "已复制";
        clearTimeout(btn._t);
        btn._t = setTimeout(() => { btn.classList.remove("is-copied"); btn.lastElementChild.textContent = "复制"; }, 2000);
      });
      list.append(el("li", { class: `layer layer-${layer.key}` },
        el("div", { class: "layer-head" },
          el("b", { text: layer.name || layer.key }),
          layer.width && layer.height ? el("span", { class: "tag tag-muted", text: `宽 ${layer.width} × 高 ${layer.height}` }) : null,
          layer.key === "avatar" ? el("span", { class: "tag", text: "勾选通过 OBS 控制音频" }) : null),
        layer.note ? el("p", { class: "hint", text: layer.note }) : null,
        el("div", { class: "copy-row" },
          el("label", { class: "visually-hidden", for: inputId, text: `${layer.name || layer.key} 的地址` }),
          input, btn)));
    }
    $("obsLocal").hidden = !(d && d.same_machine_only);
    const tips = (d && d.tips) || [];
    $("obsTipsWrap").hidden = !tips.length;
    clear($("obsTips")).append(...tips.map((t) => el("li", { text: t })));
  }

  async function switchOutput(body, okText) {
    S.outPending = true;
    try {
      const r = await api.post("/api/output", body);
      if (S.state && r && typeof r === "object") {
        for (const k of ["output_mode", "stage_role", "broadcast"]) if (r[k] !== undefined) S.state[k] = r[k];
      }
      toast(okText);
      return true;
    } catch (err) {
      toast(err.status === 409 ? `现在不能切换：${err.message}`
        : err.missing ? "服务端还没更新，不能切换输出方式" : `切换失败：${err.message}`, "error");
      return false;
    } finally {
      S.outPending = false;
      if (S.state) { renderOutput(); renderRunOut(); applyPreview(); renderSessionSub(); }
    }
  }
  $("outModes").addEventListener("change", (e) => {
    const input = e.target;
    if (input.name !== "outMode") return;
    const mode = input.value;
    // 上一个切换还没回来就先不动，避免方向键连按发出一串请求
    if (S.outPending || !outputSupported() || mode === S.state.output_mode) { renderOutput(); return; }
    const tail = { local: "画面只在直播间的预览里", obs: "去 OBS 里加浏览器源吧", builtin: "选好目标，再打开推流舞台页" }[mode];
    switchOutput({ mode }, `输出方式改成「${OUT_MODES[mode]}」，${tail}`);
  });

  // ---------------------------------------------------------------- 推流：内置推流
  function renderBroadcast(b) {
    const card = $("outBuiltin");
    if (!b) {
      setChip($("bcState"), "没有推流信息", "");
      qsa("input", card).forEach((i) => { i.disabled = true; });
      return;
    }
    const [label, tone] = BC_STATE[b.state] || [b.state || "—", ""];
    setChip($("bcState"), label, tone);
    if (!S.bcPending) {
      qsa('input[name="bcTarget"]').forEach((i) => {
        i.checked = i.value === b.target;
        i.disabled = i.value === "youtube" && !b.youtube_key;
        i.closest("label").title = i.disabled ? "没有 YouTube 推流码（~/.deepsea/youtube/stream.key）" : "";
      });
      qsa('input[name="bcLayout"]').forEach((i) => { i.checked = i.value === (b.layout || "full"); i.disabled = false; });
    }
    $("ytHint").hidden = !!b.youtube_key;
    const st = b.stats || {};
    $("bcFps").textContent = st.fps != null ? `${st.fps} fps` : "—";
    $("bcBitrate").textContent = st.bitrate ? String(st.bitrate).replace("kbits/s", " kbit/s") : "—";
    $("bcSpeed").textContent = st.speed != null && st.speed !== "" ? `${String(st.speed).replace(/x$/, "")}×` : "—";
    $("bcSegments").textContent = b.segments != null ? String(b.segments) : "—";
    $("bcReceiving").textContent = b.receiving ? "正在接收" : "没有画面";
    $("bcOutput").hidden = !b.output;
    $("bcOutput").textContent = b.output ? `输出：${b.output}` : "";
    $("bcError").hidden = !b.error;
    $("bcError").textContent = b.error ? `推流出错：${b.error}` : "";
  }
  async function setBcTarget(target) {
    S.bcPending = true;
    qsa("#bcTarget input").forEach((i) => { i.disabled = true; });
    try {
      await switchOutput({ mode: "builtin", target }, target === "youtube" ? "改成推到 YouTube" : "改成录到文件");
    } finally {
      S.bcPending = false;
      if (S.state) renderBroadcast(S.state.broadcast);
    }
  }
  async function setBcLayout(layout) {
    S.bcPending = true;
    qsa("#bcLayout input").forEach((i) => { i.disabled = true; });
    try {
      const r = await api.post("/api/broadcast", { layout });
      if (S.state && S.state.broadcast && r && r.layout) S.state.broadcast.layout = r.layout;
      toast(`构图改成${layout === "box" ? "小框" : "全屏"}，从下一段画面生效`);
    } catch (err) { toast(`构图没改成：${err.message}`, "error"); }
    finally { S.bcPending = false; if (S.state) renderBroadcast(S.state.broadcast); }
  }
  $("bcTarget").addEventListener("change", (e) => setBcTarget(e.target.value));
  $("bcLayout").addEventListener("change", (e) => setBcLayout(e.target.value));
  $("bcOpen").addEventListener("click", () => {
    // 单独一个小窗口：后台标签页会被浏览器降频，画面容易卡
    const win = window.open("/stage?broadcast=1", "ds-broadcast-stage", "popup,width=480,height=880");
    if (win) { try { win.focus(); } catch { /* 跨窗口焦点被拦也没关系 */ } toast("推流舞台页已打开，推流期间别关它", "info"); }
    else toast("浏览器拦住了弹出窗口，允许本页弹窗后再点一次", "error");
  });

  // 1 秒节拍：已运行时长、倒计时、换装等待
  setInterval(() => {
    const s = S.state;
    if (!s) return;
    if (s.status === "live" || (s.status === "waiting_stage" && S.elapsed > 0)) {
      S.elapsed += 1;
      $("mElapsed").textContent = fmtDur(S.elapsed);
      $("sideElapsed").textContent = fmtDur(S.elapsed);
      if (s.max_session_minutes) setMeter($("meterTime"), $("meterTimeNum"), S.elapsed / (s.max_session_minutes * 60));
    }
    renderCountdown();
    if (s.swapping) renderSwap();
    if (S.prewarm && !S.prewarm.done && S.tab === "wardrobe") renderPrewarm();
  }, 1000);

  // ---------------------------------------------------------------- profiles data
  function profileList() {
    if (S.profiles) return S.profiles;
    return ((S.state && S.state.profiles) || []).map((p) => ({ ...p, image_url: `/avatar.jpg?profile=${enc(p.key)}`, fallback: true }));
  }
  function profileName(key) {
    const list = profileList();
    const p = list.find((x) => x.key === key);
    if (!p) return key || "";
    // Several test profiles share a display name; add the start of the note so they can be told apart.
    if (list.filter((x) => x.name === p.name).length < 2) return p.name;
    const hint = (p.note || "").split(/[，,。（(]/)[0].trim().slice(0, 10);
    return `${p.name} · ${hint || p.key}`;
  }
  async function loadProfiles() {
    if (S.inflight.has("profiles")) return S.inflight.get("profiles");
    const job = (async () => {
      try {
        const d = await api.get("/api/profiles");
        S.profiles = d.profiles || [];
        S.profilesCurrent = d.current || null;
        S.profilesErr = null;
      } catch (err) { S.profilesErr = err; }
      ensureSel();
      renderCtx();
      updateLiveThumb();
    })();
    S.inflight.set("profiles", job);
    try { await job; } finally { S.inflight.delete("profiles"); }
  }
  function ensureSel() {
    const list = profileList();
    if (!list.length) return;
    if (S.sel && list.some((p) => p.key === S.sel)) return;
    const stored = LS.get("wb.profile");
    S.sel = [stored, S.profilesCurrent, S.state && S.state.profile, list[0].key].find((k) => k && list.some((p) => p.key === k));
  }
  function renderCtx() {
    const sel = $("ctxProfile");
    const list = profileList();
    const sig = list.map((p) => `${p.key}|${p.name}|${p.note}`).join(",");
    if (S.sigs.ctx !== sig) {
      S.sigs.ctx = sig;
      clear(sel);
      for (const p of list) sel.append(el("option", { value: p.key, text: `${p.name}${p.note ? ` · ${clip(p.note, 14)}` : ""}` }));
    }
    if (S.sel) sel.value = S.sel;
  }
  async function loadDetail(key) {
    if (!key) return null;
    const tag = `detail:${key}`;
    if (S.inflight.has(tag)) return S.inflight.get(tag);
    const job = (async () => {
      try {
        const d = await api.get(`/api/profiles/${enc(key)}`);
        S.detail.set(key, d);
        S.detailErr.delete(key);
        return d;
      } catch (err) { S.detailErr.set(key, err); return null; }
    })();
    S.inflight.set(tag, job);
    try { return await job; } finally { S.inflight.delete(tag); }
  }
  async function hasUnsaved() {
    if (!S.pfDirty && !S.idleDirty && !S.topicDirty) return false;
    const ok = await confirmBox({ title: "放弃没保存的修改？", message: "当前角色有修改还没保存，切换后会丢掉。", ok: "放弃修改", danger: true });
    return !ok;
  }
  async function selectProfile(key) {
    if (!key || key === S.sel) { renderCtx(); return; }
    if (await hasUnsaved()) { renderCtx(); return; }
    S.sel = key;
    LS.set("wb.profile", key);
    S.newProfile = null;
    setPfDirty(false);
    setIdleDirty(false);
    S.idleFor = null;
    setTopicDirty(false);
    S.topicFor = null;
    S.pfFilledFor = null;
    S.wdEditing = null;
    renderCtx();
    renderCurrentTab();
    await loadDetail(key);
    renderCurrentTab();
  }
  $("ctxProfile").addEventListener("change", () => selectProfile($("ctxProfile").value));

  async function refreshProfile(key) {
    await Promise.all([loadProfiles(), loadDetail(key)]);
    renderCurrentTab();
  }
  function inUse(key) {
    const d = S.detail.get(key);
    return !!((d && d.in_use) || (isActive() && S.state.profile === key));
  }
  async function setBase(item, key = S.sel) {
    if (!key) { toast("先在上面选一个角色", "error"); return false; }
    $("baseDlgTitle").textContent = `设为「${profileName(key)}」的底图`;
    $("baseDlgImg").src = item.url || "";
    $("baseDlgImg").alt = "新底图";
    $("baseDlgName").value = "";
    $("baseDlgHint").textContent = `角色的长相和场景以底图为准，衣柜里的底图那套也会换成这张。${inUse(key) ? "她的数字人正在运行，下次启动或换装时生效。" : "下次启动数字人时生效。"}`;
    if ((await openDialog($("baseDialog"))) !== "ok") return false;
    const body = { gallery_id: item.id };
    const outfitName = $("baseDlgName").value.trim();
    if (outfitName) body.outfit_name = outfitName;
    try {
      const r = await api.post(`/api/profiles/${enc(key)}/base`, body);
      if (r && r.key === key) S.detail.set(key, r);
      const d = S.detail.get(key);
      const others = ((d && d.outfits) || []).filter((o) => !o.base).length;
      if (others) {
        S.regenNudge = key;
        toast(`已设为「${profileName(key)}」的底图。衣柜里另外 ${others} 套还是旧场景，建议按新底图重画`, "ok",
          { label: "去重画", run: () => { showTab("wardrobe", { focus: true }); openRegen(); } });
      } else toast(`已设为「${profileName(key)}」的底图`);
      await refreshProfile(key);
      return true;
    } catch (err) { toast(`设置底图失败：${err.message}`, "error"); return false; }
  }
  async function addToWardrobe(item, key = S.sel) {
    if (!key) { toast("先在上面选一个角色", "error"); return; }
    const res = await outfitDialog({ item, hint: `加到「${profileName(key)}」的衣柜。图里最好是同一个人、同一个场景，只换了衣服。` });
    if (!res) return;
    try {
      await api.post(`/api/profiles/${enc(key)}/outfits`, { name: res.name, aliases: res.aliases, gallery_id: item.id });
      toast(`「${res.name}」已加入衣柜`);
      await refreshProfile(key);
    } catch (err) { toast(`加入衣柜失败：${err.message}`, "error"); }
  }

  // ---------------------------------------------------------------- 角色
  function setPfDirty(v) { S.pfDirty = v; $("pfDirty").hidden = !v; }
  $("profileForm").addEventListener("input", () => setPfDirty(true));

  function renderProfiles() {
    const list = profileList();
    const err = S.profilesErr;
    $("profilesNote").textContent = list.length ? `共 ${list.length} 个角色，点卡片编辑` : (err ? "" : "加载中…");
    if (err) {
      showMsg($("profilesMsg"), err.missing
        ? "角色接口还没上线（GET /api/profiles），下面先显示直播间里的角色，暂时不能编辑。后端接好后点重试。"
        : `角色列表加载失败：${err.message}`, { type: err.missing ? "warn" : "error", retry: () => ENTER.profiles() });
    } else clear($("profilesMsg"));
    $("newProfileBtn").disabled = !!err;

    const liveKey = S.state && S.state.profile;
    const sig = JSON.stringify([list.map((p) => [p.key, p.name, p.note, p.voice, p.image_url, p.outfits_count]), S.sel, liveKey, isOnAir(), !!S.newProfile]);
    if (S.sigs.profileGrid !== sig) {
      S.sigs.profileGrid = sig;
      const grid = clear($("profileGrid"));
      if (!list.length && !err) grid.append(el("div", { class: "empty" }, el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { text: "正在读取角色…" })));
      for (const p of list) {
        const selected = !S.newProfile && p.key === S.sel;
        const badges = el("div", { class: "tile-badges" });
        if (p.key === liveKey) badges.append(el("span", { class: `tag ${isOnAir() ? "tag-solid" : "tag-muted"}`, text: isOnAir() ? "直播中" : "直播间默认" }));
        const meta = el("div", { class: "tile-meta" });
        if (p.voice) meta.append(el("span", { class: "tag", text: `音色 ${clip(p.voice, 14)}` }));
        else if (!p.fallback) meta.append(el("span", { class: "tag tag-muted", text: "默认音色" }));
        if (p.outfits_count != null) meta.append(el("span", { class: "tag tag-muted", text: `${p.outfits_count} 套衣服` }));
        grid.append(el("article", { class: `tile${selected ? " is-selected" : ""}` },
          el("button", { class: "tile-select", type: "button", "aria-pressed": String(selected), "aria-label": `编辑角色 ${p.name}${p.note ? `，${p.note}` : ""}`, onclick: () => selectProfile(p.key).then(() => { if (window.matchMedia("(max-width: 1200px)").matches) $("profileEditor").scrollIntoView({ behavior: "smooth", block: "start" }); }) }),
          el("div", { class: "tile-media" }, el("img", { src: p.image_url, alt: "", loading: "lazy", decoding: "async", width: 240, height: 320 }), badges),
          el("div", { class: "tile-body" },
            el("div", { class: "tile-title", text: p.name }),
            el("p", { class: "tile-text", text: p.note || "没有备注" }),
            meta)));
      }
    }
    renderEditor();
  }

  function voiceOptions(select, current) {
    const sig = JSON.stringify([S.voices, current]);
    if (select.dataset.sig === sig) { select.value = current || ""; return; }
    select.dataset.sig = sig;
    clear(select);
    select.append(el("option", { value: "", text: "默认音色（不指定）" }));
    const known = new Set();
    if (S.voices) {
      const lib = el("optgroup", { label: "音色库" });
      for (const v of S.voices.library || []) { known.add(v.voice); lib.append(el("option", { value: v.voice, text: `${v.name}（实播活人感 ${v.live_score}）` })); }
      if (lib.children.length) select.append(lib);
      const sys = el("optgroup", { label: (S.voices.library || []).length ? "旧的系统音色" : "系统音色" });
      for (const v of S.voices.system || []) { known.add(v.voice); sys.append(el("option", { value: v.voice, text: v.name && v.name !== v.voice ? `${v.name}（${v.voice}）` : v.voice })); }
      const cl = el("optgroup", { label: (S.voices.library || []).length ? "旧的克隆音色" : "克隆音色" });
      for (const v of S.voices.cloned || []) { known.add(v.voice); cl.append(el("option", { value: v.voice, text: v.voice })); }
      if (sys.children.length) select.append(sys);
      if (cl.children.length) select.append(cl);
    }
    if (current && !known.has(current)) select.append(el("option", { value: current, text: `${current}（当前）` }));
    select.value = current || "";
  }

  function renderEditor() {
    const creating = !!S.newProfile;
    const form = $("profileForm");
    $("pfKeyWrap").hidden = !creating;
    $("pfBaseOutfitWrap").hidden = !creating;
    $("pfInUse").hidden = creating || !S.sel || !inUse(S.sel);
    clear($("editorMsg"));
    if (creating) {
      $("editorTitle").textContent = "新建角色";
      $("editorSub").textContent = "先从素材库选一张底图，再写名字和人设";
      const base = S.newProfile.base;
      $("pfImage").src = base ? base.url : "";
      $("pfImage").alt = base ? "新角色的底图" : "";
      $("pfImage").style.visibility = base ? "visible" : "hidden";
      $("pfImageHint").textContent = base ? "底图已选好" : "还没选底图：单人、半身、正脸看镜头、背景干净";
      $("pfPickBase").textContent = base ? "换一张" : "从素材库选底图";
      $("pfReset").textContent = "取消新建";
      $("pfSave").textContent = "创建角色";
      qsa("input, select, textarea, button", form).forEach((n) => { n.disabled = false; });
      voiceOptions($("pfVoice"), $("pfVoice").value);
      return;
    }
    $("pfReset").textContent = "还原";
    $("pfSave").textContent = "保存角色";
    $("pfPickBase").textContent = "从素材库换底图";
    $("pfImage").style.visibility = "visible";
    const key = S.sel;
    const d = key ? S.detail.get(key) : null;
    const err = key ? S.detailErr.get(key) : null;
    const fallback = !!S.profilesErr;
    $("editorTitle").textContent = key ? `编辑：${profileName(key)}` : "编辑角色";
    $("editorSub").textContent = "人设和开场白改完，下次启动数字人时生效";
    const lock = !d;
    qsa("input, select, textarea, button", form).forEach((n) => { n.disabled = lock; });
    if (!d) {
      if (err || fallback) showMsg($("editorMsg"), missingText(err || S.profilesErr, "角色详情"), { type: (err || S.profilesErr).missing ? "warn" : "error", retry: async () => { await refreshProfile(key); } });
      else if (key) showMsg($("editorMsg"), "正在读取角色详情…", { type: "info" });
      $("pfImage").src = key ? profileImage(key) : "";
      $("pfImage").alt = key ? `${profileName(key)} 的底图` : "";
      return;
    }
    if (S.pfFilledFor !== key || !S.pfDirty) {
      if (S.pfFilledFor !== key || !S.pfDirty) {
        $("pfName").value = d.name || "";
        $("pfNote").value = d.note || "";
        $("pfDeskDrink").value = (d.desk_items || {}).drink || "";
        $("pfDeskEat").value = (d.desk_items || {}).eat || "";
        $("pfGreeting").value = d.greeting_instruction || "";
        $("pfPersona").value = d.persona_text || "";
        voiceOptions($("pfVoice"), d.voice || "");
        S.pfFilledFor = key;
        setPfDirty(false);
      }
    }
    const src = d.image_url || profileImage(key);
    if ($("pfImage").getAttribute("src") !== src) $("pfImage").src = src;
    $("pfImage").alt = `${d.name || key} 的底图`;
    $("pfImageHint").textContent = "角色的长相、穿着和场景都以底图为准";
  }

  $("newProfileBtn").addEventListener("click", async () => {
    if (await hasUnsaved()) return;
    S.newProfile = { base: null };
    S.pfFilledFor = null;
    ["pfKey", "pfName", "pfNote", "pfDeskDrink", "pfDeskEat", "pfGreeting", "pfPersona", "pfBaseOutfit"].forEach((id) => { $(id).value = ""; });
    $("pfGreeting").value = "笑着跟刚进直播间的人打个招呼，随口说一句可以发弹幕让你换衣服、拿东西";
    voiceOptions($("pfVoice"), "");
    setPfDirty(false);
    S.sigs.profileGrid = null;
    renderProfiles();
    if (window.matchMedia("(max-width: 1200px)").matches) $("profileEditor").scrollIntoView({ behavior: "smooth", block: "start" });
    const picked = await pickGallery({ kind: "image", title: "给新角色选一张底图", hint: "底图决定她的长相和场景：单人、半身、正脸看镜头、像手机实拍的暗暖光效果最好。" });
    if (picked && picked[0] && S.newProfile) { S.newProfile.base = picked[0]; setPfDirty(true); renderEditor(); }
    $("pfKey").focus();
  });
  $("pfPickBase").addEventListener("click", async () => {
    if (S.newProfile) {
      const picked = await pickGallery({ kind: "image", title: "给新角色选一张底图", selected: S.newProfile.base ? [S.newProfile.base.id] : [] });
      if (picked && picked[0] && S.newProfile) { S.newProfile.base = picked[0]; setPfDirty(true); renderEditor(); }
      return;
    }
    if (!S.sel) return;
    const picked = await pickGallery({ kind: "image", title: `给「${profileName(S.sel)}」换底图` });
    if (picked && picked[0]) await setBase(picked[0], S.sel);
  });
  $("pfReset").addEventListener("click", () => {
    if (S.newProfile) { S.newProfile = null; setPfDirty(false); S.pfFilledFor = null; S.sigs.profileGrid = null; renderProfiles(); return; }
    S.pfFilledFor = null; setPfDirty(false); renderEditor();
    toast("已还原成保存过的内容", "info");
  });
  $("profileForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const creating = !!S.newProfile;
    const form = {
      name: $("pfName").value.trim(),
      note: $("pfNote").value.trim(),
      voice: $("pfVoice").value,
      greeting_instruction: $("pfGreeting").value.trim(),
      persona_text: $("pfPersona").value.trim(),
    };
    const desk = { drink: $("pfDeskDrink").value.trim(), eat: $("pfDeskEat").value.trim() };
    let key = S.sel;
    const fail = (msg, focusId) => { showMsg($("editorMsg"), msg); if (focusId) $(focusId).focus(); };
    if (!form.name) return fail("名字不能为空", "pfName");
    if (form.persona_text && form.persona_text.length < 20) return fail("人设太短了，至少写 20 个字", "pfPersona");
    let body;
    if (creating) {
      key = $("pfKey").value.trim();
      if (!/^[a-z][a-z0-9_]{1,31}$/.test(key)) return fail("角色标识要小写字母开头，只用小写字母、数字和下划线，2 到 32 位", "pfKey");
      if (profileList().some((p) => p.key === key)) return fail(`已经有叫 ${key} 的角色了，换一个标识`, "pfKey");
      if (!S.newProfile.base) return fail("新角色必须先从素材库选一张底图", "pfPickBase");
      body = { name: form.name, base_gallery_id: S.newProfile.base.id };
      const baseOutfit = $("pfBaseOutfit").value.trim();
      if (baseOutfit) body.base_outfit_name = baseOutfit;
      for (const k of ["note", "voice", "greeting_instruction", "persona_text"]) if (form[k]) body[k] = form[k];
      if (desk.drink || desk.eat) body.desk_items = desk;
    } else {
      const d = S.detail.get(key);
      if (!key || !d) return;
      if (!form.persona_text && (d.persona_text || "").trim()) return fail("人设不能清空，至少写 20 个字", "pfPersona");
      body = {};
      for (const k of Object.keys(form)) if (form[k] !== String(d[k] ?? "").trim()) body[k] = form[k];
      const was = d.desk_items || {};
      if (desk.drink !== (was.drink || "") || desk.eat !== (was.eat || "")) body.desk_items = desk;
      if (!Object.keys(body).length) { setPfDirty(false); toast("没有需要保存的修改", "info"); return; }
    }
    clear($("editorMsg"));
    try {
      const r = await withBusy($("pfSave"), () => api.put(`/api/profiles/${enc(key)}`, body));
      if (r && r.key === key) S.detail.set(key, r);
      setPfDirty(false);
      S.newProfile = null;
      S.pfFilledFor = null;
      S.sel = key;
      LS.set("wb.profile", key);
      toast(creating ? `角色「${body.name}」已创建` : "已保存，人设和开场白下次启动数字人时生效");
      await refreshProfile(key);
      S.sigs.profileGrid = null;
      renderCurrentTab();
    } catch (err) { showMsg($("editorMsg"), `${creating ? "创建" : "保存"}失败：${err.message}`); }
  });

  // ---------------------------------------------------------------- 衣柜
  function renderWardrobe() {
    const key = S.sel;
    const d = key ? S.detail.get(key) : null;
    const err = key ? S.detailErr.get(key) : null;
    const outfits = (d && d.outfits) || [];
    $("wdSub").textContent = d ? `${d.name || profileName(key)} · ${outfits.length} 套衣服` : key ? profileName(key) : "—";
    if (err) showMsg($("wdMsg"), missingText(err, "衣柜"), { type: err.missing ? "warn" : "error", retry: () => refreshProfile(key) });
    else clear($("wdMsg"));
    $("wdFromGallery").disabled = !d;
    if ($("wdPrewarm").getAttribute("aria-busy") !== "true") $("wdPrewarm").disabled = !d || !outfits.length;
    if ($("wdRegen").getAttribute("aria-busy") !== "true") $("wdRegen").disabled = !d || !outfits.some((o) => !o.base);
    $("wdInUse").hidden = !key || !inUse(key);
    $("wdNudge").hidden = !d || S.regenNudge !== key || !outfits.some((o) => !o.base);
    renderPrewarm();
    renderOutfitGrid(d, outfits, err);
    renderJobList($("wdJobs"), (j) => jobType(j) === "outfit", "还没有生成任务。右上方填好描述就能生成一套新衣服。");
  }
  function renderWardrobeBadges() {
    // 只在直播状态里的当前造型变化时重画
    const sig = `${S.state && S.state.profile}|${S.state && S.state.outfit}`;
    if (S.sigs.wdLive !== sig) { S.sigs.wdLive = sig; S.sigs.outfits = null; renderWardrobe(); }
  }
  function renderOutfitGrid(d, outfits, err) {
    const liveOutfit = (d && d.current_outfit) || (S.state && S.state.profile === S.sel ? S.state.outfit : null);
    const sig = JSON.stringify([S.sel, outfits, S.wdEditing, liveOutfit, !!d, !!err]);
    if (S.sigs.outfits === sig) return;
    S.sigs.outfits = sig;
    const grid = clear($("outfitGrid"));
    if (!d) {
      if (!err) grid.append(el("div", { class: "empty" }, el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { text: "正在读取衣柜…" })));
      return;
    }
    if (!outfits.length) {
      grid.append(el("div", { class: "empty" }, el("b", { text: "衣柜还是空的" }), el("span", { text: "从素材库加一张同人同场景的图，或者在右边生成一套。" })));
      return;
    }
    for (const o of outfits) {
      const badges = el("div", { class: "tile-badges" });
      if (o.base) badges.append(el("span", { class: "tag tag-solid", text: "底图" }));
      if (o.generated) badges.append(el("span", { class: "tag tag-muted", text: "AI 生成" }));
      if (liveOutfit && liveOutfit === o.name) badges.append(el("span", { class: "tag tag-muted", text: "正在穿" }));
      const media = el("div", { class: "tile-media" },
        el("img", { src: o.image_url, alt: `${o.name}`, loading: "lazy", decoding: "async", width: 240, height: 320 }),
        badges,
        el("button", { class: "tile-zoom", type: "button", "aria-label": `查看「${o.name}」大图`, onclick: () => openPreview({ kind: "image", url: o.image_url, name: o.name }) }, icon("eye")));
      if (S.wdEditing === o.name) {
        const nameInput = el("input", { value: o.name, maxlength: 20, "aria-label": "衣服名字", autocomplete: "off" });
        const aliasInput = el("textarea", { rows: 2, "aria-label": "别名，逗号分开", maxlength: 200 });
        aliasInput.value = (o.aliases || []).join("，");
        const descInput = el("textarea", { rows: 2, maxlength: 120 });
        descInput.value = o.description || "";
        const save = el("button", { class: "btn btn-sm btn-primary", type: "button", text: "保存" });
        save.addEventListener("click", () => saveOutfit(o, nameInput.value.trim(), parseList(aliasInput.value), descInput.value.trim(), save));
        nameInput.maxLength = 12;
        grid.append(el("article", { class: "tile is-selected" }, media,
          el("div", { class: "inline-edit" },
            el("label", { class: "field" }, "名字", nameInput),
            el("label", { class: "field" }, "别名（逗号分开）", aliasInput),
            el("label", { class: "field" }, "描述（重画时用）", descInput),
            el("div", { class: "btn-row" },
              el("button", { class: "btn btn-sm", type: "button", text: "取消", onclick: () => { S.wdEditing = null; S.sigs.outfits = null; renderWardrobe(); } }),
              save))));
        setTimeout(() => nameInput.focus(), 0);
        continue;
      }
      const aliases = el("div", { class: "alias-list" });
      const list = o.aliases || [];
      list.slice(0, 6).forEach((a) => aliases.append(el("span", { class: "tag tag-muted", text: a })));
      if (list.length > 6) aliases.append(el("span", { class: "tag tag-muted", text: `+${list.length - 6}`, title: list.slice(6).join("，") }));
      if (!list.length) aliases.append(el("span", { class: "hint", text: "没有别名" }));
      const del = el("button", { class: "btn btn-sm btn-danger", type: "button", "aria-label": `删除「${o.name}」` }, icon("trash", "ico ico-sm"), "删除");
      if (o.base) { del.disabled = true; del.title = "底图套装跟着角色底图走，要换去「角色」页换底图"; }
      else del.addEventListener("click", () => deleteOutfit(o, del));
      grid.append(el("article", { class: "tile" }, media,
        el("div", { class: "tile-body" }, el("div", { class: "tile-title", text: o.name, title: o.name }),
          o.description ? el("p", { class: "tile-desc", text: o.description, title: o.description }) : null, aliases),
        el("div", { class: "tile-actions" },
          el("button", { class: "btn btn-sm", type: "button", "aria-label": `编辑「${o.name}」`, onclick: () => { S.wdEditing = o.name; renderWardrobe(); } }, icon("edit", "ico ico-sm"), "编辑"),
          del)));
    }
  }
  async function saveOutfit(o, newName, aliases, description, btn) {
    if (!newName) { toast("名字不能为空", "error"); return; }
    const body = { aliases };
    if (newName !== o.name) body.new_name = newName;
    if (description !== (o.description || "")) body.description = description;
    try {
      await withBusy(btn, () => api.patch(`/api/profiles/${enc(S.sel)}/outfits/${enc(o.name)}`, body));
      S.wdEditing = null;
      toast("衣服信息已保存");
      await refreshProfile(S.sel);
    } catch (err) { toast(`保存失败：${err.message}`, "error"); }
  }
  async function deleteOutfit(o, btn) {
    const ok = await confirmBox({ title: `删除「${o.name}」？`, message: "这套衣服会从衣柜移除。观众之后再点这套，会现场重新生成（要等一会儿）。", ok: "删除", danger: true });
    if (!ok) return;
    try {
      await withBusy(btn, () => api.del(`/api/profiles/${enc(S.sel)}/outfits/${enc(o.name)}`));
      toast(`「${o.name}」已从衣柜删除`);
      await refreshProfile(S.sel);
    } catch (err) { toast(`删除失败：${err.message}`, "error"); }
  }
  $("wdFromGallery").addEventListener("click", async () => {
    const picked = await pickGallery({ kind: "image", title: "从素材库挑一张加进衣柜", hint: "最好是同一个人、同一个场景、只换了衣服的图，换装时才不会跳脸。" });
    if (picked && picked[0]) await addToWardrobe(picked[0], S.sel);
  });
  function renderPrewarm() {
    const box = $("wdPrewarmMsg");
    const p = S.prewarm;
    if (!p || p.key !== S.sel) { box.hidden = true; return; }
    box.hidden = false;
    clear(box);
    box.className = `progress-note${p.error ? " is-error" : p.done ? " is-done" : ""}`;
    if (!p.done) {
      const secs = Math.round((Date.now() - p.started) / 1000);
      box.append(el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { text: `正在把 ${p.count} 张衣柜底图预上传到 Vidu，通常要 40 秒左右，已等 ${secs} 秒。可以先去别的页面。` }));
    } else box.append(icon(p.error ? "alert" : "check"), el("span", { text: p.text }));
  }
  $("wdPrewarm").addEventListener("click", async () => {
    const key = S.sel;
    const d = S.detail.get(key);
    if (!d) return;
    if (isActive() && S.state.profile !== key) { toast("别的角色的数字人正在运行，停止数字人后再预上传这个角色", "error"); return; }
    S.prewarm = { key, started: Date.now(), count: (d.outfits || []).length, done: false };
    renderPrewarm();
    try {
      const r = await withBusy($("wdPrewarm"), () => api.post("/api/wardrobe/prewarm", { profile: key }));
      const entries = Object.entries(r || {});
      const ok = entries.filter(([, v]) => v === "ok");
      const bad = entries.filter(([, v]) => v !== "ok");
      const secs = Math.round((Date.now() - S.prewarm.started) / 1000);
      S.prewarm.text = entries.length
        ? `预上传完成（${secs} 秒）：${ok.length} 张成功${bad.length ? `，${bad.length} 张失败：${bad.map(([k, v]) => `${k}（${v}）`).join("；")}` : ""}。之后换装大约 5 到 6 秒。`
        : "没有需要预上传的底图：衣柜是空的，或者没有 Vidu Key。";
      S.prewarm.error = bad.length > 0 && !ok.length;
      toast(bad.length ? `预上传有 ${bad.length} 张失败` : "衣柜底图已预上传到 Vidu", bad.length ? "error" : "ok");
    } catch (err) {
      S.prewarm.text = `预上传失败：${err.message}`;
      S.prewarm.error = true;
    }
    S.prewarm.done = true;
    renderPrewarm();
  });
  $("genOutfitForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = S.sel;
    const name = $("goName").value.trim();
    const description = $("goDesc").value.trim();
    if (!key) { toast("先在上面选一个角色", "error"); return; }
    if (!name) { toast("给新衣服起个名字", "error"); $("goName").focus(); return; }
    if (!description) { toast("写一下衣服的样子", "error"); $("goDesc").focus(); return; }
    try {
      const body = { name, description, model: $("goModel").value };
      const aliases = parseList($("goAliases").value);
      if (aliases.length) body.aliases = aliases;
      const r = await withBusy(e.submitter, () => api.post(`/api/profiles/${enc(key)}/outfits/generate`, body));
      trackJob(r.job, { type: "outfit", profile: key, label: name });
      $("goName").value = ""; $("goDesc").value = ""; $("goAliases").value = "";
      toast(`开始生成「${name}」，完成后自动进衣柜`);
    } catch (err) { toast(`生成失败：${err.message}`, "error"); }
  });

  function regenCount() {
    const n = qsa('input[type="checkbox"]', $("regenList")).filter((i) => i.checked).length;
    $("regenCount").textContent = n ? `选了 ${n} 套，会生成 ${n} 张图` : "一套都没选";
    $("regenOk").disabled = n === 0;
    return n;
  }
  async function openRegen() {
    const key = S.sel;
    if (!S.detail.get(key)) await loadDetail(key);
    const d = S.detail.get(key);
    const outfits = ((d && d.outfits) || []).filter((o) => !o.base);
    if (!outfits.length) { toast("衣柜里除了底图没有别的衣服，不用重画", "info"); return; }
    $("regenTitle").textContent = `按新底图重画「${profileName(key)}」的衣柜`;
    $("regenErr").textContent = "";
    const list = clear($("regenList"));
    for (const o of outfits) {
      const box = el("input", { type: "checkbox", value: o.name, checked: true });
      box.addEventListener("change", regenCount);
      list.append(el("label", { class: "check" }, box, el("span", { text: o.name })));
    }
    regenCount();
    if ((await openDialog($("regenDialog"))) !== "ok") return;
    const names = qsa('input[type="checkbox"]', list).filter((i) => i.checked).map((i) => i.value);
    const body = { model: $("regenModel").value };
    if (names.length < outfits.length) body.names = names;
    try {
      const r = await withBusy($("wdRegen"), () => api.post(`/api/profiles/${enc(key)}/outfits/regenerate`, body));
      const jobs = (r && r.jobs) || [];
      jobs.forEach((j) => trackJob(j, { type: "outfit", profile: key, label: `重画 ${(j.meta && j.meta.name) || ""}`.trim() }));
      if (!jobs.length) refreshJobs();
      if (S.regenNudge === key) S.regenNudge = null;
      toast(`开始重画 ${jobs.length || names.length} 套衣服，每套 20 到 60 秒，完成后自动替换`);
      renderWardrobe();
    } catch (err) { toast(`重画失败：${err.message}`, "error"); }
  }
  $("regenForm").addEventListener("submit", (e) => {
    if (e.submitter && e.submitter.value === "ok" && regenCount() === 0) { e.preventDefault(); $("regenErr").textContent = "至少选一套"; }
  });
  $("wdRegen").addEventListener("click", openRegen);
  $("wdNudgeGo").addEventListener("click", openRegen);
  $("wdNudgeX").addEventListener("click", () => { S.regenNudge = null; renderWardrobe(); });

  // ---------------------------------------------------------------- 场景
  SCENES.forEach(([label, text]) => {
    $("scPresets").append(el("button", {
      class: "chip-btn", type: "button", text: label, "aria-pressed": "false",
      onclick: (e) => {
        $("scPrompt").value = text;
        qsa(".chip-btn", $("scPresets")).forEach((b) => b.setAttribute("aria-pressed", String(b === e.currentTarget)));
      },
    }));
  });
  $("scPrompt").addEventListener("input", () => qsa(".chip-btn", $("scPresets")).forEach((b) => b.setAttribute("aria-pressed", "false")));
  $("scShowAll").addEventListener("change", () => renderScene());
  $("sceneForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = S.sel;
    const prompt = $("scPrompt").value.trim();
    if (!key) { toast("先在上面选一个角色", "error"); return; }
    if (!prompt) { toast("写一下场景，或者点一个常用场景", "error"); $("scPrompt").focus(); return; }
    try {
      const r = await withBusy(e.submitter, () => api.post("/api/media/scene", { profile: key, scene_prompt: prompt, model: $("scModel").value }));
      trackJob(r.job, { type: "scene", profile: key });
      toast("开始生成底图候选，大约一分钟");
    } catch (err) { toast(`生成失败：${err.message}`, "error"); }
  });
  function sceneResultIds() {
    const ids = new Set();
    for (const j of S.jobs || []) if (jobType(j) === "scene" && j.result_id) ids.add(j.result_id);
    return ids;
  }
  function renderScene() {
    const key = S.sel;
    const d = key ? S.detail.get(key) : null;
    const src = (d && d.image_url) || (key ? profileImage(key) : "");
    if ($("scBase").getAttribute("src") !== src) $("scBase").src = src;
    $("scBase").alt = key ? `${profileName(key)} 当前的底图` : "";
    $("scBaseName").textContent = key ? profileName(key) : "—";
    renderJobList($("scJobs"), (j) => jobType(j) === "scene", "还没有场景任务。");

    const showAll = $("scShowAll").checked;
    if (S.galleryErr && !S.gallery) {
      showMsg($("scMsg"), missingText(S.galleryErr, "素材库"), { type: S.galleryErr.missing ? "warn" : "error", retry: async () => { await loadGallery(); renderScene(); } });
    } else clear($("scMsg"));
    const ids = sceneResultIds();
    const items = (S.gallery || []).filter((it) => it.kind === "image" && (showAll || ids.has(it.id) || /scene|场景/i.test(it.source || "")));
    $("scCandSub").textContent = showAll ? `素材库里全部 ${items.length} 张图片` : `场景生成的 ${items.length} 张候选`;
    const sig = JSON.stringify([items.map((i) => i.id), key, showAll, !!S.gallery]);
    if (S.sigs.scene === sig) return;
    S.sigs.scene = sig;
    const grid = clear($("scGrid"));
    if (!S.gallery) {
      if (!S.galleryErr) grid.append(el("div", { class: "empty" }, el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { text: "正在读取素材库…" })));
      return;
    }
    if (!items.length) {
      grid.append(el("div", { class: "empty" }, el("b", { text: "还没有底图候选" }), el("span", { text: "在左边选个场景生成，大约一分钟出图；也可以勾上「显示素材库全部图片」。" })));
      return;
    }
    for (const it of items) grid.append(galleryTile(it, { scene: true }));
  }

  // ---------------------------------------------------------------- 动作
  function setIdleDirty(v) { S.idleDirty = v; $("idleDirty").hidden = !v; }
  function setTopicDirty(v) { S.topicDirty = v; $("topicDirty").hidden = !v; }
  function topicLines() { return $("topicText").value.split("\n").map((t) => t.trim()).filter(Boolean); }
  function renderTopicCount() { $("topicCount").textContent = `${topicLines().length} 条`; }
  function setKwDirty(v) { S.kwDirty = v; $("kwDirty").hidden = !v; }
  async function loadActions() {
    try { const d = await api.get("/api/actions"); S.actions = d.actions || []; S.actionsErr = null; }
    catch (err) { S.actionsErr = err; }
  }
  function tryButton(getContent) {
    const btn = el("button", { class: "btn btn-sm", type: "button", "data-needs-live": "" }, icon("play", "ico ico-sm"), "试一下");
    btn.addEventListener("click", async () => {
      const content = getContent().trim();
      if (!content) { toast("这一行没写动作指令（运行时留空会随机做一个），写上具体动作再试", "error"); return; }
      try { await withBusy(btn, () => api.post("/api/manual", { kind: "text_action", content })); toast(`已发送动作：${clip(content, 24)}`); }
      catch (err) { toast(`发送失败：${err.message}`, "error"); }
    });
    return btn;
  }
  function editCell(label, input, wide) {
    return el("label", { class: `cell${wide ? " cell-wide" : ""}` }, el("span", { class: "cell-label", text: label }), input);
  }
  function renderIdleRows() {
    const box = clear($("idleRows"));
    const rows = S.idleRows || [];
    if (!rows.length) box.append(el("p", { class: "edit-empty", text: "还没有待机小动作。点「添加一行」，比如：撩头发、看一眼弹幕、喝口水。" }));
    rows.forEach((row, idx) => {
      const name = el("input", { value: row.name || "", maxlength: 12, autocomplete: "off" });
      const content = el("textarea", { rows: 1, maxlength: 80 });
      content.value = row.content || "";
      const weight = el("input", { type: "number", min: "0.1", max: "5", step: "0.1", value: row.weight ?? 1, inputmode: "decimal" });
      const desc = el("input", { value: row.description || "", maxlength: 40, autocomplete: "off" });
      name.addEventListener("input", () => { row.name = name.value; setIdleDirty(true); });
      content.addEventListener("input", () => { row.content = content.value; setIdleDirty(true); });
      weight.addEventListener("input", () => { row.weight = weight.value; setIdleDirty(true); });
      desc.addEventListener("input", () => { row.description = desc.value; setIdleDirty(true); });
      const remove = el("button", { class: "btn btn-sm btn-danger", type: "button", "aria-label": `删除第 ${idx + 1} 行` }, icon("trash", "ico ico-sm"));
      remove.addEventListener("click", () => { rows.splice(idx, 1); setIdleDirty(true); renderIdleRows(); applyGates(); });
      box.append(el("div", { class: "edit-row", role: "group", "aria-label": `待机动作第 ${idx + 1} 行` },
        editCell("名称", name),
        editCell("动作指令", content, true),
        editCell("权重", weight),
        editCell("说明", desc, true),
        el("div", { class: "row-actions" }, tryButton(() => content.value), remove)));
    });
  }
  function renderKwRows() {
    const box = clear($("kwRows"));
    const rows = S.kwRows || [];
    if (!rows.length) box.append(el("p", { class: "edit-empty", text: "还没有关键词映射。点「添加一行」，比如关键词「比心，笔芯」→ 动作「双手在胸前比一个心」。" }));
    rows.forEach((row, idx) => {
      const kw = el("input", { value: row.keywords || "", maxlength: 200, autocomplete: "off" });
      const action = el("input", { value: row.action || "", maxlength: 60, autocomplete: "off" });
      kw.addEventListener("input", () => { row.keywords = kw.value; setKwDirty(true); });
      action.addEventListener("input", () => { row.action = action.value; setKwDirty(true); });
      const remove = el("button", { class: "btn btn-sm btn-danger", type: "button", "aria-label": `删除第 ${idx + 1} 行` }, icon("trash", "ico ico-sm"));
      remove.addEventListener("click", () => { rows.splice(idx, 1); setKwDirty(true); renderKwRows(); applyGates(); });
      box.append(el("div", { class: "edit-row", role: "group", "aria-label": `关键词映射第 ${idx + 1} 行` },
        editCell("关键词", kw, true),
        editCell("动作指令", action, true),
        el("div", { class: "row-actions" }, tryButton(() => action.value), remove)));
    });
  }
  function renderActions() {
    const key = S.sel;
    const d = key ? S.detail.get(key) : null;
    const err = key ? S.detailErr.get(key) : null;
    $("idleSub").textContent = `${key ? `「${profileName(key)}」` : ""}没人发弹幕时按权重随机做，权重 0.1 到 5，越大越常出现，最多 20 个`;
    $("idleDefault").hidden = !d || !!d.custom_idle_motions;
    $("idleRestore").hidden = !d || !d.custom_idle_motions;
    $("idleInUse").hidden = !key || !inUse(key);
    if (err) showMsg($("idleMsg"), missingText(err, "待机动作"), { type: err.missing ? "warn" : "error", retry: () => refreshProfile(key) });
    else clear($("idleMsg"));
    if (d && (S.idleFor !== key || !S.idleDirty)) {
      if (S.idleFor !== key || !S.idleDirty) {
        S.idleRows = (d.idle_motions || []).map((m) => ({ ...m }));
        S.idleFor = key;
        setIdleDirty(false);
        renderIdleRows();
      }
    } else if (!d) {
      S.idleRows = null;
      clear($("idleRows")).append(el("p", { class: "edit-empty", text: err ? "读不到这个角色的待机动作" : "正在读取…" }));
    }
    $("idleAdd").disabled = !d || (S.idleRows || []).length >= 20;
    $("idleSave").disabled = !d;

    $("topicSub").textContent = `${key ? `「${profileName(key)}」` : ""}没人发弹幕时从这里挑一个话题开口，全部说完一轮才会重复`;
    $("topicDefault").hidden = !d || !!d.custom_idle_topics;
    $("topicRestore").hidden = !d || !d.custom_idle_topics;
    $("topicInUse").hidden = !key || !inUse(key);
    if (err) showMsg($("topicMsg"), missingText(err, "冷场话题"), { type: err.missing ? "warn" : "error", retry: () => refreshProfile(key) });
    else clear($("topicMsg"));
    if (d && (S.topicFor !== key || !S.topicDirty)) {
      $("topicText").value = (d.idle_topics || []).join("\n");
      S.topicFor = key;
      setTopicDirty(false);
    } else if (!d) {
      $("topicText").value = "";
    }
    $("topicText").disabled = !d;
    $("topicSave").disabled = !d;
    renderTopicCount();

    if (S.actionsErr && !S.actions) showMsg($("kwMsg"), missingText(S.actionsErr, "关键词映射"), { type: S.actionsErr.missing ? "warn" : "error", retry: async () => { await loadActions(); renderActions(); } });
    else clear($("kwMsg"));
    if (S.actions && !S.kwDirty) {
      const sig = JSON.stringify(S.actions);
      if (S.sigs.kw !== sig) {
        S.sigs.kw = sig;
        S.kwRows = S.actions.map((a) => ({ keywords: (a.keywords || []).join("，"), action: a.action || "" }));
        renderKwRows();
      }
    } else if (!S.actions) clear($("kwRows")).append(el("p", { class: "edit-empty", text: S.actionsErr ? "读不到关键词映射" : "正在读取…" }));
    $("kwAdd").disabled = !S.actions;
    $("kwSave").disabled = !S.actions;
    applyGates();
  }
  $("idleRestore").addEventListener("click", async () => {
    const key = S.sel;
    const ok = await confirmBox({ title: "恢复内置默认的待机动作？", message: `「${profileName(key)}」自己的这套待机动作会被删掉，改回内置默认的那套。`, ok: "恢复默认", danger: true });
    if (!ok) return;
    try {
      const r = await withBusy($("idleRestore"), () => api.put(`/api/profiles/${enc(key)}`, { idle_motions: [] }));
      if (r && r.key === key) S.detail.set(key, r); else await loadDetail(key);
      setIdleDirty(false);
      S.idleFor = null;
      toast("已恢复内置默认的待机动作");
      renderActions();
    } catch (err) { showMsg($("idleMsg"), `恢复失败：${err.message}`); }
  });
  $("idleAdd").addEventListener("click", () => {
    if (!S.idleRows) return;
    if (S.idleRows.length >= 20) { toast("待机动作最多 20 个", "error"); return; }
    S.idleRows.push({ name: "", content: "", weight: 1, description: "" });
    setIdleDirty(true);
    renderIdleRows();
    applyGates();
    const rows = $("idleRows").querySelectorAll(".edit-row");
    const last = rows[rows.length - 1];
    if (last) last.querySelector("input").focus();
  });
  $("kwAdd").addEventListener("click", () => {
    if (!S.kwRows) return;
    S.kwRows.push({ keywords: "", action: "" });
    setKwDirty(true);
    renderKwRows();
    applyGates();
    const rows = $("kwRows").querySelectorAll(".edit-row");
    const last = rows[rows.length - 1];
    if (last) last.querySelector("input").focus();
  });
  $("idleSave").addEventListener("click", async () => {
    const key = S.sel;
    const rows = S.idleRows || [];
    const out = [];
    for (const [i, r] of rows.entries()) {
      const name = String(r.name || "").trim(), content = String(r.content || "").trim();
      if (!name && !content) continue;
      if (!content) { showMsg($("idleMsg"), `第 ${i + 1} 行还没写动作指令`); return; }
      const w = Number(r.weight);
      if (!(w >= 0.1 && w <= 5)) { showMsg($("idleMsg"), `第 ${i + 1} 行的权重要在 0.1 到 5 之间`); return; }
      out.push({ name: name || content.slice(0, 10), content, weight: Math.round(w * 10) / 10, description: String(r.description || "").trim() });
    }
    if (!out.length) { showMsg($("idleMsg"), "至少要有一个待机动作；想用内置那套就点「恢复内置默认」"); return; }
    clear($("idleMsg"));
    try {
      await withBusy($("idleSave"), () => api.put(`/api/profiles/${enc(key)}`, { idle_motions: out }));
      setIdleDirty(false);
      S.idleFor = null;
      toast("待机动作已保存");
      await loadDetail(key);
      renderActions();
    } catch (err) { showMsg($("idleMsg"), `保存失败：${err.message}`); }
  });
  $("topicText").addEventListener("input", () => { setTopicDirty(true); renderTopicCount(); });
  $("topicSave").addEventListener("click", async () => {
    const key = S.sel;
    const topics = topicLines();
    if (!topics.length) { showMsg($("topicMsg"), "至少写一个话题；想用内置那套就点「恢复内置默认」"); return; }
    const long = topics.findIndex((t) => t.length > 60);
    if (long >= 0) { showMsg($("topicMsg"), `第 ${long + 1} 条太长了，一条 60 字以内`); return; }
    if (topics.length > 100) { showMsg($("topicMsg"), "冷场话题最多 100 条"); return; }
    clear($("topicMsg"));
    try {
      const r = await withBusy($("topicSave"), () => api.put(`/api/profiles/${enc(key)}`, { idle_topics: topics }));
      if (r && r.key === key) S.detail.set(key, r); else await loadDetail(key);
      setTopicDirty(false);
      S.topicFor = null;
      toast(`冷场话题已保存，共 ${topics.length} 条`);
      renderActions();
    } catch (err) { showMsg($("topicMsg"), `保存失败：${err.message}`); }
  });
  $("topicRestore").addEventListener("click", async () => {
    const key = S.sel;
    const ok = await confirmBox({ title: "恢复内置默认的冷场话题？", message: `「${profileName(key)}」自己的这套话题会被删掉，改回内置的通用话题。`, ok: "恢复默认", danger: true });
    if (!ok) return;
    try {
      const r = await withBusy($("topicRestore"), () => api.put(`/api/profiles/${enc(key)}`, { idle_topics: [] }));
      if (r && r.key === key) S.detail.set(key, r); else await loadDetail(key);
      setTopicDirty(false);
      S.topicFor = null;
      toast("已恢复内置默认的冷场话题");
      renderActions();
    } catch (err) { showMsg($("topicMsg"), `恢复失败：${err.message}`); }
  });
  $("kwSave").addEventListener("click", async () => {
    const rows = S.kwRows || [];
    const out = [];
    for (const [i, r] of rows.entries()) {
      const keywords = parseList(r.keywords), action = String(r.action || "").trim();
      if (!keywords.length && !action) continue;
      if (!keywords.length) { showMsg($("kwMsg"), `第 ${i + 1} 行还没填关键词`); return; }
      out.push({ keywords, action: action || null });
    }
    if (!out.length) { showMsg($("kwMsg"), "至少要有一条关键词映射"); return; }
    if (out.length > 80) { showMsg($("kwMsg"), "关键词映射最多 80 条"); return; }
    clear($("kwMsg"));
    try {
      await withBusy($("kwSave"), () => api.put("/api/actions", { actions: out }));
      S.actions = out;
      S.sigs.kw = null;
      setKwDirty(false);
      toast("关键词映射已保存");
      renderActions();
    } catch (err) { showMsg($("kwMsg"), `保存失败：${err.message}`); }
  });

  // ---------------------------------------------------------------- 声音
  async function loadVoices() {
    try { const d = await api.get("/api/voices"); S.voices = { library: d.library || [], system: d.system || [], cloned: d.cloned || [], old_count: d.old_count || 0, used_by: d.used_by || {}, samples: d.samples || {}, error: d.error || "" }; S.voicesErr = null; }
    catch (err) { S.voicesErr = err; }
  }
  function voiceLabel(voice) {
    if (!voice) return "默认音色";
    const named = S.voices && [...(S.voices.library || []), ...(S.voices.system || [])].find((v) => v.voice === voice);
    return named && named.name ? named.name : voice;
  }
  function voiceCard(v, current, cloned) {
    const isCur = v.voice === current;
    const head = el("div", { class: "voice-item-head" }, el("b", { text: cloned ? v.voice : (v.name || v.voice) }));
    if (isCur) head.append(el("span", { class: "tag tag-solid", text: "当前使用" }));
    const item = el("div", { class: `voice-item${isCur ? " is-current" : ""}` }, head);
    if (!cloned && v.desc) item.append(el("p", { text: v.desc }));
    if (!cloned && v.name && v.name !== v.voice) item.append(el("span", { class: "mono-wrap hint", text: v.voice }));
    if (v.live_score != null) item.append(el("span", { class: "voice-score", text: `实播活人感 ${v.live_score} · 原样本 ${v.source_score}` }));
    if (cloned) item.append(el("p", { text: "自己克隆的音色" }));
    // Recorded examples: excerpts from real shows ("实播") and the clip the voice was cloned from ("原样本").
    const samples = (S.voices && S.voices.samples && S.voices.samples[v.voice]) || [];
    if (samples.length) {
      const list = el("div", { class: "voice-samples" });
      samples.slice(0, 4).forEach((smp) => {
        const tag = smp.sample_kind === "live" ? "实播" : "原样本";
        const row = el("div", { class: "voice-sample" }, el("span", { class: "voice-sample-label", text: `${tag}${smp.label ? " · " + smp.label : ""}` }));
        const audio = el("audio", { controls: true, preload: "none", src: smp.url });
        audio.addEventListener("play", () => qsa("audio").forEach((other) => { if (other !== audio) other.pause(); }));  // one at a time
        row.append(audio);
        list.append(row);
      });
      item.append(list);
    } else if (!cloned) {
      item.append(el("span", { class: "hint", text: "还没有试听片段" }));
    }
    const users = ((S.voices && S.voices.used_by && S.voices.used_by[v.voice]) || []).map(profileName);
    if (users.length) item.append(el("span", { class: "voice-users", text: `正在用：${users.join("、")}` }));
    const btn = el("button", { class: "btn btn-sm", type: "button", text: isCur ? "正在使用" : "设为当前音色" });
    btn.disabled = isCur || !S.detail.get(S.sel);
    btn.addEventListener("click", () => setVoice(v.voice, btn));
    item.append(btn);
    // While this profile is on air, try a voice right away; Vidu switches after the current sentence.
    if (isOnAir() && S.state.profile === S.sel) {
      const live = S.state.voice === v.voice;
      const sw = el("button", { class: "btn btn-sm", type: "button", text: live ? "直播中正在用" : "直播中切过去" });
      sw.disabled = live;
      sw.addEventListener("click", async () => {
        try {
          await withBusy(sw, () => api.post("/api/session/voice", { voice: v.voice }));
          toast(`运行中的她换成 ${voiceLabel(v.voice)}，说完这句就生效。想以后每次启动都用它，再点“设为当前音色”`);
          S.sigs.voices = null;
        } catch (err) { toast(`切换失败：${err.message}`, "error"); }
      });
      item.append(sw);
    }
    return item;
  }
  function renderVoice() {
    const key = S.sel;
    const d = key ? S.detail.get(key) : null;
    const err = key ? S.detailErr.get(key) : null;
    const current = d ? d.voice || "" : null;
    $("cvSub").textContent = key ? `「${profileName(key)}」启动数字人时用这个音色` : "—";
    $("cvChip").textContent = d ? voiceLabel(current) : "—";
    const box = $("voiceMsg");
    clear(box);
    if (err) showMsg(box, missingText(err, "角色详情"), { type: err.missing ? "warn" : "error", retry: () => refreshProfile(key) });
    if (key && inUse(key)) { const h = el("div"); box.append(h); showMsg(h, "这个角色的数字人正在运行，换音色下次启动时生效。", { type: "info" }); }
    if (S.voices && S.voices.error) { const h = el("div"); box.append(h); showMsg(h, `克隆音色列表没读到：${S.voices.error}`, { type: "warn", retry: async () => { await loadVoices(); S.sigs.voices = null; renderVoice(); } }); }
    if (S.voicesErr && !S.voices) {
      const holder = el("div");
      box.append(holder);
      showMsg(holder, missingText(S.voicesErr, "音色列表"), { type: S.voicesErr.missing ? "warn" : "error", retry: async () => { await loadVoices(); renderVoice(); } });
    }
    const sig = JSON.stringify([S.voices, current, !!d, isOnAir() && S.state.profile === key ? S.state.voice : null, !!S.showOldVoices]);
    if (S.sigs.voices === sig) return;
    S.sigs.voices = sig;
    const sys = clear($("sysVoices"));
    const cl = clear($("clVoices"));
    if (!S.voices) {
      const txt = S.voicesErr ? "音色列表暂时读不到" : "正在读取音色…";
      sys.append(el("div", { class: "empty", text: txt }));
      cl.append(el("div", { class: "empty", text: txt }));
      return;
    }
    const lib = S.voices.library || [];
    if (lib.length) {
      // curated library first; the old system and cloned voices stay folded unless asked for
      $("sysVoiceTitle").textContent = "音色库";
      $("clVoiceTitle").textContent = `旧音色（${S.voices.old_count} 个）`;
      lib.forEach((v) => sys.append(voiceCard(v, current, false)));
      const toggle = el("button", { class: "btn btn-sm btn-ghost", type: "button", text: S.showOldVoices ? "收起旧音色" : `展开 ${S.voices.old_count} 个旧音色` });
      toggle.addEventListener("click", () => { S.showOldVoices = !S.showOldVoices; S.sigs.voices = null; renderVoice(); });
      cl.append(toggle);
      if (!S.showOldVoices) return;
    }
    if (!S.voices.system.length && !lib.length) sys.append(el("div", { class: "empty", text: "没有系统音色" }));
    (lib.length ? cl : sys).append(...S.voices.system.map((v) => voiceCard(v, current, false)));
    if (!S.voices.cloned.length && !lib.length) cl.append(el("div", { class: "empty" }, el("b", { text: "还没有克隆音色" }), el("span", { text: "用右边的表单上传一段录音，克隆一个专属音色。" })));
    S.voices.cloned.forEach((v) => cl.append(voiceCard(v, current, true)));
  }
  async function setVoice(voice, btn) {
    const key = S.sel;
    try {
      await withBusy(btn, () => api.put(`/api/profiles/${enc(key)}`, { voice }));
      const d = S.detail.get(key);
      if (d) d.voice = voice;
      toast(`「${profileName(key)}」的音色换成 ${voiceLabel(voice)}，下次启动数字人时生效`);
      S.sigs.voices = null;
      renderVoice();
      loadProfiles();
    } catch (err) { toast(`设置音色失败：${err.message}`, "error"); }
  }
  let origUrl = null, prevUrl = null;
  $("clSpeed").addEventListener("input", () => { $("clSpeedOut").textContent = `${Number($("clSpeed").value).toFixed(2)}×`; });
  $("clPitch").addEventListener("input", () => { const n = Number($("clPitch").value); $("clPitchOut").textContent = `${n > 0 ? "+" : ""}${n} 半音`; });
  function renderClGallery() {
    const box = clear($("clGallery"));
    if (S.clGallery) box.append(refItem(S.clGallery, () => { S.clGallery = null; renderClGallery(); $("clOrigBox").hidden = true; setFormMsg($("clDurMsg"), ""); }));
    $("clPickGallery").lastChild.textContent = S.clGallery ? "换一段" : "从素材库选";
  }
  function suggestVoiceName(raw) {
    if ($("clName").value) return;
    const base = String(raw || "").replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_]+/g, "_").replace(/^[^A-Za-z]+|_+$/g, "").slice(0, 32);
    if (base.length >= 3) $("clName").value = base;
  }
  function showOriginal(src) {
    $("clPrevBox").hidden = true;
    setFormMsg($("clDurMsg"), "");
    if (!src) { $("clOrigBox").hidden = true; return; }
    $("clOrig").src = src;
    $("clOrigBox").hidden = false;
    $("clDur").textContent = "";
  }
  function renderClFile() {
    const file = $("clFile").files[0];
    $("clFileName").textContent = file ? file.name : "还没选文件";
    $("clFileName").classList.toggle("has-file", !!file);
    $("clFileBtn").lastChild.textContent = file ? "换一个文件" : "选择音频文件";
  }
  $("clFileBtn").addEventListener("click", () => $("clFile").click());
  $("clFile").addEventListener("change", () => {
    renderClFile();
    const file = $("clFile").files[0];
    if (origUrl) URL.revokeObjectURL(origUrl);
    origUrl = null;
    if (!file) { showOriginal(S.clGallery ? S.clGallery.url : null); return; }
    S.clGallery = null;
    renderClGallery();
    origUrl = URL.createObjectURL(file);
    showOriginal(origUrl);
    suggestVoiceName(file.name);
  });
  $("clPickGallery").addEventListener("click", async () => {
    const picked = await pickGallery({ kind: "audio", title: "选一段参考音频", hint: "10 到 20 秒、只有一个人说话、没有背景音乐的录音效果最好。", selected: S.clGallery ? [S.clGallery.id] : [] });
    if (!picked || !picked[0]) return;
    S.clGallery = picked[0];
    $("clFile").value = "";
    renderClFile();
    if (origUrl) { URL.revokeObjectURL(origUrl); origUrl = null; }
    renderClGallery();
    showOriginal(S.clGallery.url);
  });
  $("clOrig").addEventListener("loadedmetadata", () => {
    const sec = $("clOrig").duration;
    if (!Number.isFinite(sec)) return;
    $("clDur").textContent = `· ${sec.toFixed(1)} 秒`;
    if (sec < 10 || sec > 20) setFormMsg($("clDurMsg"), `这段音频 ${sec.toFixed(1)} 秒，建议 10 到 20 秒，太短学不像，太长会被拒`, "warn");
    else setFormMsg($("clDurMsg"), "时长合适", "ok");
  });
  const hasCloneSource = () => !!($("clFile").files[0] || S.clGallery);
  function cloneData(withMeta) {
    const fd = new FormData();
    if ($("clFile").files[0]) fd.append("file", $("clFile").files[0]);
    else fd.append("gallery_id", S.clGallery.id);
    fd.append("speed", $("clSpeed").value);
    fd.append("pitch", $("clPitch").value);
    if (withMeta) {
      fd.append("name", $("clName").value.trim());
      const text = $("clText").value.trim();
      if (text) fd.append("text", text);
    }
    return fd;
  }
  $("clPreviewBtn").addEventListener("click", async () => {
    if (!hasCloneSource()) { setFormMsg($("cloneMsg"), "先上传或从素材库选一段参考音频", "error"); $("clFileBtn").focus(); return; }
    setFormMsg($("cloneMsg"), "正在处理…");
    try {
      const resp = await withBusy($("clPreviewBtn"), () => request("POST", "/api/voices/preview", cloneData(false), { form: true, raw: true }));
      const blob = await resp.blob();
      if (prevUrl) URL.revokeObjectURL(prevUrl);
      prevUrl = URL.createObjectURL(blob);
      $("clPrev").src = prevUrl;
      $("clPrevBox").hidden = false;
      setFormMsg($("cloneMsg"), "处理好了，听听看是否满意", "ok");
      $("clPrev").play().catch(() => { /* 浏览器拦截自动播放时手动点播放 */ });
    } catch (err) { setFormMsg($("cloneMsg"), `试听失败：${err.message}`, "error"); }
  });
  $("cloneForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("clName").value.trim();
    if (!hasCloneSource()) { setFormMsg($("cloneMsg"), "先上传或从素材库选一段参考音频", "error"); $("clFileBtn").focus(); return; }
    if (!/^[A-Za-z][A-Za-z0-9_]{2,31}$/.test(name)) { setFormMsg($("cloneMsg"), "音色名字要英文字母开头，只用字母、数字和下划线，3 到 32 位", "error"); $("clName").focus(); return; }
    setFormMsg($("cloneMsg"), "正在克隆，通常要 20 到 40 秒，别关页面…");
    try {
      const r = await withBusy($("clCloneBtn"), () => api.form("/api/voices/clone", cloneData(true)));
      const secs = r.seconds != null ? `，样本 ${r.seconds} 秒` : "";
      const said = r.text ? `。识别的文字：「${clip(r.text, 80)}」` : "";
      setFormMsg($("cloneMsg"), `克隆完成：${r.voice || name}${secs}${said}。在「克隆音色」里可以设为当前音色。`, "ok");
      if (r.sample_id) S.gallery = null;
      toast(`音色 ${r.voice || name} 克隆完成`);
      await loadVoices();
      S.sigs.voices = null;
      renderVoice();
    } catch (err) { setFormMsg($("cloneMsg"), `克隆失败：${err.message}`, "error"); }
  });

  // ---------------------------------------------------------------- 素材 & 任务
  async function loadGallery() {
    if (S.inflight.has("gallery")) return S.inflight.get("gallery");
    const job = (async () => {
      try { const d = await api.get("/api/media/gallery"); S.gallery = d.items || []; S.galleryErr = null; }
      catch (err) { S.galleryErr = err; }
    })();
    S.inflight.set("gallery", job);
    try { await job; } finally { S.inflight.delete("gallery"); }
  }
  function jobType(j) {
    const k = String(j.kind || "");
    if (JOB_KIND[k]) return k;
    const meta = S.jobMeta.get(j.id);
    if (meta) return meta.type;
    if (/outfit|wardrobe/.test(k)) return "outfit";
    if (/scene/.test(k)) return "scene";
    return k;
  }
  const isRunning = (j) => j.status === "queued" || j.status === "running";
  function trackJob(job, meta) {
    if (!job || !job.id) { refreshJobs(); return; }
    S.jobMeta.set(job.id, meta);
    if (!S.jobs) S.jobs = [];
    if (!S.jobsById.has(job.id)) S.jobs.unshift(job);
    S.jobsById.set(job.id, job);
    renderJobsEverywhere();
    scheduleJobs(1500);
  }
  async function refreshJobs() {
    clearTimeout(S.jobTimer);
    S.jobTimer = null;
    let d;
    try { d = await api.get("/api/media/jobs"); S.jobsErr = null; }
    catch (err) { S.jobsErr = err; renderJobsEverywhere(); if (!err.missing && S.jobs && S.jobs.some(isRunning)) scheduleJobs(6000); return; }
    const jobs = d.jobs || [];
    S.spent24h = d.spent_cny_24h != null ? Number(d.spent_cny_24h) : null;
    const finished = [];
    for (const j of jobs) {
      const before = S.jobsById.get(j.id);
      if (before && isRunning(before) && !isRunning(j)) finished.push(j);
    }
    S.jobs = jobs;
    S.jobsById = new Map(jobs.map((j) => [j.id, j]));
    if (finished.length) {
      for (const j of finished) {
        const isOutfit = jobType(j) === "outfit";
        const name = (j.meta && j.meta.name) || (S.jobMeta.get(j.id) || {}).label;
        const what = isOutfit ? `衣服「${name || clip(j.prompt, 12)}」` : `${JOB_KIND[jobType(j)] || "任务"}「${clip(j.prompt, 16)}」`;
        if (j.status === "done") toast(`${what}生成好了${isOutfit ? "，衣柜已更新" : ""}`);
        else toast(`${what}生成失败：${j.error || "未知原因"}`, "error");
      }
      await loadGallery();
      const outfitKeys = new Set(finished.filter((j) => jobType(j) === "outfit").map((j) => (j.meta && j.meta.profile) || (S.jobMeta.get(j.id) || {}).profile || S.sel));
      for (const key of outfitKeys) if (key) await loadDetail(key);
      S.sigs.gallery = null; S.sigs.scene = null; S.sigs.outfits = null;
      renderCurrentTab();
    } else renderJobsEverywhere();
    if (jobs.some(isRunning)) scheduleJobs(3000);
  }
  function scheduleJobs(ms) {
    clearTimeout(S.jobTimer);
    S.jobTimer = setTimeout(refreshJobs, ms);
  }
  function jobProgress(j) {
    const p = Number(j.progress);
    if (!Number.isFinite(p) || p <= 0) return null;
    return Math.min(100, p <= 1 ? p * 100 : p);
  }
  function jobRow(j) {
    const [label, tone] = JOB_STATUS[j.status] || [j.status || "—", ""];
    const type = jobType(j);
    const local = S.jobMeta.get(j.id) || {};
    const jm = j.meta || {};
    const meta = { profile: jm.profile || local.profile, label: jm.name || local.label };
    const side = el("div", { class: "job-side" }, el("span", { class: `chip${tone ? ` ${tone}` : ""}` }, el("span", { class: "dot", "aria-hidden": "true" }), el("span", { class: "label", text: label })));
    if (j.status === "done" && j.result_id) {
      side.append(el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "查看", onclick: async () => {
        if (j.result_url) { openPreview({ kind: type === "video" ? "video" : "image", url: j.result_url, prompt: j.prompt }); return; }
        let item = (S.gallery || []).find((it) => it.id === j.result_id);
        if (!item) { await loadGallery(); item = (S.gallery || []).find((it) => it.id === j.result_id); }
        if (item) openPreview(item); else toast("素材库里还找不到这个结果，稍后刷新试试", "error");
      } }));
    }
    const metaLine = el("div", { class: "job-meta" });
    if (meta.profile) metaLine.append(el("span", { text: profileName(meta.profile) }));
    if (j.model) metaLine.append(el("span", { text: j.model }));
    if (j.created) metaLine.append(el("span", { text: fmtWhen(j.created) }));
    if (j.cost_cny != null && j.cost_cny !== "") metaLine.append(el("span", { text: money(j.cost_cny) }));
    const row = el("div", { class: "job" },
      el("span", { class: "tag job-kind", text: JOB_KIND[type] || type || "任务" }),
      el("div", { class: "job-prompt", title: j.prompt || "", text: j.prompt || meta.label || "（没有描述）" }),
      side,
      metaLine);
    if (isRunning(j)) {
      const p = jobProgress(j);
      const bar = el("div", { class: `job-bar${p == null ? " is-indeterminate" : ""}`, role: "progressbar", "aria-label": "生成进度", "aria-valuemin": "0", "aria-valuemax": "100" }, el("i"));
      if (p != null) { bar.firstChild.style.width = `${p}%`; bar.setAttribute("aria-valuenow", String(Math.round(p))); }
      row.append(bar);
    }
    if (j.status === "failed" && j.error) row.append(el("p", { class: "job-error", text: j.error }));
    return row;
  }
  function renderJobList(container, filter, emptyText) {
    const jobs = (S.jobs || []).filter(filter);
    const sig = JSON.stringify([!!S.jobs, jobs.map((j) => [j.id, j.status, j.progress, j.error, j.result_id]), S.jobsErr && S.jobsErr.message]);
    if (container.dataset.sig === sig) return;
    container.dataset.sig = sig;
    clear(container);
    if (S.jobsErr && !S.jobs) {
      const holder = el("div");
      container.append(holder);
      showMsg(holder, missingText(S.jobsErr, "任务列表"), { type: S.jobsErr.missing ? "warn" : "error", retry: () => refreshJobs() });
      return;
    }
    if (!S.jobs) { container.append(el("p", { class: "hint", text: "正在读取任务…" })); return; }
    if (!jobs.length) { container.append(el("p", { class: "hint", text: emptyText })); return; }
    const sorted = jobs.slice().sort((a, b) => (toDate(b.created) || 0) - (toDate(a.created) || 0));
    sorted.slice(0, 30).forEach((j) => container.append(jobRow(j)));
  }
  function renderJobsEverywhere() {
    if (S.tab === "media") { renderMediaJobs(); renderGallery(); }
    if (S.tab === "wardrobe") renderJobList($("wdJobs"), (j) => jobType(j) === "outfit", "还没有生成任务。右上方填好描述就能生成一套新衣服。");
    if (S.tab === "scene") renderJobList($("scJobs"), (j) => jobType(j) === "scene", "还没有场景任务。");
  }
  function renderMediaJobs() {
    const running = (S.jobs || []).filter(isRunning).length;
    $("jobsSpent").hidden = S.spent24h == null;
    if (S.spent24h != null) $("jobsSpent").textContent = `近 24 小时花了 ${money(S.spent24h)}`;
    $("jobsSub").textContent = running ? `${running} 个任务在跑，每 3 秒自动刷新` : "有任务在跑时每 3 秒自动刷新。生图一般 20 到 60 秒，生视频 2 到 4 分钟";
    renderJobList($("mediaJobs"), () => true, "还没有生成任务。上面填好描述就能开始。");
  }
  $("jobsRefresh").addEventListener("click", (e) => { e.preventDefault(); withBusy($("jobsRefresh"), refreshJobs); });

  function galleryTile(it, { scene = false } = {}) {
    const kindLabel = MEDIA_KIND[it.kind] || it.kind || "素材";
    const badges = el("div", { class: "tile-badges" }, el("span", { class: "tag tag-muted", text: kindLabel }));
    let mediaBox;
    if (it.kind === "video") {
      mediaBox = el("div", { class: "tile-media is-dark" }, el("video", { src: it.url, controls: true, preload: "metadata", playsInline: true, "aria-label": `视频：${clip(it.prompt, 30)}` }));
    } else if (it.kind === "audio") {
      mediaBox = el("div", { class: "tile-media" }, el("div", { class: "audio-ph" }, icon("voice")), badges);
    } else {
      mediaBox = el("div", { class: "tile-media" },
        el("img", { src: it.url, alt: it.prompt ? `图片：${clip(it.prompt, 40)}` : "素材图片", loading: "lazy", decoding: "async", width: 240, height: 320 }),
        badges,
        el("button", { class: "tile-zoom", type: "button", "aria-label": "查看大图", onclick: () => openPreview(it) }, icon("eye")));
    }
    const body = el("div", { class: "tile-body" });
    if (it.kind === "audio") body.append(el("audio", { src: it.url, controls: true, preload: "metadata", style: "width:100%;height:40px" }));
    body.append(el("p", { class: "tile-text", title: it.prompt || "", text: it.prompt || it.source || "（没有描述）" }));
    const meta = el("div", { class: "tile-meta" });
    if (it.model) meta.append(el("span", { class: "tag tag-muted", text: it.model }));
    if (it.cost_cny) meta.append(el("span", { class: "tag tag-muted", text: money(it.cost_cny) }));
    if (it.created) meta.append(el("span", { class: "hint", text: fmtWhen(it.created) }));
    body.append(meta);
    const actions = el("div", { class: "tile-actions" });
    if (it.kind === "image") {
      const baseBtn = el("button", { class: `btn btn-sm${scene ? " btn-primary" : ""}`, type: "button", text: "设为底图", title: S.sel ? `设为「${profileName(S.sel)}」的底图` : "" });
      baseBtn.disabled = !S.sel;
      baseBtn.addEventListener("click", () => setBase(it));
      actions.append(baseBtn);
      if (!scene) {
        const wardBtn = el("button", { class: "btn btn-sm", type: "button", text: "加入衣柜" });
        wardBtn.disabled = !S.sel;
        wardBtn.addEventListener("click", () => addToWardrobe(it));
        actions.append(wardBtn);
      }
    }
    if (!scene) {
      const delBtn = el("button", { class: "tile-del", type: "button", "aria-label": `删除这个${kindLabel}`, title: "删除" }, icon("trash"));
      delBtn.addEventListener("click", () => deleteGalleryItem(it, delBtn));
      mediaBox.append(delBtn);
    }
    return el("article", { class: "tile" }, mediaBox, body, actions.childElementCount ? actions : null);
  }
  async function deleteGalleryItem(it, btn) {
    const ok = await confirmBox({ title: `删除这个${MEDIA_KIND[it.kind] || "素材"}？`, message: `「${clip(it.prompt || it.source || it.id, 40)}」会从素材库移除，文件不能恢复。已经设成底图或进了衣柜的不受影响。`, ok: "删除", danger: true });
    if (!ok) return;
    try {
      await withBusy(btn, () => api.del(`/api/media/gallery/${enc(it.id)}`));
      S.gallery = (S.gallery || []).filter((x) => x.id !== it.id);
      toast("已从素材库删除");
      renderMedia();
    } catch (err) { toast(`删除失败：${err.message}`, "error"); }
  }
  function galMatches(it, q) {
    if (!q) return true;
    const hay = [it.prompt, it.source, it.model, it.full_prompt, it.voice].filter(Boolean).join(" ").toLowerCase();
    return q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
  }
  function wallItem(it, list) {
    const name = clip(it.prompt || it.source || "没有名字", 42);
    let media;
    if (it.kind === "video") media = el("video", { src: `${it.url}#t=0.5`, preload: "metadata", muted: true, playsInline: true, "aria-hidden": "true" });
    else if (it.kind === "audio") media = el("div", { class: "gal-audio" }, icon("voice"), el("audio", { src: it.url, controls: true, preload: "none" }));
    else media = el("img", { src: it.thumb_url || it.url, alt: name, loading: "lazy", decoding: "async" });
    const fig = el("figure", { class: `gal-item is-${it.kind}` }, media);
    // Row-major masonry: each tile spans grid rows in proportion to its picture once the size is known.
    fig.dataset.ratio = it.kind === "audio" ? "" : "0.5625";
    if (it.kind === "image") media.addEventListener("load", () => fitTile(fig, media.naturalHeight / media.naturalWidth));
    if (it.kind === "video") media.addEventListener("loadedmetadata", () => fitTile(fig, media.videoHeight / media.videoWidth));
    if (it.kind !== "audio") fig.append(el("button", { class: "gal-open", type: "button", "aria-label": `查看大图：${name}`, onclick: () => openPreview(it, list) }));
    fig.append(el("figcaption", { class: "gal-cap" }, it.kind === "video" ? el("span", { class: "gal-kind", text: "视频" }) : null, el("span", { text: name })));
    const acts = el("div", { class: "gal-actions" });
    if (it.kind === "image" && S.sel) {
      acts.append(el("button", { class: "gal-act", type: "button", text: "设为底图", title: `设为「${profileName(S.sel)}」的底图`, onclick: () => setBase(it) }));
      acts.append(el("button", { class: "gal-act", type: "button", text: "加入衣柜", title: `加入「${profileName(S.sel)}」的衣柜`, onclick: () => addToWardrobe(it) }));
    }
    const del = el("button", { class: "gal-act gal-act-icon", type: "button", "aria-label": "删除", title: "删除" }, icon("trash", "ico ico-sm"));
    del.addEventListener("click", () => deleteGalleryItem(it, del));
    acts.append(del);
    fig.append(acts);
    return fig;
  }
  function fitTile(fig, ratio) {
    if (ratio) fig.dataset.ratio = String(ratio);
    const r = Number(fig.dataset.ratio);
    if (!r) { fig.style.removeProperty("--span"); return; }
    const gap = S.galSize === "small" ? 10 : 14;
    const width = fig.getBoundingClientRect().width || 300;
    fig.style.setProperty("--span", String(Math.ceil((width * r + gap) / 2)));
  }
  function fitAllTiles() { qsa("#galGrid .gal-item").forEach((fig) => fitTile(fig)); }
  let fitTimer = null;
  window.addEventListener("resize", () => { clearTimeout(fitTimer); fitTimer = setTimeout(fitAllTiles, 120); });
  function pendingItem(j) {
    const [w, h] = String((j.meta && j.meta.size) || "16:9").split(":").map(Number);
    const label = (S.jobMeta.get(j.id) || {}).label || (j.meta && j.meta.name) || j.prompt || "";
    const status = j.status === "queued" ? "排队中" : `生成中${j.progress ? ` ${Math.round(j.progress)}%` : ""}`;
    const fig = el("figure", { class: "gal-item is-pending", style: `aspect-ratio:${w || 16} / ${h || 9}` },
      el("div", { class: "gal-pending" }, el("span", { class: "spinner", "aria-hidden": "true" }), el("b", { text: status }), el("span", { class: "gal-pending-text", text: clip(label, 60) })));
    fig.dataset.ratio = String((h || 9) / (w || 16));
    return fig;
  }
  function renderGallery() {
    if (S.galleryErr && !S.gallery) showMsg($("galMsg"), missingText(S.galleryErr, "素材库"), { type: S.galleryErr.missing ? "warn" : "error", retry: async () => { await loadGallery(); renderMedia(); } });
    else clear($("galMsg"));
    const all = S.gallery || [];
    const counts = { image: 0, video: 0, audio: 0 };
    all.forEach((it) => { if (counts[it.kind] != null) counts[it.kind] += 1; });
    const q = S.galSearch.trim();
    // "全部" is the picture wall (images and videos); audio, including voice audition clips, lives under 音频.
    const items = all.filter((it) => (S.galKind === "all" ? it.kind !== "audio" : it.kind === S.galKind) && galMatches(it, q))
      .sort((a, b) => (toDate(b.created) || 0) - (toDate(a.created) || 0));
    const pending = (S.jobs || []).filter((j) => isRunning(j) && jobType(j) !== "outfit" && !q && (S.galKind === "all" || (S.galKind === "video") === (jobType(j) === "video")));
    $("galSub").textContent = S.gallery ? (q || S.galKind !== "all" ? `找到 ${items.length} 个 · 共 ${all.length} 个` : `图片 ${counts.image} · 视频 ${counts.video} · 音频 ${counts.audio}`) + (pending.length ? ` · ${pending.length} 个在生成` : "") : "—";
    const grid = $("galGrid");
    grid.classList.toggle("is-small", S.galSize === "small");
    const sig = JSON.stringify([items.map((i) => [i.id, i.prompt]), pending.map((j) => [j.id, j.status, j.progress]), S.sel, !!S.gallery]);
    if (S.sigs.gallery === sig) return;
    S.sigs.gallery = sig;
    clear(grid);
    if (!S.gallery) {
      if (!S.galleryErr) grid.append(el("div", { class: "empty" }, el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { text: "正在读取素材库…" })));
      return;
    }
    pending.forEach((j) => grid.append(pendingItem(j)));
    if (!items.length && !pending.length) {
      grid.append(el("div", { class: "empty" }, el("b", { text: q ? `没有找到“${q}”` : S.galKind === "all" ? "素材库还是空的" : `还没有${MEDIA_KIND[S.galKind]}` }),
        el("span", { text: q ? "换个词试试，比如角色名、衣服或表情。" : "点右上角“生成 / 上传”做一张图或传个文件。" })));
      return;
    }
    const viewable = items.filter((it) => it.kind !== "audio");
    items.forEach((it) => grid.append(wallItem(it, viewable)));
    requestAnimationFrame(fitAllTiles);
  }
  $("galFilter").addEventListener("change", (e) => { S.galKind = e.target.value; renderGallery(); });
  $("galSize").addEventListener("change", (e) => { S.galSize = e.target.value; LS.set("wb.galSize", S.galSize); renderGallery(); requestAnimationFrame(fitAllTiles); });
  $("galSearch").addEventListener("input", () => { S.galSearch = $("galSearch").value; renderGallery(); });
  $("galRefresh").addEventListener("click", () => withBusy($("galRefresh"), async () => { await loadGallery(); S.sigs.gallery = null; renderMedia(); }));
  function setGenPanel(open) {
    $("genPanel").hidden = !open;
    $("galCreateBtn").setAttribute("aria-expanded", String(open));
    $("galCreateBtn").lastChild.textContent = open ? "收起生成面板" : "生成 / 上传";
  }
  $("galCreateBtn").addEventListener("click", () => setGenPanel($("genPanel").hidden));

  function refItem(it, onRemove) {
    const isAudio = it.kind === "audio";
    return el("div", { class: `ref-item${isAudio ? " is-audio" : ""}` },
      isAudio ? [icon("voice", "ico ico-sm"), el("span", { text: clip(it.prompt || it.source || it.id, 18) })] : el("img", { src: it.url, alt: "参考图", loading: "lazy", decoding: "async", width: 64, height: 80 }),
      el("button", { class: "ref-remove", type: "button", "aria-label": "移除", onclick: onRemove }, el("span", null, icon("close"))));
  }
  function renderRefs() {
    const gi = clear($("giRefs"));
    S.giRefs.forEach((it, idx) => gi.append(refItem(it, () => { S.giRefs.splice(idx, 1); renderRefs(); })));
    $("giPickRefs").lastChild.textContent = S.giRefs.length ? `参考图（${S.giRefs.length}）· 修改` : "从素材库选参考图";
    const gv = clear($("gvImage"));
    if (S.gvImage) gv.append(refItem(S.gvImage, () => { S.gvImage = null; renderRefs(); }));
    $("gvPickImage").textContent = S.gvImage ? "换一张" : "选图片";
    const ga = clear($("gvAudio"));
    if (S.gvAudio) ga.append(refItem(S.gvAudio, () => { S.gvAudio = null; renderRefs(); }));
    $("gvPickAudio").textContent = S.gvAudio ? "换一段" : "选音频";
    const mode = (document.querySelector('input[name="gvMode"]:checked') || {}).value;
    $("gvImageLabel").textContent = mode === "image" ? "首帧图片（必填）" : mode === "reference" ? "参考图片（必填）" : "参考图片（文生视频不用）";
    $("gvPickImage").disabled = mode === "text";
    $("gvPickAudio").disabled = mode !== "reference";
    $("gvAudioLabel").textContent = mode === "reference" ? "声音参考（可选）" : "声音参考（仅参考生视频）";
  }
  $("giPickRefs").addEventListener("click", async () => {
    const picked = await pickGallery({ kind: "image", multiple: true, max: 4, title: "选参考图（最多 4 张）", hint: "角色底图用来保持长相，衣服或场景图做参考。", selected: S.giRefs.map((i) => i.id) });
    if (picked) { S.giRefs = picked; renderRefs(); }
  });
  $("gvPickImage").addEventListener("click", async () => {
    const picked = await pickGallery({ kind: "image", title: "选视频用的图片", selected: S.gvImage ? [S.gvImage.id] : [] });
    if (picked) { S.gvImage = picked[0] || null; renderRefs(); }
  });
  $("gvPickAudio").addEventListener("click", async () => {
    const picked = await pickGallery({ kind: "audio", title: "选参考音频", selected: S.gvAudio ? [S.gvAudio.id] : [] });
    if (picked) { S.gvAudio = picked[0] || null; renderRefs(); }
  });
  qsa('input[name="gvMode"]').forEach((i) => i.addEventListener("change", renderRefs));

  $("genImageForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = $("giPrompt").value.trim();
    if (!prompt) { toast("写一下要生成的画面", "error"); $("giPrompt").focus(); return; }
    const size = (document.querySelector('input[name="giSize"]:checked') || {}).value || "9:16";
    try {
      const r = await withBusy(e.submitter, () => api.post("/api/media/image", { model: $("giModel").value, prompt, ref_ids: S.giRefs.slice(0, 4).map((i) => i.id), size, photo_real: $("giPhotoReal").checked }));
      trackJob(r.job, { type: "image" });
      setGenPanel(false);
      S.sigs.gallery = null;
      renderGallery();
      toast("开始生成图片，一般 20 到 60 秒，下面第一格就是它");
    } catch (err) { toast(`生成失败：${err.message}`, "error"); }
  });
  $("genVideoForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = $("gvPrompt").value.trim();
    const mode = (document.querySelector('input[name="gvMode"]:checked') || {}).value || "text";
    const duration = Number($("gvDuration").value);
    if (!prompt) { toast("写一下视频画面", "error"); $("gvPrompt").focus(); return; }
    if (mode !== "text" && !S.gvImage) { toast(mode === "image" ? "图生视频要先选一张首帧图片" : "参考生视频要先选一张参考图片", "error"); $("gvPickImage").focus(); return; }
    if (!(duration >= 4 && duration <= 12)) { toast("时长要在 4 到 12 秒之间", "error"); $("gvDuration").focus(); return; }
    const body = {
      model: $("gvModel").value, mode, prompt,
      image_id: mode !== "text" && S.gvImage ? S.gvImage.id : null,
      audio_id: mode === "reference" && S.gvAudio ? S.gvAudio.id : null,
      duration: Math.round(duration),
      resolution: (document.querySelector('input[name="gvRes"]:checked') || {}).value || "480p",
    };
    try {
      const r = await withBusy(e.submitter, () => api.post("/api/media/video", body));
      trackJob(r.job, { type: "video" });
      setGenPanel(false);
      S.sigs.gallery = null;
      renderGallery();
      toast("开始生成视频，一般 2 到 4 分钟，下面第一格就是它");
    } catch (err) { toast(`生成失败：${err.message}`, "error"); }
  });

  async function uploadFiles(files, msgNode) {
    const list = Array.from(files || []);
    const added = [];
    for (const [i, file] of list.entries()) {
      if (msgNode) setFormMsg(msgNode, `正在上传 ${i + 1}/${list.length}：${file.name}`);
      const fd = new FormData();
      fd.append("file", file);
      try {
        const item = await api.form("/api/media/upload", fd);
        if (item && item.id) {
          added.push(item);
          if (S.gallery) S.gallery = [item, ...S.gallery.filter((x) => x.id !== item.id)];
        }
      } catch (err) {
        if (msgNode) setFormMsg(msgNode, `「${file.name}」上传失败：${err.message}`, "error");
        toast(`上传失败：${err.message}`, "error");
        return added;
      }
    }
    if (!S.gallery || !added.length) await loadGallery();
    if (msgNode) setFormMsg(msgNode, `已上传 ${added.length || list.length} 个文件`, "ok");
    S.sigs.gallery = null;
    return added;
  }
  $("upFiles").addEventListener("change", async () => {
    const files = $("upFiles").files;
    if (!files.length) return;
    await uploadFiles(files, $("upMsg"));
    $("upFiles").value = "";
    renderMedia();
  });
  $("pickerUpload").addEventListener("click", () => $("pickerFile").click());
  $("pickerFile").addEventListener("change", async () => {
    const files = $("pickerFile").files;
    if (!files.length) return;
    const msg = el("p", { class: "form-msg" });
    clear($("pickerMsg")).append(msg);
    const added = await uploadFiles(files, msg);
    $("pickerFile").value = "";
    for (const it of added) {
      if (picker.kind !== "any" && it.kind !== picker.kind) continue;
      if (!picker.multiple) picker.chosen.clear();
      else if (picker.max && picker.chosen.size >= picker.max) break;
      picker.chosen.add(it.id);
    }
    renderPicker();
    $("pickerMsg").append(msg);
  });

  function renderMedia() {
    renderRefs();
    renderMediaJobs();
    renderGallery();
  }

  // ---------------------------------------------------------------- 设置
  const PROBES = [
    ["/api/state", "直播状态"],
    ["/api/obs", "OBS 图层"],
    ["/api/profiles", "角色"],
    ["/api/voices", "音色"],
    ["/api/actions", "动作映射"],
    ["/api/media/jobs", "生成任务"],
    ["/api/media/gallery", "素材库"],
  ];
  const probeResult = new Map();
  function statusRow(label, text, tone, sub) {
    const left = el("span", null, label);
    if (sub) left.append(" ", el("code", { text: sub }));
    return el("li", null, left, el("span", { class: `chip${tone ? ` ${tone}` : ""}` }, el("span", { class: "dot", "aria-hidden": "true" }), el("span", { class: "label", text })));
  }
  function renderSettings() {
    const s = S.state;
    const env = clear($("envList"));
    if (!s) env.append(el("li", null, el("span", { text: "正在连接本机服务…" })));
    else {
      env.append(statusRow("Vidu API Key", s.has_api_key ? "已配置" : "未配置，只能空跑", s.has_api_key ? "tone-ok" : "tone-err"));
      const yt = s.broadcast && s.broadcast.youtube_key;
      env.append(statusRow("YouTube 推流码", yt ? "已配置" : "未配置", yt ? "tone-ok" : "tone-warn"));
      env.append(statusRow("生图服务（道具、背景、衣服）", s.generator ? "已开启" : "未开启", s.generator ? "tone-ok" : "tone-warn"));
      env.append(statusRow("实时连接", S.connected ? "正常" : "断开，重连中", S.connected ? "tone-ok" : "tone-err"));
      env.append(statusRow("直播间默认", `${s.avatar_name || "—"} · ${s.model || "—"}`, ""));
    }
    const list = clear($("apiList"));
    for (const [path, label] of PROBES) {
      const r = probeResult.get(path);
      if (!r) list.append(statusRow(label, "检查中", "", path));
      else if (r.ok) list.append(statusRow(label, "正常", "tone-ok", path));
      else if (r.missing) list.append(statusRow(label, "未上线", "tone-warn", path));
      else list.append(statusRow(label, r.status ? `出错 ${r.status}` : "连不上", "tone-err", path));
    }
  }
  async function probeApis() {
    probeResult.clear();
    renderSettings();
    await Promise.all(PROBES.map(async ([path]) => {
      try { await api.get(path); probeResult.set(path, { ok: true }); }
      catch (err) { probeResult.set(path, { ok: false, missing: err.missing, status: err.status }); }
    }));
    if (S.tab === "settings") renderSettings();
  }
  $("apiRecheck").addEventListener("click", () => withBusy($("apiRecheck"), probeApis));

  // ---------------------------------------------------------------- tab hooks
  function renderCurrentTab() {
    switch (S.tab) {
      case "profiles": renderProfiles(); break;
      case "wardrobe": renderWardrobe(); break;
      case "scene": renderScene(); break;
      case "actions": renderActions(); break;
      case "voice": renderVoice(); break;
      case "media": renderMedia(); break;
      case "settings": renderSettings(); break;
      case "output": renderOutput(); renderObs(); break;
      default: break;
    }
  }
  const ENTER = {
    live() { if (!S.profiles && !S.profilesErr) loadProfiles(); },
    output() { renderOutput(); renderObs(); return loadObs(); },
    async profiles() {
      renderProfiles();
      await Promise.all([loadProfiles(), loadVoices()]);
      ensureSel();
      renderProfiles();
      if (S.sel && !S.profilesErr) { await loadDetail(S.sel); renderProfiles(); }
    },
    async wardrobe() {
      renderWardrobe();
      await loadProfiles();
      if (S.sel) await loadDetail(S.sel);
      renderWardrobe();
      refreshJobs();
    },
    async scene() {
      renderScene();
      await loadProfiles();
      await Promise.all([S.sel ? loadDetail(S.sel) : null, loadGallery()]);
      S.sigs.scene = null;
      renderScene();
      refreshJobs();
    },
    async actions() {
      renderActions();
      await loadProfiles();
      await Promise.all([S.sel ? loadDetail(S.sel) : null, loadActions()]);
      renderActions();
    },
    async voice() {
      renderVoice();
      await loadProfiles();
      await Promise.all([S.sel ? loadDetail(S.sel) : null, loadVoices()]);
      S.sigs.voices = null;
      renderVoice();
    },
    async media() {
      renderMedia();
      await Promise.all([loadProfiles(), loadGallery()]);
      renderMedia();
      refreshJobs();
    },
    settings() { renderSettings(); probeApis(); },
  };

  window.addEventListener("beforeunload", (e) => {
    if (S.pfDirty || S.idleDirty || S.topicDirty || S.kwDirty) { e.preventDefault(); e.returnValue = ""; }
  });

  // ---------------------------------------------------------------- boot
  applyPreview();
  renderOutput();
  renderDmAdv();
  $("manualText").disabled = false;
  applyGates();
  connect();
  api.get("/api/logs?n=200").then((entries) => (Array.isArray(entries) ? entries : []).forEach(addLog)).catch(() => { /* 旧服务也没关系 */ });
  S.galSize = LS.get("wb.galSize") === "small" ? "small" : "big";
  const sizeInput = document.querySelector(`input[name="galSize"][value="${S.galSize}"]`);
  if (sizeInput) sizeInput.checked = true;
  const initial = location.hash.slice(1);
  showTab(TABS[initial] ? initial : (TABS[LS.get("wb.tab")] ? LS.get("wb.tab") : "live"));
})();
