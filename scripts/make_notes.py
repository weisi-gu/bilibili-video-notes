#!/usr/bin/env python3
"""
make_notes.py — 把逐字稿 + 模板**组装成一份交给 AI Agent 的笔记草稿**（note_prompt.md）。

本脚本**不调用任何 LLM API**。它只负责把材料拼好，真正的笔记由主流 AI Agent
（Claude Code / Codex / WorkBuddy / Cursor 等）产出——这些 Agent 能直接读
templates/ 下的模板和逐字稿写笔记，还能联网核实代码、读图表，比纯 API 更完整。

两种交给 Agent 的方式（任选其一）：
  1. 直接让 Agent 读文件：把 `_work/<标题>/transcript.txt` + `templates/<类型>.md`
     交给 Agent，让它按模板结构写（推荐，Agent 还能联网核实）。
  2. 本脚本组装：运行后生成 `note_prompt.md`（系统指令 + 元数据 + shownotes + 模板 + 逐字稿），
     整段粘给任意 AI 聊天框即可，无需 API Key。

用法：
  python make_notes.py --work ./_work/<标题> --type knowledge
    --work       含 transcript.txt / meta.json / shownotes.md 的目录
    --type       finance / tech-business / interview / knowledge / general（必填，无 LLM 自动判类型）
    --out        输出路径（默认 <work>/note_prompt.md）
    --transcript 单独指定逐字稿（默认 <work>/transcript.txt，兼容 <标题>-transcript.txt 重命名）
"""
import argparse, json, os, sys

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(SKILL_ROOT, "templates")
TYPES = ["finance", "tech-business", "interview", "knowledge", "general"]


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def resolve(work, std_name, suffix):
    """定位工作目录内的文件：优先标准名，不存在则回退到「<标题>-<std_name>」。

    ⚠️ 为什么需要回退：SKILL.md 第 5 步要求交付时把 transcript.txt 原地重命名为
    `<标题>-transcript.txt`。重命名后再跑本脚本（例如换个模板重出笔记）就会因为找不到
    `transcript.txt` 而报错退出——这是真实的流程断裂。这里兼容重命名前后的两种状态。
    """
    p = os.path.join(work, std_name)
    if os.path.isfile(p):
        return p
    try:
        for f in sorted(os.listdir(work)):
            if f.endswith(suffix):
                return os.path.join(work, f)
    except OSError:
        pass
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="./_work", help="含 transcript.txt/meta.json/shownotes.md 的目录")
    ap.add_argument("--transcript", default=None, help="单独指定逐字稿路径（默认 <work>/transcript.txt）")
    ap.add_argument("--type", required=True, choices=TYPES,
                    help="笔记类型：finance/tech-business/interview/knowledge/general")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tpath = args.transcript or resolve(args.work, "transcript.txt", "-transcript.txt")
    transcript = read(tpath)
    if not transcript.strip():
        sys.exit(f"[错误] 逐字稿为空或不存在：{tpath}"
                 f"（已在 {args.work} 内回退查找 *-transcript.txt；也可用 --transcript 显式指定）")
    meta = {}
    try:
        meta = json.loads(read(os.path.join(args.work, "meta.json")) or "{}")
    except Exception:
        pass
    shownotes = read(resolve(args.work, "shownotes.md", "-shownotes.md"))

    typ = args.type
    print(f"[类型] {typ}")
    template = read(os.path.join(TEMPLATE_DIR, f"{typ}.md"))
    if not template:
        sys.exit(f"[错误] 找不到模板 {typ}.md")

    # 组装提示词（交给 AI Agent 产出笔记，本脚本不调用 LLM）
    meta_txt = "\n".join(f"{k}: {v}" for k, v in meta.items()
                         if k in ("podcast_title", "title", "pub_date", "duration_sec",
                                  "url", "hosts", "guest_hints"))
    system = (
        "你是专业的中文视频/播客笔记整理师。请严格按用户给出的【模板】结构与排版要求，"
        "基于【逐字稿】输出一份详尽的简体中文 Markdown 笔记。要求：\n"
        "1) 忠实于逐字稿，不编造其中没有的内容；模板里要求'若未涉及则如实标注'的地方照实写。\n"
        "2) 保留模板的 ━━━ 分节标题、表格、加粗字段、逻辑链代码块等排版。\n"
        "3) 不要把模板里的说明性文字原样保留，要输出填好的真实内容。\n"
        "4) 从 shownotes、标题、逐字稿识别主播与嘉宾的姓名及背景，填入基本信息。\n"
        "5) 你无法联网，遇到股票/基金/ETF 代码时标注'（代码待核实）'，不要编造代码。\n"
        "6) 只输出最终笔记本身，不要额外解释。"
    )
    user = (f"【元数据】\n{meta_txt}\n\n【shownotes】\n{shownotes[:4000]}\n\n"
            f"【模板】\n{template}\n\n【逐字稿】\n{transcript}")

    out = args.out or os.path.join(args.work, "note_prompt.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("> 把下面【系统指令】和【内容】整段复制，粘贴给任意 AI"
                "（Claude Code / Codex / WorkBuddy / Cursor / ChatGPT / DeepSeek / Gemini 等）即可生成笔记。\n\n"
                "=== 系统指令 ===\n" + system + "\n\n=== 内容 ===\n" + user)
    print(f"[完成] 笔记草稿已写入 {out}（交给任意 AI Agent 即可产出最终笔记，无需 API Key）。")


if __name__ == "__main__":
    main()
