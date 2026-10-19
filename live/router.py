"""Turn one danmaku into one avatar command.

Rule-based on purpose: it is instant, free and predictable on stream. Kinds map to
Vidu realtime signals:

  chat        -> text_msg (99): the avatar's own LLM replies and picks a motion
  action      -> text_action (101) + a short text_dialog (102) line
  garment     -> prompt_operation (11) kind=garment, plus a text_dialog line
  object      -> prompt_operation kind=object
  background  -> prompt_operation kind=background
  reset       -> prompt_operation op_type=remove
  gift_thanks -> text_dialog thanking the sender
"""
from __future__ import annotations

import itertools
import re
import time
from dataclasses import dataclass, field

_ids = itertools.count(1)

VISUAL_KINDS = {"garment", "object", "background"}

GARMENT_WORDS = (
    "衣服 裙子 裙 外套 夹克 卫衣 T恤 t恤 白T 白t 长袖 短袖 衬衫 西装 毛衣 开衫 高领 吊带 风衣 大衣 羽绒服 棉袄 旗袍 汉服 校服 "
    "制服 运动服 球衣 睡袍 睡衣 帽子 帽 眼镜 墨镜 耳环 项链 围巾 领带 领结 发箍 发卡 蝴蝶结 手套 背包 书包 口罩 雨衣 披风 斗篷 "
    "盔甲 戏服 皇冠 头盔 耳机 工装 厨师服 白大褂 宇航服 熊猫服"
).split()
HAIR_WORDS = "丸子头 双马尾 马尾 卷发 短发 长发 大波浪 寸头 麻花辫 辫子 刘海 发型 头发 发色".split()
INFO_QUESTION = re.compile(r"(多久|多长时间|冷却|几次|几套|多少套|有哪些|有什么|为什么|为啥|怎么换|怎么才能|是不是|什么时候)")
GENERIC_GARMENT = {"衣服", "套衣服", "身衣服", "件衣服", "装", "套装", "身装扮", "造型", "个造型", "衣裳"}
BACKGROUND_WORDS = ("背景", "场景", "换个地方", "换个地点")
OBJECT_VERBS = (
    "手里拿着", "手上拿着", "拿出来", "拿出", "拿着", "拿起", "拿个", "拿一", "掏出", "举起", "举着", "抱着",
    "抱起", "抱一", "端着", "捧着", "变出", "吃个", "吃一", "吃", "喝个", "喝一", "喝", "弹",
    "给你来一", "送你一", "给你一", "你需要一", "来一", "来个", "来杯", "来辆",
)
VAGUE_ITEM = re.compile(r"^(什么|啥|东西|点东西|点啥|的|呢|吧)")
RESET_WORDS = ("换回来", "变回来", "恢复原样", "恢复原来", "原来的样子", "复原", "还原")
QUESTION_TAIL = re.compile(r"(吗|呢|么|嘛|\?|？)\s*$")
CHANGE_VERB = re.compile(r"(换|穿|戴|来|弄|变|试|拿|披|套|整|剪|扎|梳|做|染)")

QUANTIFIERS = set("一个只件套身条顶副双杯瓶把块串根碗份束盒台部本支朵颗张对罐袋包些点辆架艘")
STOP_CHARS = set(" ，,。.!！?？~～、;；:：然后再和跟并且又给我你的了吧呀啊哦嘛呢")
NOUN_STOPS = (QUANTIFIERS | STOP_CHARS | set("拿出上着换穿戴来把成变要想让请帮")) - {"的"}

ACTIONS = [
    (("跳舞", "跳个舞", "跳一段", "跳支舞", "来段舞"), "开心地跳一小段舞"),
    (("挥手", "打招呼", "招手"), "笑着向观众挥手打招呼"),
    (("比心", "比个心"), "双手在胸前比一个心"),
    (("转圈", "转一圈", "转个圈"), "原地轻快地转一圈"),
    (("鞠躬",), "微微鞠躬致谢"),
    (("点头", "点个头", "点点头"), "认真地点点头"),
    (("摇头", "摇个头"), "俏皮地摇摇头"),
    (("托腮", "托个腮", "托着腮"), "托着腮帮子歪头看着镜头笑"),
    (("眨眼", "眨个眼", "wink"), "对着镜头俏皮地眨一下眼"),
    (("鼓掌", "拍手"), "开心地拍手鼓掌"),
    (("加油",), "握拳做一个加油的手势"),
    (("敬礼", "敬个礼"), "调皮地敬一个礼"),
    (("思考", "想一想"), "托着下巴做思考状"),
    (("生气",), "假装生气地鼓起脸颊"),
    (("害羞",), "有点害羞地捂住脸笑"),
    (("笑一个", "笑一下", "微笑"), "开心地笑起来"),
    (("伸懒腰",), "伸一个懒腰"),
    (("比耶", "剪刀手"), "对着镜头比一个耶"),
    (("飞吻",), "对镜头挥挥手说再见"),
    (("做个动作", "来个动作", "整个活"), None),
]
FOOD = ("饼干 薯片 烤鸭 鸭 鸡 面 包子 馒头 饺子 糖 蛋糕 甜点 水果 西瓜 苹果 香蕉 葡萄 草莓 橘子 串 烧烤 火锅 披萨 汉堡 "
        "冰淇淋 雪糕 巧克力 零食 饭 肉 虾 蟹 鱼 粽子 月饼 辣条 薯条 寿司 三明治").split()
