# 方案：B站视频 → 结构化笔记（bilibili-video-notes）

> 状态：方案阶段（待确认后落地功能代码）
> 目标：独立新项目，把一条 B站视频链接变成一份类型匹配、可直接归档的 Markdown 笔记。
> 与小宇宙 skill `xiaoyuzhou-podcast-notes` 的关系：**取数层不同，转录层与笔记层 100% 复用**。

---

## 1. 项目定位与触发

- 触发：用户给 B站视频链接（`https://www.bilibili.com/video/BV...` 或 `b23.tv` 短链），或说"整理B站视频 / 转录B站 / 生成B站笔记 / 视频总结"。
- 与小宇宙 skill 平行：第 1 步（取数）换成 B站适配；第 2 步（转录）复用现有 ASR；第 3–5 步（判类型/写笔记/存文件）复用模板与流程。

## 2. 目录结构

```
D:\My AI Projects\skills\bilibili-video-notes\
├── SKILL.md                  # 触发词 + 五步流程（改写自播客 skill）
├── scripts/
│   ├── fetch_bilibili.py      # 新建：bilibili-api 封装 → meta.json + 字幕 + 音频
│   └── transcribe.py          # 复用：从播客 skill 拷入，补「本地文件上传 DashScope」分支
├── templates/                 # 复用：直接拷 finance/tech-business/interview/knowledge/general
└── _work/                     # 每期独立目录：<UP主>-<标题>/
```

## 3. 数据流（五步，对齐播客 skill）

1. **取数** `fetch_bilibili.py`：BV号 → 标题/UP主/简介/时长/字幕/音频直链 → 下载音频 → 写 meta.json + 字幕文件。
2. **得转录**：字幕三级兜底（见 §4）→ 有字幕直接用（=官方转录等价物，跳过 ASR）；无字幕才走 `transcribe.py` 上传分支。
3. **判类型**：UP主 + 简介 + 转录开头 → 选模板（逻辑复用）。
4. **写笔记**：读模板逐块填充（复用）。
5. **存文件**：每期独立目录，产出 `<UP主>-<标题>-笔记.md` 与 `<UP主>-<标题>-逐字稿.txt`。

## 4. 取数层详细设计（fetch_bilibili.py，基于 bilibili-api）

依赖（GitHub 高赞、成熟）：
- `bilibili-api-python`（Nemo2011 维护，内置 WBI 签名、BV/AV 互转、字幕接口、Cookie 刷新、异步）
- `aiohttp` 或 `httpx`（请求后端）
- `ffmpeg`（系统安装，音频重封装）
- 可选 `yt-dlp`（字幕二级兜底）

输入：`BV号` / `b23.tv` 短链 / `视频链接` / `UID`（UID 用于批量某 UP 主系列）。

步骤：
- a. `Video(bvid=...)` → `get_info()` 取 `title` / `owner.name`(UP主) / `pubdate` / `duration` / `desc`(简介) / `stat`。
- b. `get_subtitle()` 取字幕列表 → 筛选 `zh-CN` / `ai_subtitle` / 本地字幕 → 下载字幕 json → 转纯文本 `official_transcript.txt` + 带时间戳 `subtitle.srt`。**字幕即"官方转录"等价物，优先级最高、零成本。**
- c. 下载音频：`get_download_url()` 取 dash 音频分支（最高音质音频流）→ 落盘 `audio.m4s` → `ffmpeg -i audio.m4s -c copy audio.m4a`（DashScope 友好格式）。
- d. 组装 `meta.json`（schema 见 §5）。

**字幕三级兜底顺序**（借鉴 `bilibili-analyzer` 的 API→yt-dlp→ASR 思路，末级换成我们的 DashScope）：
1. `bilibili-api` 字幕（AI / CC）→ 直接用
2. `yt-dlp --write-subs --skip-download`（当①为空）→ 用其字幕
3. 均无 → 标 `has_official_transcript=false`，走 ASR 上传分支

**风控**：公开视频无需登录；番剧/高码率需 `Credential(SESSDATA=...)`；请求带真实 `User-Agent` + `Referer: https://www.bilibili.com`；翻页/批量加 `sleep` 限流。

## 5. meta.json schema（对齐 transcribe.py 的 --from-meta）

