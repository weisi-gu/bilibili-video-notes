#!/usr/bin/env python3
"""
fetch_bilibili.py — 取 B站视频数据：元数据 + 字幕(三级兜底) + 音频下载。
依赖: bilibili-api-python（元数据 get_info + 音频直链 get_download_url，匿名可用）；
      yt-dlp（字幕二级兜底）；可选 ffmpeg（音频重封装 m4a）；aiohttp（下载音频流）。

许可提示: bilibili-api-python 是 GPL-3.0-or-later。本项目若以 pip 安装并 import 它，
          则构成衍生作品，按 GPL 须以 GPL-3.0 分发（与仓库根目录的 MIT LICENSE 冲突）。
          当前代码层面已恢复使用该库；LICENSE 处置见仓库根 README 的「许可」一节。

复用: 产出的 meta.json / transcript.txt / shownotes.md / subtitle.srt 与 transcribe.py、make_notes.py 契约一致。

用法:
  python fetch_bilibili.py "<BV号或B站链接>" --out ./_work
  python fetch_bilibili.py "BV1xx..." --out ./_work --no-audio

产出 (在 ./_work/<标题>/ 下):
  meta.json           结构化元数据（audio_path 优先本地音频；has_official_transcript 标记有无字幕）
  shownotes.md        视频简介 desc（供 make_notes.py 当背景，等价于播客 shownotes）
  transcript.txt      有字幕时=字幕纯文本（=官方转录等价物，跳过 ASR）；无则缺，等 transcribe.py 上传音频生成
  subtitle.srt        字幕带时间戳（有字幕时）
  audio.m4a / audio.m4s  音频（有则下载；无 ffmpeg 时直存 m4s 为 .m4a）
"""
import argparse, os, sys, json, re, time, asyncio, shutil, subprocess, mimetypes
from bilibili_api import video, Credential

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com"


def get_cred():
    """环境变量 BILIBILI_SESSDATA 可注入登录态（仅登录可见字幕/更高风控额度时用）；
    缺省空 Credential（匿名，get_info / get_download_url 均可用）。"""
    sess = os.environ.get("BILIBILI_SESSDATA", "").strip()
    return Credential(sessdata=sess) if sess else Credential()


def sanitize(name):
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name[:80].strip() or "untitled"


