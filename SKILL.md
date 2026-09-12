---
name: bilibili-video-notes
description: 把 B站（Bilibili）视频转成结构化 Markdown 笔记。工作流：解析 BV号/链接 → 取元数据、简介、字幕（有官方字幕则直接用、跳过 ASR）、音频 → 判断内容类型 → 套用笔记模板 → 输出 .md 笔记。只要用户给出 B站链接（bilibili.com/video/BV... 或 b23.tv 短链），或提到"整理B站视频""转录B站""生成B站笔记""视频总结/逐字稿"，就使用本技能；即便没说"skill"也应触发。也支持用户已自备音频/字幕、只需生成笔记的场景。
---

# B站视频笔记生成器

把一条 B站视频链接，变成一份类型匹配、可直接归档的 Markdown 笔记。转录层复用既有 DashScope ASR，笔记层复用既有模板。

> 用户看的视频**部分有字幕、部分没有**——本技能用「字幕三级兜底」自动应对：有字幕优先当官方转录用（零成本、跳过 ASR），没有才走 ASR。

## 何时用本技能

- 用户给出 B站视频链接（`https://www.bilibili.com/video/BV...` 或 `b23.tv` 短链），想要笔记/总结/逐字稿；
- 用户已有音频文件或字幕/转录文本，想按类型整理成结构化笔记；
- 用户想批量整理某 UP 主的多期视频。

## 总体流程（五步）

1. **取数据** —— `fetch_bilibili.py` 解析 BV号/链接，拿到标题、UP主、简介、时长、字幕（有则下载）、音频（默认下载）。
2. **得转录** —— 有官方字幕则直接用（已写入 `transcript.txt`，跳过 ASR）；否则下载音频并 `transcribe.py` 上传 DashScope 转录。
3. **判类型** —— 据 UP主 + 简介 + 转录开头选模板（见第 3 步）。
4. **写笔记** —— 读模板，用转录 + 简介逐块填充。**⚠️ 此步开始前必须显式与用户确认，不得默认自动生成。**
5. **存文件** —— 每期独立目录 `./_work/<标题>/`，保存笔记 + 逐字稿 + 元数据。

---

## 第 1 步：取数据

```bash
python scripts/fetch_bilibili.py "<BV号或B站链接>" --out ./_work
```

脚本自动建 `./_work/<标题>/` 子目录（UP主信息存进 `meta.json`，不进目录名），产出：

- `meta.json` —— 结构化元数据（`title` / `owner`(UP主) / `pub_date` / `duration_sec` / `audio_path`(本地音频) / `bvid` / `has_official_transcript`）
- `shownotes.md` —— 视频简介 `desc`（供第 4 步当背景，等价于播客 shownotes）
- `transcript.txt` —— **有字幕时** = 字幕纯文本（官方转录等价物，第 2 步直接用，跳过 ASR）
- `subtitle.srt` —— 字幕带时间戳（有字幕时）
- `audio.m4a` —— 音频（默认下载；ffmpeg 已自带于项目 `bin/`，自动重封装为标准 m4a，无需系统安装）

**字幕三级兜底**（脚本自动逐级尝试，前一级成功即停）：
1. **player 接口字幕**：匿名直连 `api.bilibili.com/x/player/wbi/v2` 读 `data.subtitle.list`（AI 字幕优先，其次 zh-CN）—— 最准、零成本。
   - ⚠️ **不要用** `bilibili_api.Video.get_subtitle()`：实测空 Credential 下抛 `CredentialNoSessdataException`（"Credential 类未提供 sessdata"），匿名环境永远命中不了，会白白掉到付费 ASR。需要登录态字幕时设环境变量 `BILIBILI_SESSDATA`。
2. **yt-dlp 字幕**（`--write-subs --write-auto-subs`）—— 兜底。
   - 前提：yt-dlp 必须能被找到（PATH / venv 的 Scripts / 项目 `bin/`）；找不到会**静默跳过**，直接掉到 ASR。
   - ⚠️ 脚本**只处理 yt-dlp 自己产出的 `ydl*` 前缀文件**。切勿写成"匹配目录里任意 `.srt`/`.vtt`"——那会删掉已生成的字幕，并把它的文本误报成"yt-dlp 命中"（曾因此误判为"拿到官方字幕"）。
   - 想先确认有没有字幕轨、避免白花 ASR：`yt-dlp --list-subs --skip-download <url>`；只列出 `danmaku`（弹幕）即**无字幕轨**。