DRINK = "奶茶 果汁 咖啡 水 可乐 雪碧 饮料 茶 牛奶 酸奶 啤酒 汽水 椰汁 豆浆".split()


def prop_scene(item: str, verb_hint: str = "") -> str:
    """The reference-image caption. Vidu renders what this says continuously, so food is
    described as being eaten, not held (a follow-up action command alone does not make her eat)."""
    if verb_hint == "喝" or any(w in item for w in DRINK):
        return f"正在用吸管小口喝{item}，边喝边聊天"
    if verb_hint == "吃" or any(w in item for w in FOOD):
        return f"正在一口一口吃{item}，边吃边聊天"
    return f"手里拿着{item}"


OUTERWEAR = ("夹克", "外套", "大衣", "风衣", "开衫", "羽绒服", "披风", "斗篷", "马甲", "西装外套")
ACCESSORY = ("帽", "眼镜", "耳环", "项链", "围巾", "皇冠", "头盔", "耳机", "发箍", "发卡", "口罩", "手套")


def garment_text(item: str) -> str:
    """Accessories are put on, outerwear is layered over the T-shirt, anything else replaces the outfit."""
    if any(w in item for w in ACCESSORY):
        return f"戴上{item}"
    if any(w in item for w in OUTERWEAR):
        return f"在现在的衣服外面穿上{item}"
    return f"把身上的衣服整套换成{item}，上衣和下装一起换"


def interaction_for(item: str, verb_hint: str = "") -> str:
    """What the avatar does with a prop once it appears; holding it still reads as fake."""
    if verb_hint == "喝" or any(w in item for w in DRINK):
        return f"喝一口手里的{item}"
    if verb_hint == "吃" or any(w in item for w in FOOD):
        return f"吃一口手里的{item}，嚼一嚼"
    return f"把{item}举到镜头前晃一晃给观众看"


def desk_action(text: str, desk: dict) -> str | None:
    """Eat or drink what is already drawn on the desk in the base photo instead of conjuring a prop
    (a generated prop floats in her hand and lingers). Showing something ("拿出一只烤鸭") still gets a prop."""
    if "喝" in text and desk.get("drink"):
        return f"拿起桌上的{desk['drink']}喝一口，再放回桌上"
    if re.search(r"[吃尝嗦啃]", text) and desk.get("eat"):
        return f"从桌上的{desk['eat']}里拿一点吃，嚼一嚼"
    return None


RANDOM_ACTIONS = ["笑着向观众挥手打招呼", "双手在胸前比一个心", "原地轻快地转一圈", "对着镜头比一个耶", "开心地拍手鼓掌"]


@dataclass
class Command:
    user: str
    text: str
    kind: str
    gift: float = 0.0
    item: str | None = None
    action_text: str | None = None
    user_text: str | None = None
    id: int = field(default_factory=lambda: next(_ids))
    created_at: float = field(default_factory=time.time)
    status: str = "ready"
    note: str = ""
    asset: dict | None = None
    executed_at: float | None = None
    acked_at: float | None = None
    parts: list = field(default_factory=list)  # several visual changes from one danmaku, applied in one switch

    def public(self) -> dict:
        return {
            "id": self.id, "user": self.user, "text": self.text, "kind": self.kind, "gift": self.gift,
            "item": self.item, "status": self.status, "note": self.note,
            "created_at": self.created_at, "executed_at": self.executed_at, "acked_at": self.acked_at,
            "asset": (self.asset or {}).get("name"),
            "parts": [{"kind": p.kind, "item": p.item} for p in self.parts],
        }


def _noun_before(text: str, end: int, limit: int = 8) -> str:
    start = end
    while start > 0 and end - start < limit and text[start - 1] not in NOUN_STOPS:
        start -= 1
    return text[start:end]


def _noun_after(text: str, start: int, limit: int = 16) -> str:
    while start < len(text) and text[start] in QUANTIFIERS:
        start += 1
    end = start
    while end < len(text) and end - start < limit and text[end] not in STOP_CHARS:
        if end > start and text[end] in "拿吃喝穿戴换":
            break
        end += 1
    return text[start:end]


def _garment_item(text: str) -> str | None:
    best = None
    for word in sorted(GARMENT_WORDS, key=len, reverse=True):
        pos = text.find(word)
        if pos >= 0:
            phrase = _noun_before(text, pos) + word
            if best is None or len(phrase) > len(best):
                best = phrase
    return best