def extract_bvid(raw):
    s = (raw or "").strip()
    m = re.search(r"(BV[0-9A-Za-z]+)", s)
    if m:
        return m.group(1)
    if "b23.tv" in s or "bilibili.com" in s:
        import urllib.request
        try:
            req = urllib.request.Request(s, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                final = r.geturl()
            m = re.search(r"(BV[0-9A-Za-z]+)", final)
            if m:
                return m.group(1)
        except Exception:
            pass
    sys.exit(f"[错误] 无法从输入解析 BV号：{raw}")


async def get_info(bvid):
    """bilibili-api 取视频元数据（匿名空 Credential 即可，内部走 web-interface/view）。"""
    try:
        v = video.Video(bvid=bvid, credential=get_cred())
        return await v.get_info()
    except Exception as e:
        sys.exit(f"[错误] bilibili_api 取视频信息失败：{e}"
                 f"（可能是番剧/需登录/视频失效，或受 B站风控 HTTP 412 限流）")


async def get_subtitle(bvid, cid):
    """匿名直连 B站 player 接口读字幕轨。

    ⚠️ 刻意**不**用 bilibili_api 的 Video.get_subtitle()：实测在空 Credential 下会抛
    CredentialNoSessdataException（"Credential 类未提供 sessdata 或者为空"），
    导致匿名环境下永远拿不到官方字幕、白白掉到付费 ASR。
    公开的 player 接口匿名实测返回 code=0，可正常读 data.subtitle.list。
    需要登录态（如仅登录可见的字幕）时设置环境变量 BILIBILI_SESSDATA。
    """
    sess = os.environ.get("BILIBILI_SESSDATA", "").strip()
    for api in (f"https://api.bilibili.com/x/player/wbi/v2?bvid={bvid}&cid={cid}",
                f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}"):
        try:
            d = dl_json(api, cookie=sess)
            if d.get("code") == 0:
                lst = ((d.get("data") or {}).get("subtitle") or {}).get("list") or []
                return {"list": lst}
            print(f"    [字幕] player 接口 code={d.get('code')}：{d.get('message')}")
        except Exception as e:
            print(f"    [字幕] player 接口失败（{api.split('?')[0]}）：{e}")
    return {"list": []}


def pick_subtitle(sub):
    """从字幕轨列表挑最合适的一条。

    ⚠️ ai_type 语义：0 / 缺失 = 人工上传字幕（质量高）；非 0 = AI 生成字幕（质量较差）。
    因此**优先人工字幕**，只在没有人工字幕时才退到 AI 字幕。
    （旧实现把 `if ai_type` 当成"有 AI 字幕就先用"，恰好选反了质量顺序。）
    同类内再按中文轨优先。
    """
    lst = (sub or {}).get("list") or []
    if not lst:
        return None
    human = [s for s in lst if not s.get("ai_type")]
    pool = human or lst          # 有人工字幕就只在人工里挑，否则退到 AI 字幕
    for lan in ("zh-CN", "zh", "cn", "chi"):
        for s in pool:
            if s.get("lan") == lan:
                return s
    return pool[0]


def dl_json(url, cookie=""):
    import urllib.request
    h = {"User-Agent": UA, "Referer": REFERER}
    if cookie:
        h["Cookie"] = f"SESSDATA={cookie}"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _abs_url(u):
    """字幕地址可能是协议相对形式（//host/path），补全为 https 才能下载。"""
    u = (u or "").strip()
    return "https:" + u if u.startswith("//") else u


def sub_to_text_srt(body):
    def ts(sec):
        h = int(sec // 3600); m = int(sec % 3600 // 60)
        s = int(sec % 60); ms = int((sec - s) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    lines, srt = [], []
    for i, b in enumerate(body, 1):
        c = (b.get("content") or "").strip()
        if not c:
            continue
        lines.append(c)
        srt.append(f"{i}\n{ts(b.get('from', 0))} --> {ts(b.get('to', 0))}\n{c}\n")
    return "\n".join(lines), "\n".join(srt)


def parse_vtt_or_srt(path):
    txt = []
    for line in open(path, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if not line:
            continue
        if line.isdigit():
            continue
        if "-->" in line:
            continue
        txt.append(line)
    return "\n".join(txt)


def _find_yt_dlp():
    # PATH 优先；回退当前 python 同目录（通常是 venv 的 Scripts/bin）的兄弟 yt-dlp[.exe]；
    # 再回退项目自带 bin/。避免 venv 安装的 yt-dlp 不在 PATH 时被静默跳过（之前一直静默失效）。
    yt = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if yt:
        return yt
    sib = os.path.dirname(sys.executable)
    for name in ("yt-dlp.exe", "yt-dlp"):
        cand = os.path.join(sib, name)
        if os.path.isfile(cand):
            return cand
    proj_bin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
    for name in ("yt-dlp.exe", "yt-dlp"):
        cand = os.path.join(proj_bin, name)
        if os.path.isfile(cand):
            return cand
    return None


def yt_dlp_subs(bvid, out_dir):
    yt = _find_yt_dlp()
    if not yt:
        print("    [yt-dlp] 未找到可执行（PATH/venv/项目 bin 均无），跳过二级兜底")
        return None
    try:
        subprocess.run([yt, "--write-subs", "--write-auto-subs", "--sub-langs", "zh-CN",
                        "--skip-download", "-o", os.path.join(out_dir, "ydl"),
                        f"https://www.bilibili.com/video/{bvid}"],
                       capture_output=True, timeout=180)
        # 只处理本次 yt-dlp 自己产出的文件（-o 模板前缀为 "ydl"）。
        # ⚠️ 绝不能匹配目录里任意 .srt/.vtt：那会把用户已生成/已交付的字幕当临时文件删掉。
        for f in os.listdir(out_dir):
            if not f.startswith("ydl"):
                continue
            if f.endswith(".vtt") or f.endswith(".srt"):
                p = os.path.join(out_dir, f)
                txt = parse_vtt_or_srt(p)
                try:
                    os.remove(p)  # yt-dlp 临时字幕用完即删，避免残留无标题前缀的散文件
                except OSError:
                    pass
                return txt
    except Exception as e:
        print(f"    [yt-dlp] 兜底失败：{e}")
    return None


async def get_audio_url(bvid):
    """用 bilibili-api 的 Video.get_download_url() 取最佳音频直链（替代 yt-dlp 风控 412 路径）。

    ⚠️ 多 P 视频用 page_index=0 取第 1 P，与原先 get_download_url(page_index=0) 一致。
    DASH 流优先取 audio 列表里码率最高的一条 base_url；FLV 流取 durl[0].url。
    """
    try:
        v = video.Video(bvid=bvid, credential=get_cred())
        info = await v.get_download_url(page_index=0)
    except Exception as e:
        print(f"    [音频] bilibili_api 取直链失败：{e}（番剧/需登录？或 B站风控 412）")
        return None
    try:
        # bilibili_api v17 把 playurl 字段（dash/durl）直接铺在返回 dict 顶层，
        # 不再提供 is_dash/is_flv 布尔键，故直接判断 dash.audio / durl。
        dash = info.get("dash") or {}
        audios = dash.get("audio") or []
        if audios:
            audios.sort(key=lambda a: a.get("bandwidth", 0), reverse=True)
            return audios[0].get("base_url")
        durls = info.get("durl") or []
        if durls:
            return (durls[0] or {}).get("url")
    except Exception as e:
        print(f"    [音频] 解析直链结构失败：{e}")
    return None


async def download_audio(url, path):
    import aiohttp
    headers = {"User-Agent": UA, "Referer": REFERER}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
    with open(path, "wb") as f:
        f.write(data)


def _ffmpeg_path():
    ff = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if ff:
        return ff
    # 回退 1：项目自带 bin/（Windows 为 ffmpeg.exe，Linux/macOS 为 ffmpeg）
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("ffmpeg.exe", "ffmpeg"):
        cand = os.path.abspath(os.path.join(here, "..", "bin", name))
        if os.path.exists(cand):
            return cand
    # 回退 2：pip 安装的 imageio-ffmpeg 自带静态 ffmpeg（跨平台，免系统安装）
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return None


def remux(src, dst):
    ff = _ffmpeg_path()
    if not ff:
        return False
    try:
        subprocess.run([ff, "-y", "-i", src, "-c", "copy", dst],
                       check=True, capture_output=True)
        return True
    except Exception:
        return False


async def main_async(args):
    bvid = extract_bvid(args.url)
    print(f"[取数] BV={bvid}")
    info = await get_info(bvid)
    title = info.get("title", "")
    owner = (info.get("owner") or {}).get("name", "")
    pubdate = time.strftime("%Y-%m-%d", time.localtime(info.get("pubdate", 0)))
    duration = int(info.get("duration", 0))
    desc = (info.get("desc") or "").strip()
    stat = info.get("stat") or {}

    # cid 是分 P ID，取数/字幕/下载接口都需要
    cid = info.get("cid")
    if not cid:
        first_page = (info.get("pages") or [{}])[0]
        cid = first_page.get("cid")
    if not cid:
        sys.exit(f"[错误] 无法从视频信息获取 cid（可能是番剧/特殊页面），info keys={list(info.keys())[:20]}")

    # 输出目录名只用视频标题（UP主信息保留在 meta.json 的 owner 字段，不进目录名）
    out_dir = os.path.join(args.out, sanitize(title))
    os.makedirs(out_dir, exist_ok=True)

    # ---- 字幕三级兜底 ----
    has_sub = False
    transcript_text, srt_text = None, None
    sub = await get_subtitle(bvid, cid)
    best = pick_subtitle(sub)
    if best and best.get("subtitle_url"):
        try:
            data = await dl_json(_abs_url(best["subtitle_url"]))
            transcript_text, srt_text = sub_to_text_srt(data.get("body") or [])
            has_sub = bool(transcript_text)
            print(f"    [字幕] ① player 接口字幕命中（lan={best.get('lan')}, ai={best.get('ai_type')}）")
        except Exception as e:
            print(f"    [字幕] ① 下载/解析失败：{e}")
    if not has_sub:
        y = yt_dlp_subs(bvid, out_dir)
        if y:
            transcript_text, srt_text = y, None
            has_sub = True
            print("    [字幕] ② yt-dlp 字幕兜底命中")
    if not has_sub:
        print("    [字幕] ③ 无官方字幕，后续走 ASR 上传分支")

    # ---- 写文件 ----
    if desc:
        with open(os.path.join(out_dir, "shownotes.md"), "w", encoding="utf-8") as f:
            f.write(desc + "\n")
    if has_sub and transcript_text:
        with open(os.path.join(out_dir, "transcript.txt"), "w", encoding="utf-8") as f:
            f.write(transcript_text + "\n")
        if srt_text:
            with open(os.path.join(out_dir, "subtitle.srt"), "w", encoding="utf-8") as f:
                f.write(srt_text)

    meta = {
        "eid": bvid,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "title": title,
        "podcast_title": owner,
        "hosts": [owner],
        "guest_hints": [],
        "pub_date": pubdate,
        "duration_sec": duration,
        "audio_url": "",
        "audio_path": "",
        "has_official_transcript": has_sub,
        "view_count": stat.get("view"),
        "source": "bilibili",
    }

    # ---- 音频下载 ----
    if not args.no_audio:
        try:
            aurl = await get_audio_url(bvid)
            if not aurl:
                raise RuntimeError("bilibili_api 未返回音频直链（番剧/需登录？或 B站风控 412）")
            raw = os.path.join(out_dir, "audio.m4s")
            print("    [音频] 下载中...")
            await download_audio(aurl, raw)
            final = os.path.join(out_dir, "audio.m4a")
            if remux(raw, final):
                os.remove(raw)
                print(f"    [音频] 已重封装 -> {final}")
            else:
                os.replace(raw, final)
                print(f"    [音频] 未检测到 ffmpeg，直存 m4s 为 .m4a -> {final}"
                      f"（若 ASR 拒收，需装 ffmpeg 重封装）")
            meta["audio_path"] = os.path.abspath(final)
        except Exception as e:
            print(f"    [音频] 下载失败：{e}（将无本地音频，无字幕时无法 ASR）")
    else:
        print("    [音频] 跳过（--no-audio）")

    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"[完成] 输出目录：{out_dir}")
    print(f"        有字幕={has_sub}，本地音频={'有' if meta['audio_path'] else '无'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="BV号 / 完整B站链接 / b23.tv 短链")
    ap.add_argument("--out", default="./_work", help="工作根目录，下建 <标题>/ 子目录（目录名即视频标题）")
    ap.add_argument("--no-audio", action="store_true", help="不下载音频（仅取元数据+字幕）")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