3. **均无** → 标 `has_official_transcript=false`，第 2 步走 ASR。

> 本脚本只处理**公开视频**。番剧/会员/需登录的内容拿不到音频流，脚本提示并留空 `audio_path`；此时若无字幕则无法出转录稿，请让用户改用官方导出。加 `--no-audio` 可只取元数据+字幕。

---

## 第 2 步：得转录（分层兜底，务必拿到真逐字稿）

**核心原则：笔记必须基于真转录稿，不能只靠简介推断。**

**① 官方字幕**：`meta.json` 里 `has_official_transcript` 为真时，第 1 步已写好 `<标题>-transcript.txt`，**直接用、跳过 ASR**（最准、零成本）。

**② 无字幕 → 云端 ASR（上传本地音频到 DashScope）**：B站音频直链带 referer + 时效签名，云端公网 ASR 拉不到；所以先下载到本地 `audio.m4a`，再用 `transcribe.py` 的**本地上传分支**——经 DashScope `getPolicy` 拿临时凭证 → 直传 OSS → 得 `oss://` URL → 提交 ASR 时带 `X-DashScope-OssResourceResolve: enable`。说话人分离、免费额度、自动降级全部继承：

```bash
python scripts/transcribe.py --from-meta "./_work/<标题>" --out "./_work/<标题>" --fallback funasr,paraformer,qwen --diarize --speaker-count 2 --language zh
# 新加坡账号加 --region intl
```

> `--from-meta "./_work/<标题>"` 让脚本自读该期 `meta.json` 的 `audio_path`（本地音频优先），无需手动传路径。

### ⚠️ 模型选择铁律（按说话人数决定，不要默认 qwen）

| 场景 | 后端 | 原因 |
|---|---|---|
| **对谈 / 多人**（≥2 说话人） | **`funasr` + `--diarize`**（或 `--fallback funasr,paraformer,qwen`） | funasr/paraformer 支持说话人分离，能给每句标 `【说话人N】` |
| **单人**口播 / 独白 | `qwen`（默认，便宜、长音频友好） | 无需区分说话人 |
| 省钱 / 想用免费额度 | `paraformer` + `--diarize` | 每月自动续 10h 免费额度，支持分离+热词，精度略弱 |

- 对谈视频且音频 ≤2 小时：默认开 `--diarize --speaker-count 2`。
- `>2` 小时：先说明取舍再执行。
- `--fallback` 是某后端额度用尽/欠费时自动换下一个；**对谈场景 fallback 链必须把 qwen 放最后**。

产物：本期目录内 `transcript.txt`（纯文本）与 `transcript.srt`（带时间戳）。

> **句子碎片自动合并（默认开启）**：ASR 后端按停顿+句末点切句，会把 `a.m.`/`p.m.` 这类缩写误拆成 `a.`/`m.`/`p.`/`m.` 四段。`transcribe.py` 默认后置合并——单字母缩写碎片（`a.`/`p.`/`m.`）、已知缩写（`Dr.`/`Mr.`/`etc.`/`e.g.` 等）及小间隔续写片段并回上一句。若发现过度合并，加 `--no-merge` 重跑。

> ⚠️ 若都失败（无字幕 + 无本地音频/无 Key），**不要**用简介硬凑"逐字稿笔记"。如实告知用户没拿到转录，并标注"整理依据：仅简介（未转录）"。

### 转录后必做：专名校正（别跳过）

ASR 对专有名词错字率高。动笔前先用 **`shownotes.md`（简介专名通常较准）反查转录稿**，批量校正听错专名；有官方字幕（第①级）时可略过。简介/字幕查不到、但音频反复出现的词，按上下文与通行写法判断；仍拿不准的如实写"节目口述，未点明具体名称"，不要脑补。

---

## 第 3 步：判断内容类型并选模板

看 `owner`(UP主) + `shownotes.md`(简介) + 转录前 ~500 字，选最贴切类型：

