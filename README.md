# bilibili-video-notes

把一条 B站视频链接，变成**可用的字幕** + **结构化 Markdown 笔记**。

两条主线：

1. **拿字幕**（主）：优先白嫖官方/AI 字幕，拿不到才付费走 ASR。
2. **出笔记**（辅）：由**主流 AI Agent 直接完成**（推荐），或用任意 OpenAI 兼容模型生成。

> 笔记这一步**不强制依赖任何 LLM API**。把 `templates/` 里的模板和逐字稿一起交给
> Claude Code / Codex / WorkBuddy / Cursor 等 Agent，让它直接写就行——这也是推荐路径。

---

## 安装

```bash
python -m venv .venv && ./.venv/Scripts/pip install -r requirements.txt
```

- **核心依赖 `bilibili-api-python`**（取元数据 `get_info` + 音频直链 `get_download_url`，匿名空 Credential 即可用）。
- `yt-dlp`：字幕二级兜底。
- `aiohttp`：下载音频流。
- `imageio-ffmpeg`：可选，免系统安装的 ffmpeg。
- 详见下方「依赖与许可」关于 `bilibili-api-python` 的许可提示。

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

**备选 A：脚本调 LLM（任意 OpenAI 兼容接口）**

```bash
export LLM_API_BASE="https://api.deepseek.com/v1"
export LLM_API_KEY="xxx"
export LLM_MODEL="deepseek-chat"
python scripts/make_notes.py --work "./_work/<标题>" --type auto
```

**备选 B：完全免 Key**

```bash
python scripts/make_notes.py --work "./_work/<标题>" --type knowledge --dump-prompt
```

脚本导出提示词到 `note_prompt.md`，整段粘给任意网页版 AI 即可。

模板：`templates/` 下 `finance` / `tech-business` / `interview` / `knowledge` / `general`。

---

## 环境变量

| 变量 | 用途 | 必需 |
|---|---|---|
| `DASHSCOPE_API_KEY` | ASR（第 2 步，无字幕时才需要） | 视情况 |
| `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` | 笔记生成（第 3 步备选 A） | 否 |
| `BILIBILI_SESSDATA` | 登录态字幕 | 否 |

---

## 依赖与许可

- 本工具**依赖 `bilibili-api-python`**（GPL-3.0-or-later）做元数据与音频直链。由于代码层面 `import` 了它，
  按 GPL 这属于**衍生作品**，因此本仓库以 **GPL-3.0** 分发（见根目录 `LICENSE`）。
- **许可已统一为 GPL-3.0**（Copyright (C) 2026 weisi-gu）。早前曾尝试"去依赖保 MIT"，后恢复该依赖，
  故 LICENSE 同步改为 GPL-3.0 以消除冲突。
- 字幕读取**不**走 `bilibili_api.Video.get_subtitle()`（空 Credential 下必抛
  `CredentialNoSessdataException`），而是匿名直连公开 player 接口，因此字幕路径的实现本身独立于该 GPL 库——
  但整体仓库因 import 关系仍整体适用 GPL-3.0。

---

## 已知限制

- **官方字幕命中率很低（重要预期）**：实测扫描 100+ 个视频（全站热门榜 + 知识/科技/动物圈分区），
  **零字幕轨**——B站绝大多数视频没有上传字幕，实际主要靠第 ③ 级 ASR 兜底。
  字幕三级兜底是为了"有就白嫖、没有也不阻塞"，**不要假设多数视频能免 ASR**。
  花 ASR 的钱之前先用 `yt-dlp --list-subs --skip-download <url>` 确认。
- 只处理**公开视频**；番剧/会员/需登录内容拿不到音频流。
- 纯音频转录拿不到画面，笔记里图表会标「未含画面」。
- 说话人分离仅 `funasr`/`paraformer` 支持，且建议音频 ≤2 小时。
