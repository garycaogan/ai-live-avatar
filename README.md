# AI 实时直播间

一个让 AI 数字人自己开直播的小项目。观众发弹幕，程序先过滤、再排队，等她说完一句再交下一条：聊天她就回一句，换衣服就换一整套画面，比心、挥手这类就做动作。画面、声音和回复都由 [Vidu S2 实时数字人](https://platform.vidu.cn/vidu-stream/doc)生成，这个项目负责中间这一层：弹幕过滤和调度、角色和衣柜、直播间叠层、本地工作台，以及交给 OBS 或者直接推流。

这是 Gary（曹淦）做的 AI 实时直播测试，代码按 MIT 开源。测试时用的角色形象、人设、音色、素材库和录屏都是作者自己的角色资产，没有放进这个仓库，你需要用自己的。

## 现在能做什么

| 已经能做 | 还做不到 |
| --- | --- |
| 聊天接话，人设和开场白可以自己写 | 换背景、吃喝和拿东西（默认关闭，原因见下） |
| 换衣服（衣柜里预先做好的整套衣服，换一次大约 6 秒） | 唱歌 |
| 小动作：比心、挥手、眨眼等，关键词可以在工作台改 | 站起来、全身画面 |
| 没人说话时按角色自己的话题池暖场，一轮说完才重复 | 大幅度跳舞，坐着只能晃一晃 |
| 低俗、引流弹幕自动过滤，礼物不插队 | 带货 |
| Vidu 中途挂断后自动重新开播（要手动打开） | 跨场次记住老观众 |

换背景和拿道具默认是关着的。换衣服是拿一整张照片重开一次会话，衣服就在照片里，不会走样；换背景和道具却是在直播进行中让 Vidu 照着一张背景图或道具图重画当前画面，这张图里没有她身上的衣服。一次公开测试里连续换了两次背景，她身上的衣服就被画走样了，不适合再放进公开直播，所以现在观众说“换个背景”“吃个香蕉”，只会按聊天回一句。只在私下测试时想打开，在 `config.json` 里写 `{"safety": {"live_visual_edits": true}}`，别在公开直播里开。

自动重新开播用于长时间直播：Vidu 偶尔会中途挂断，打开后程序会隔 3 秒自己再开一场，一小时最多 6 次，积分不足和内容审核挂断不会重来。打开方式是 `curl -s -X POST http://127.0.0.1:28900/api/settings -H 'Content-Type: application/json' -d '{"auto_recover":true}'`，或者在 `config.json` 里写 `{"schedule": {"auto_recover": true}}`。

弹幕写法对效果影响很大。问主播身边的小事、用衣柜里的衣服名字换装、一次只要一个动作，效果最好；一句话塞好几件事、问实时新闻和天气、让她唱歌放歌，效果都不好。

## 需要准备

- macOS 或 Linux，Python 3.11 以上（作者用 3.13），ffmpeg（要带 libx264 和 aac）。
- Vidu 开放平台的 API Key（`vda_` 开头），账号里要有积分。实时数字人按时长计费，官方价每秒 1.5 积分，1 积分 0.03125 元，一小时大约 169 元。
- 可选：[Evolink](https://evolink.ai) 的 API Key，用来生成形象图、衣服、道具和背景，以及克隆声音时上传音频。
- 可选：OBS 30 以上，用来加真人摄像头、叠层和推流。
- 可选：YouTube 推流码，用内置推流时才需要。

密钥只放在本机文件里，服务端读取，不会发到浏览器。默认路径如下，也可以在 `config.json` 里改：

```bash
mkdir -p ~/.deepsea/vidu ~/.deepsea/evolink && chmod 700 ~/.deepsea/vidu ~/.deepsea/evolink
```

```bash
pbpaste > ~/.deepsea/vidu/api.key && chmod 600 ~/.deepsea/vidu/api.key
```

Evolink 的 Key 同样放到 `~/.deepsea/evolink/api.key`。YouTube 推流码放 `~/.deepsea/youtube/stream.key`，OBS WebSocket 密码放 `~/.deepsea/obs/ws.password`。

## 安装

```bash
git clone https://github.com/garycaogan/ai-live-avatar.git
```

```bash
cd ai-live-avatar && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

舞台页用到阿里云 RTC 的网页 SDK，版权属于阿里云，这里不重新分发，用脚本从官方 CDN 下载并校验：

```bash
./tools/fetch_vendor.sh
```

```bash
cp config.example.json config.json
```

## 启动

```bash
.venv/bin/python server.py
```

服务只监听本机，打开 http://127.0.0.1:28900/workbench 就是工作台。

不想花钱先看效果，可以在直播间页用空跑开播，它不调用 Vidu，只演示过滤、排队和叠层。`tests/dev_mock_stack.py` 会连一个本地模拟的 Vidu，把整个控制流程走一遍。

## 做一个自己的角色

1. 素材页上传一张形象图，或者用 Evolink 生成。底图决定她的长相、穿着、房间和常驻表情：单人、近景、双手放在桌上、表情自然，效果最好。横构图会放进抖音那种竖屏上方的小框里，比例按 1.2:1 裁。
2. 角色页新建角色，选这张图做底图，写名字、人设和开场白。人设里写说话风格就够了，别写示例句子，模型会原样照念。
3. 衣柜页加几套衣服。每套都是一张完整的照片，房间、光线和构图要和底图一致，否则换装时画面会跳。
4. 声音页挑一个系统音色，或者上传 10 到 20 秒的干净人声克隆一个（Vidu 克隆要收费）。
5. 动作页可以改弹幕关键词对应的动作，待机小动作在角色里编辑。

角色资料存在 `assets/avatars/profiles.json`，人设在 `personas/`，这些文件都被 `.gitignore` 排除了，不会被提交。

## 接 OBS

工作台的推流页把输出方式切到 OBS，然后在 OBS 里加浏览器源：

- 只要数字人画面：`http://127.0.0.1:28900/stage?obs=1&role=obs&layer=avatar`
- 只要叠层（透明背景）：`http://127.0.0.1:28900/stage?overlay=1`
- 画面加叠层：`http://127.0.0.1:28900/stage?obs=1&role=obs&layer=all`，宽 1080、高 1920

浏览器源要勾选“通过 OBS 控制音频”。服务只监听本机，所以 OBS 要和服务在同一台电脑上。`tools/obs_control.py` 可以通过 OBS WebSocket 一键搭好场景。

叠层里常驻“AI 实时生成画面”的标识，这是平台对 AI 数字人直播的要求，请保留。

## 在抖音开播

抖音上常见的那种数字人直播间是横屏开播：抖音会把画面放进手机上方的框里，评论显示在框下面。下面这条命令把 OBS 画布设成 1296×1080（1.2:1，和横构图底图一样的比例），新建“抖音直播间”场景：数字人铺满画面，说明卡在上方两个角，字幕在下方，左下角是真人摄像头。

```bash
.venv/bin/python tools/obs_control.py setup douyin --camera FaceTime
```

`--camera` 后面写摄像头名字里的一段，不写就用找到的第一个真实摄像头。第一次运行时 macOS 会问 OBS 能不能用摄像头，允许以后再跑一次，裁切才准。竖屏场景随时可以用 `setup` 换回来，两个场景共用同一个数字人图层，不会抢入会名额。

画面交给抖音直播伴侣有两种接法。一种是开 OBS 虚拟摄像头（`tools/obs_control.py vcam start`，第一次要在系统设置里允许 OBS 的摄像头扩展），然后在直播伴侣横屏模式里添加摄像头素材，选 OBS Virtual Camera，铺满画面。另一种是在直播伴侣里添加窗口素材，抓 OBS 的投影窗口。虚拟摄像头只带画面不带声音，所以“抖音直播间”场景会把数字人的声音同时从电脑播放出来，直播伴侣要能采到这路声音，具体看你那个版本支持哪种采集方式。如果你的账号有第三方推流权限，也可以不用直播伴侣，直接在 OBS 里填推流地址。

抖音的弹幕不会自动进来。要接进来，得走抖音开放平台的直播玩法接口（需要申请），再推到 `/api/danmaku`。这个项目没有内置这部分，也请不要抓取直播间网页。

## 直播间说明卡

画面框左上角和右上角有两张说明卡，写着这场测试能做什么、还做不到什么、弹幕怎么发效果好。内容来自 `assets/stage_panel.json`，没有这个文件时用 `assets/stage_panel.example.json`。复制一份改成你自己的就行，舞台页一分钟内会自动刷新，内置推流的叠层会立刻用新内容。`outfit_hint` 可以按角色写不同的换装提示。

## 弹幕从哪来

工作台直播间页可以手动发模拟弹幕。真实弹幕推到这个接口：

```bash
curl -s -X POST http://127.0.0.1:28900/api/danmaku -H 'Content-Type: application/json' -d '{"user":"观众A","text":"换件衣服","gift":0}'
```

`tools/youtube_chat_bridge.js` 是把 YouTube 直播聊天转进来的示例。接国内平台请用平台官方开放的直播互动接口，不要抓取直播间网页。

弹幕先过 `moderation.json` 的过滤词，再由 `live/router.py` 按规则识别成聊天、换装、道具、背景、动作或复原，不调大模型，没有延迟也不花钱。道具和背景在默认设置下按聊天处理，见上面“现在能做什么”。

## 文件

- `server.py`：服务入口和默认配置。
- `live/`：直播间调度、Vidu 实时接口、弹幕识别、衣柜、素材生成、内置推流和工作台接口。
- `static/`：工作台、舞台页和旧版控制台。
- `WORKBENCH_API.md`：工作台用到的全部接口。
- `tests/`：`python -m unittest tests.test_logic tests.test_workbench tests.test_e2e`，用本地模拟的 Vidu，不花钱。

## 许可

代码使用 MIT 许可。阿里云 RTC SDK、Vidu、Evolink 等第三方服务按各自的条款使用。你用这个项目做的角色和直播内容，版权和合规责任在你自己，用真人或者有版权的角色形象之前请先拿到授权。