| 类型 | 触发信号 | 模板文件 |
|---|---|---|
| **财经/投资** | 宏观、股市、利率、基金、ETF、财报、投资策略 | `templates/finance.md`（含子分支：行情/研报型 与 理念/对谈型） |
| **科技/商业/创投** | 产品、创业、AI、行业分析、公司战略、融资 | `templates/tech-business.md` |
| **人物/访谈/对谈** | 以嘉宾经历、观点、故事为主，弱结论强叙事 | `templates/interview.md` |
| **知识/科普/文史** | 讲解某学科、历史、概念、方法论 | `templates/knowledge.md` |
| **通用/其它** | 以上都不贴切 | `templates/general.md` |

类型明显就直接用；**两类之间拿不准，用一句话问用户确认一次**，别硬猜。用户也可指定"就用财经模板"。

---

## 第 4 步：写笔记（⚠️ 必须先确认）

> **生成笔记是可选步骤，不是默认动作。** 进入本步前必须显式问用户"要不要出笔记 / 用哪个模板"。用户说"不用""跳过""先不生成"即停止，只交付转录稿（transcript.txt / .srt）。

确认后，**由当前 agent 直接按模板写即可**（Claude Code / Codex / WorkBuddy / Cursor 等主流 AI Agent 都能胜任，这是推荐路径，且**不依赖任何 LLM API**）。
仅在用户没有可用 Agent、或想离线批量生成时，才用备选 `scripts/make_notes.py`（配任意 OpenAI 兼容 LLM）或 `--dump-prompt`（导出提示词粘给网页版 AI）。

1. `view` 选定模板文件，按其结构和逐块说明填写。
2. 每块都**输出填好的真实内容**，不用"详见原文""同上"占位，不留模板方括号说明。
3. 文件开头加 YAML front-matter：

```yaml
---
节目: <UP主名>
单集: <title>
发布日期: <pub_date>
时长: <duration 分钟>
链接: <B站链接>
类型: <财经/科技商业/人物访谈/知识科普/通用>
整理日期: <今天>
---
```

4. **财经类**：提到股票/基金/ETF 只写名称、不附代码；图表须逐一解读（纯音频转录拿不到画面——笔记里对画面图表标注"未含画面"，可后续人工补）。
5. 忠实于音频，**不要脑补**主讲人没说的判断或数据；模板要求"若未涉及则如实标注"处，照实写"本集未涉及"。

---

## 第 5 步：输出

确认生成笔记后，在本期 `./_work/<标题>/` 内保存**两个成品文件**：

1. `<标题>-笔记.md`
2. `<标题>-transcript.txt` —— 将同目录 `transcript.txt` 原地重命名为此文件名（有说话人分离则含 `【说话人N】`），不复制、不另存第二份。

两个都用 `present_files` 交给用户（若可用）；否则告诉用户本期目录内的文件路径。

---

## 依赖

```bash
# 用项目自带 managed venv（推荐）
<venv>/Scripts/python.exe -m pip install bilibili-api-python aiohttp yt-dlp
```

- **取数**：`bilibili-api-python`（元数据 `get_info` + 音频直链 `get_download_url`，匿名可用）、`aiohttp`（下载音频）、`yt-dlp`（字幕二级兜底，可选）
  - ⚠️ 字幕**不**走 `bilibili_api.Video.get_subtitle()`（空 Credential 必抛 `CredentialNoSessdataException`），改匿名直连 player 接口。
  - ⚠️ 许可：`bilibili-api-python` 是 **GPL-3.0-or-later**，import 即构成衍生作品，故本仓库以 **GPL-3.0** 分发（见根目录 `LICENSE`）。
- **转录**：`DASHSCOPE_API_KEY`（阿里云百炼）
- **写笔记**：`LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL`（任意 OpenAI 兼容，可选；agent 自身也能写）

## 已自备音频 / 字幕的情况

- 直接给音频：跳过第 1 步，把音频放 `./_work/<标题>/<标题>-audio.m4a`，写 `meta.json`（`audio_path` 指向它），从第 2 步开始。
- 直接给字幕/转录文本：跳过第 1–2 步，文本存为 `<标题>-transcript.txt`，从第 3 步选模板开始。

## 批量整理

用户给多条 BV/链接时，对每条依次跑完整流程，每期一个 md 文件，最后可另生成 `索引.md` 汇总各期标题、类型、一句话主旨和文件名。