def classify(user: str, text: str, gift: float = 0.0) -> Command:
    """One danmaku may carry several orders ("教室背景，穿校服"): visual ones are merged."""
    segments = [seg for seg in re.split(r"[，,；;。！!\s]+", text.strip()) if seg]
    if len(segments) >= 2:
        parts: dict[str, Command] = {}
        for seg in segments:
            sub = _classify_one(user, seg, gift)
            if sub.kind in VISUAL_KINDS and sub.kind not in parts:
                parts[sub.kind] = sub
        if parts:
            whole = _classify_one(user, text, gift)
            if whole.kind in VISUAL_KINDS and whole.kind not in parts and whole.item:
                parts[whole.kind] = whole
            ordered = list(parts.values())
            head = ordered[0]
            return Command(user, text.strip(), head.kind, gift, item=head.item, user_text=head.user_text, action_text=head.action_text,
                           parts=ordered if len(ordered) > 1 else [])
    return _classify_one(user, text, gift)


def _classify_one(user: str, text: str, gift: float = 0.0) -> Command:
    raw = text.strip()
    t = raw.replace(" ", "")
    if not t and gift > 0:
        return Command(user, raw, "gift_thanks", gift)

    if any(word in t for word in RESET_WORDS):
        return Command(user, raw, "reset", gift)

    # "我的帽子呢" is a question, not an order; only an imperative verb makes it visual. Asking about the feature
    # itself ("换衣服要等多久", "你有几套衣服") is never an order, even with the verb in it.
    visual_ok = not QUESTION_TAIL.search(t) or re.search(r"(换|穿|戴|来|弄|变|染|拿|举|抱|端|捧)", t)
    visual_ok = visual_ok and not INFO_QUESTION.search(t)

    if visual_ok and any(word in t for word in BACKGROUND_WORDS):
        match = re.search(r"(?:换成|变成|改成|换到|变到|换个|换|是|去|到)(?P<item>[^，,。!！?？]{1,12})$", t)
        item = match.group("item") if match else None
        if item and any(word in item for word in BACKGROUND_WORDS):
            item = None
        if not item:  # "教室背景", "来个海边的场景"
            before = re.search(r"(?P<item>[^，,。!！?？\s]{1,10}?)的?(?:背景|场景)", t)
            if before:
                item = re.sub(r"^(换成|换个|换到|来个|来一个|换|来|要|弄个)", "", before.group("item")) or None
        return Command(user, raw, "background", gift, item=item, user_text=f"把背景换成{item}" if item else None)

    if visual_ok and ("染" in t or (CHANGE_VERB.search(t) and any(word in t for word in HAIR_WORDS))):
        if "染" in t:
            color = t.split("染", 1)[1]
            for junk in ("成", "一个", "个", "的", "头发", "发色"):
                color = color.replace(junk, "")
            color = color.rstrip("色") or "棕"
            item, instruction = f"{color}色头发", f"把头发染成{color}色"
        else:
            style = next(word for word in HAIR_WORDS if word in t)
            item = _noun_before(t, t.find(style)) + style if style not in ("发型", "头发", "发色") else "新发型"
            instruction = f"把发型换成{item}"
        return Command(user, raw, "garment", gift, item=item, user_text=instruction)

    visual_ok = visual_ok and CHANGE_VERB.search(t)
    garment = _garment_item(t).lstrip("的") if visual_ok and _garment_item(t) else None
    if visual_ok and garment or visual_ok and (re.search(r"(穿|换上|换成|换个|换身|换套|换件)", t) and t.endswith("装")):
        if garment is None:
            match = re.search(r"(?:穿|换上|换成|换个|换身|换套|换件)(?P<item>.{1,8})$", t)
            garment = match.group("item").lstrip("".join(QUANTIFIERS)) if match else None
        item = None if (garment is None or garment.lstrip("".join(QUANTIFIERS)) in GENERIC_GARMENT) else garment
        return Command(user, raw, "garment", gift, item=item, user_text=garment_text(item) if item else None)

    if not QUESTION_TAIL.search(t):
        action_words = [w for words, _ in ACTIONS for w in words]
        for verb in OBJECT_VERBS:
            pos = t.find(verb)
            if pos >= 0 and verb.startswith("来") and any(w in t for w in action_words):
                continue
            if pos >= 0:
                item = _noun_after(t, pos + len(verb))
                if item and verb in ("吃", "喝") and item.startswith("口") and len(item) > 1:
                    item = item[1:]  # 喝口水 / 吃口饭: 口 is a measure word here, not 口水
                if item and not VAGUE_ITEM.match(item):
                    hint = "吃" if verb.startswith("吃") else "喝" if verb.startswith("喝") else ""
                    return Command(user, raw, "object", gift, item=item, user_text=prop_scene(item, hint),
                                   action_text=interaction_for(item, hint))

    if len(t) <= 8 and re.match(r"^(带我们|带我)?(去|到)(?P<item>.{1,6}?)(吧|呀|啊|看看|玩)?$", t):
        item = re.match(r"^(带我们|带我)?(去|到)(?P<item>.{1,6}?)(吧|呀|啊|看看|玩)?$", t).group("item")
        return Command(user, raw, "background", gift, item=item, user_text=f"把背景换成{item}")

    for words, action in ACTIONS:
        if any(word in t for word in words):
            return Command(user, raw, "action", gift, action_text=action)

    return Command(user, raw, "chat", gift)