```json
{
  "eid": "<bvid>",
  "url": "<B站链接>",
  "title": "单集标题",
  "podcast_title": "<UP主名>",        // 复用字段名，语义改为 UP主
  "hosts": ["<UP主>"],
  "guest_hints": [],
  "pub_date": "2026-...",
  "duration_sec": 1234,
  "audio_url": "",                     // 公网直链（B站带签名/referer 不稳，通常空）
  "audio_path": "<绝对路径>/audio.m4a", // 本地文件，优先使用
  "has_official_transcript": true,
  "source": "bilibili"
}
```

`transcribe.py` 改动点：`--from-meta` 时**优先读 `audio_path`**；有值走新增「上传 DashScope」分支；否则回退 `audio_url` 公网分支。

## 6. 转录层复用 + 桥接（transcribe.py）

- 原样拷入 `scripts/transcribe.py`（qwen / funasr / paraformer / api 四后端全保留，说话人分离、免费额度、自动降级 `--fallback` 原样继承）。
- **新增「本地文件上传」分支**（解决 B站直链 referer/时效签名 → 云端 `file_url` ASR 拉不到的问题）：
  - `POST https://dashscope.aliyuncs.com/api/v1/files`（`Authorization: Bearer`，multipart）→ 拿 `file_id`。
  - 提交 ASR 任务时 `input.file_ids=[file_id]`（qwen3-asr-flash-filetrans / fun-asr / paraformer-v2 均支持；以 DashScope 最新文档为准，`file_urls` 与 `file_ids` 二选一）。
  - 上传分支同时支持 `--diarize` / `--vocabulary-id`，说话人分离与热词原样继承。
- 质量自检、专名校正（用视频**简介 desc** 替代 shownotes 反查错字）复用。

## 7. 笔记层复用（templates/）

直接拷 5 个模板：`finance.md` / `tech-business.md` / `interview.md` / `knowledge.md` / `general.md`。判类型逻辑复用（看 UP主 + 简介 + 转录开头）。

## 8. SKILL.md

- 触发词改为 B站链接 / "整理B站视频" 等。
- 流程同播客 skill，第 1 步换 `fetch_bilibili.py`；专名校正说明里把"shownotes"改为"视频简介 / 字幕"。

## 9. 依赖与安装

- `pip install bilibili-api-python aiohttp`（或 httpx）
- 系统安装 `ffmpeg`（音频重封装 m4s→m4a）
- 复用现有 `DASHSCOPE_API_KEY`（阿里云百炼）
- 可选 `pip install yt-dlp`（字幕二级兜底）
- ⚠️ 许可证：`bilibili-api` 为 GPLv3。纯自用/内部无分发风险；若未来要闭源发布需注意传染性。

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| B站直链 referer/时效 → 云端 ASR 拉不到 | 下载到本地 + 上传 DashScope 分支 |
| m4s 格式不被 ASR 接受 | ffmpeg 重封装 m4a |
| 字幕缺失 → 走 ASR 成本 | 三级兜底，优先字幕 |
| 风控/限流/番剧需登录 | Credential + UA + Referer + sleep |
| 画面信息（PPT/图表）漏抓 | 纯音频转录固有限制，笔记注明"未含画面"，可后续人工补 |
| GPLv3 传染性 | 仅自用；发布前评估 |

## 11. 验证计划

- **单期冒烟**：选一个公开知识区 BV（有 AI 字幕）→ fetch → 确认 meta/字幕/音频 → 有字幕跳过 ASR → 套 finance 模板出笔记，对比播客 skill 格式一致。
- **无字幕期**：选一个无字幕视频 → 走上传 ASR 分支 → 确认 qwen/funasr 能出稿 + 说话人分离。
- **批量**：某 UP 主系列（UID）→ 批量 fetch + 笔记 + 生成 `索引.md`。

## 12. 落地步骤（确认后执行）

1. 建 `bilibili-video-notes/` 目录结构。
2. 写 `fetch_bilibili.py`（bilibili-api 封装 + 字幕三级兜底 + 音频下载 + meta.json）。
3. 拷 `transcribe.py` 并补「本地文件上传 DashScope」分支。
4. 拷 `templates/*`。
5. 写 `SKILL.md`。
6. 单期冒烟验证（§11）。
