# bilibili-video-notes

把一条 B站视频链接，变成**可用的字幕** + **结构化 Markdown 笔记**。

两条主线：

1. **拿字幕**（主）：优先白嫖官方/AI 字幕，拿不到才付费走 ASR。
2. **出笔记**（辅）：交给你的 AI Agent 直接完成（本仓库不内置任何 LLM 调用）。

---

## 安装

```bash
python -m venv .venv && ./.venv/Scripts/pip install -r requirements.txt
```

- **核心依赖 `bilibili-api-python`**（取元数据 `get_info` + 音频直链 `get_download_url`，**二者**匿名空 Credential 即可用）。
- `yt-dlp`：字幕二级兜底。
- `aiohttp`：下载音频流。
- `imageio-ffmpeg`：可选，免系统安装的 ffmpeg。
- 许可：**GPL-3.0**，详见根目录 `LICENSE` 与下方「许可」一节。

### ffmpeg（可选）

ffmpeg **只用于把音频流重封装成标准 `.m4a`**，不是必需项。三者都没有时脚本不会报错，
只把音频流直接存成 `.m4a`（多数 ASR 仍可接受）。

脚本按此顺序探测：`PATH` → 项目 `bin/` → `imageio-ffmpeg`（pip）。

> 仓库**不带 ffmpeg 二进制**：单文件约 164 MB，超过 GitHub 100 MB 硬上限，push 会被拒。
> 请按下面任一方式自行准备。

**Windows**

```powershell
winget install ffmpeg          # 或： choco install ffmpeg
ffmpeg -version                # 验证
```

**macOS**

```bash
brew install ffmpeg
```

**Linux**

```bash
sudo apt install ffmpeg        # Debian/Ubuntu；Fedora 用 sudo dnf install ffmpeg
```

**免系统安装（跨平台）**

```bash
pip install imageio-ffmpeg     # 脚本会自动探测到它自带的静态 ffmpeg
```

---

## 用法

### 第 1 步：取数据（元数据 + 字幕 + 音频）

```bash
python scripts/fetch_bilibili.py "https://www.bilibili.com/video/BV1xxxxx" --out ./_work
python scripts/fetch_bilibili.py "BV1xxxxx" --out ./_work --no-audio   # 只要字幕
```

产出 `./_work/<标题>/`：`meta.json`、`shownotes.md`、`transcript.txt`、`subtitle.srt`、`audio.m4a`。

**字幕三级兜底**（前一级命中即停）：

| 级别 | 方式 | 成本 |
|---|---|---|
| ① | 匿名直连 B站 player 接口读 `subtitle.list` | 免费 |
| ② | `yt-dlp --write-subs --write-auto-subs` | 免费 |
| ③ | 均无 → 下载音频走 ASR | 付费 |

> ⚠️ ①**不要**用 `bilibili_api.Video.get_subtitle()`：空 Credential 下会抛
> `CredentialNoSessdataException`，匿名环境永远拿不到字幕。本脚本已改用公开 player 接口。
> 需要登录态字幕时设 `BILIBILI_SESSDATA`。

**怎么判断某视频有没有官方字幕？**

```bash
yt-dlp --list-subs --skip-download "https://www.bilibili.com/video/BV1xxxxx"
```

只列出 `danmaku`（弹幕）就说明**没有字幕轨**，只能走 ASR。这条命令能在花 ASR 的钱之前先确认。

### 第 2 步：得转录（仅当第 1 步没拿到字幕）

```bash
export DASHSCOPE_API_KEY="sk-xxx"
python scripts/transcribe.py --from-meta "./_work/<标题>" --out "./_work/<标题>"
```

- 单人口播默认 `qwen`；**对谈/多人请用 `--backend funasr --diarize`**（qwen 不做说话人分离）。
- 产出 `transcript.txt` + `transcript.srt`。

### 第 3 步：出笔记（可选）

**推荐：交给 AI Agent。** 把模板和逐字稿一起给它，例如：

> 读 `templates/knowledge.md` 模板，按它的结构把
> `_work/<标题>/transcript.txt` 整理成一份中文笔记，写到 `_work/<标题>/notes.md`。

主流 Agent（Claude Code / Codex / WorkBuddy / Cursor 等）都能直接完成，还能顺带联网核实代码与图表。

**或：用本仓库的组装脚本。** 把材料拼成一份可直接粘贴的草稿（脚本本身不调用 LLM）：

```bash
python scripts/make_notes.py --work "./_work/<标题>" --type knowledge
```

产出 `note_prompt.md`，整段粘给任意 AI（网页版或 Agent 均可）即生成笔记。
`--type` 必填：`finance` / `tech-business` / `interview` / `knowledge` / `general`。

模板：`templates/` 下 `finance` / `tech-business` / `interview` / `knowledge` / `general`。

---

## 环境变量

| 变量 | 用途 | 必需 |
|---|---|---|
| `DASHSCOPE_API_KEY` | ASR（第 2 步，无字幕时才需要） | 视情况 |
| `BILIBILI_SESSDATA` | 登录态字幕 | 否 |

---

## 许可

本项目以 **GPL-3.0** 分发（Copyright (C) 2026 weisi-gu）。原因是代码依赖
`bilibili-api-python`（GPL-3.0-or-later）——`import` 即构成衍生作品，故整体适用 GPL-3.0。
完整依赖见 `requirements.txt`。

---

## 已知限制

- **官方字幕命中率很低（重要预期）**：实测扫描 100+ 个视频（全站热门榜 + 知识/科技/动物圈分区），
  **零字幕轨**——B站绝大多数视频没有上传字幕，实际主要靠第 ③ 级 ASR 兜底。
  字幕三级兜底是为了"有就白嫖、没有也不阻塞"，**不要假设多数视频能免 ASR**。
  花 ASR 的钱之前先用 `yt-dlp --list-subs --skip-download <url>` 确认。
- 只处理**公开视频**；番剧/会员/需登录内容拿不到音频流。
- 纯音频转录拿不到画面，笔记里图表会标「未含画面」。
- 说话人分离仅 `funasr`/`paraformer` 支持，且建议音频 ≤2 小时。
