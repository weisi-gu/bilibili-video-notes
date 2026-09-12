#!/usr/bin/env python3
"""
transcribe.py — 把音频转成文字。云端后端（阿里云百炼 DashScope，同一个 DASHSCOPE_API_KEY）：

  qwen    (模型 qwen3-asr-flash-filetrans，单人口播、长音频、便宜；不区分说话人)
      export DASHSCOPE_API_KEY="sk-xxx"
      python transcribe.py --from-meta ./_work --out ./_work --backend qwen [--region cn|intl]

  funasr  (模型 fun-asr，对谈型首选：支持说话人分离 + 热词)
      python transcribe.py --from-meta ./_work --out ./_work --backend funasr --diarize [--speaker-count 2] [--vocabulary-id vocab-xxx]

  api     (可选：其它 OpenAI 兼容的语音转写接口，需先下载音频为本地文件)
      export ASR_API_BASE=...; export ASR_API_KEY=...
      python transcribe.py ./_work/audio.m4a --out ./_work --backend api --model whisper-large-v3

音频来源：云端后端用公网 URL（推荐 --from-meta 自动读 meta.json 的 audio_url，无需下载）。
产物：transcript.txt（纯文本；开分离时带【说话人N】标注）、transcript.srt（带时间戳）。
"""
import argparse, os, sys, json, time, mimetypes, urllib.request, urllib.error, urllib.parse


class QuotaExhausted(Exception):
    """某模型免费额度用尽/欠费，可触发自动降级到下一个后端。"""


# 额度不足/欠费的常见关键词（阿里云错误码或提示里出现即判定为额度问题）
_QUOTA_KEYWORDS = ("arrear", "quota", "insufficient", "freeallocated", "allocationexceeded",
                   "balance", "欠费", "额度", "余额", "配额", "免费额度")


def _is_quota_error(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _QUOTA_KEYWORDS)


# ---------------- 通用输出 ----------------
def fmt_ts(sec: float) -> str:
    h = int(sec // 3600); m = int(sec % 3600 // 60)
    s = int(sec % 60); ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------- ASR 句子碎片合并 ----------------
# DashScope 等后端按停顿+句末点切句，会把 a.m./p.m. 这类缩写误拆成 "a."/"m."/"p."/"m."。
# 下面规则把它们并回上一句，减少「一句话被拆开」的问题。
import re as _re

_RE_SINGLE_DOT = _re.compile(r"^[A-Za-z]\.$")          # 整段就一个 "x."（a. p. m. 等）
_ABBREV_FRAG = {
    "dr.", "mr.", "mrs.", "ms.", "st.", "vs.", "etc.", "fig.", "no.", "vol.",
    "prof.", "capt.", "gen.", "sgt.", "corp.", "ltd.", "inc.", "co.", "jr.",
    "sr.", "al.", "e.g.", "i.e.", "c.f.", "viz.", "ph.d.", "u.s.", "u.k.",
    "rev.", "sen.", "rep.", "dept.", "est.", "approx.", "attn.",
}
_SENT_END = ".!?。！？"

# 上一句以 " a." / " p." 结尾（前接行首或空格），且下一句恰为 "m." → 属 a.m./p.m. 被拆
_RE_AMPM_PREV = _re.compile(r"(?:^|\s)([ap])\.$", _re.IGNORECASE)


def _merge_text(prev_text, cur_text):
    """把被拆开的两句拼回去。
    a./p. + m. 必须连写成 a.m./p.m.——若加空格变成 'a. m.'，结尾的 ' m.' 会再次命中
    「缩写中段」判断，导致后面原本独立的句子被一路错误并入（过度合并）。
    """
    p = (prev_text or "").rstrip()
    c = (cur_text or "").strip()
    if _RE_AMPM_PREV.search(p) and c.lower() == "m.":
        return p[:-1] + ".m."          # "...two a." + "m." -> "...two a.m."
    return (p + " " + c).strip()


def _is_abbrev_fragment(text):
    t = (text or "").strip().lower()
    if not t:
        return False
    if _RE_SINGLE_DOT.match(t):          # 单字母+点：强信号（a. p. m.）
        return True
    if t in _ABBREV_FRAG:
        return True
    return False


def _prev_ends_single_dot(prev_text):
    """上一句以 ' x.' 形式结尾（x 为单字母），说明正处于 a.m./p.m. 这类缩写中段。"""
    return _re.search(r"(?:^|\s)([A-Za-z])\.$", (prev_text or "").rstrip()) is not None


def _prev_ends_abbrev(prev_text):
    """上一句以已知缩写结尾（Dr./Mr./etc. 等），其后应续写而非断句。"""
    last = (prev_text or "").rstrip().split()[-1].lower() if (prev_text or "").rstrip() else ""
    return last in _ABBREV_FRAG


def merge_segments(segments, max_gap=0.3):
    """把被 ASR 误拆的缩写碎片/小间隔续写片段并回上一句。
    合并条件（满足任一对 (prev,cur) 即并入）：
      1) cur 本身是缩写碎片（单字母点 / 已知缩写）；
      2) prev 以缩写结尾（' x.' 单字母中段，或 Dr./etc. 等已知缩写），且
         cur 小写开头 或 间隔很小 或 cur 也是碎片；
      3) 间隔<=max_gap 且 cur 小写/标点开头，且 prev 不以句末标点结尾（真句界不并）。
    时间戳取 prev.start 与 max(prev.end, cur.end)。
    """
    out = []
    for start, end, text in segments:
        text = (text or "").strip()
        if not text:
            continue
        if out:
            p_start, p_end, p_text = out[-1]
            gap = start - p_end
            frag = _is_abbrev_fragment(text)
            mid_abbrev = _prev_ends_single_dot(p_text) or _prev_ends_abbrev(p_text)
            prev_end_ch = (p_text.rstrip()[-1:] if p_text.rstrip() else "")
            real_boundary = prev_end_ch in _SENT_END
            cont = gap <= max_gap and (text[0].islower() or text[0] in "，、）),.;:（(")
            if frag:
                out[-1] = (p_start, max(p_end, end), _merge_text(p_text, text))
            elif mid_abbrev and (text[0].islower() or gap <= max_gap or frag):
                out[-1] = (p_start, max(p_end, end), _merge_text(p_text, text))
            elif cont and not real_boundary:
                out[-1] = (p_start, max(p_end, end), (p_text.rstrip() + " " + text).strip())
            else:
                out.append((start, end, text))
        else:
            out.append((start, end, text))
    return out


def write_outputs(out_dir, segments, merge=True):
    """segments: list of (start_sec, end_sec, text)。merge=True 时先做碎片合并。"""
    if merge:
        before = len(segments)
        segments = merge_segments(segments)
        if len(segments) != before:
            print(f"    [合并] 已把 {before} 句误拆碎片合并为 {len(segments)} 句")
    txt = os.path.join(out_dir, "transcript.txt")
    srt = os.path.join(out_dir, "transcript.srt")
    with open(txt, "w", encoding="utf-8") as ft, open(srt, "w", encoding="utf-8") as fs:
        for i, (start, end, text) in enumerate(segments, 1):
            text = (text or "").strip()
            if not text:
                continue
            ft.write(text + "\n")
            fs.write(f"{i}\n{fmt_ts(start)} --> {fmt_ts(end)}\n{text}\n\n")
    print(f"[完成] 转录文本 -> {txt}")
    print(f"       字幕     -> {srt}")
    _quality_hint(segments)


def _quality_hint(segments):
    """转录后粗略自检：英文/字母占比过高时，提示中英混或可考虑用 fun-asr 重转。"""
    full = "".join((t or "") for _, _, t in segments)
    chars = [c for c in full if not c.isspace()]
    if not chars:
        return
    latin = sum(1 for c in chars if ("a" <= c.lower() <= "z"))
    ratio = latin / len(chars)
    if ratio >= 0.12:
        print(f"    ⚠️ 英文/字母占比约 {ratio:.0%}，这期可能中英混较多或含较多专名。"
              f"若识别不准，建议用 --backend funasr 重转，并可配热词表(--vocabulary-id)提升人名/术语。")


def _http_json(url, headers, data=None, method=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- DashScope 异步任务：提交 / 轮询 / 解析（qwen 与 funasr 共用） ----------
def _dashscope_urls(region):
    if region == "intl":
        return ("https://dashscope-intl.aliyuncs.com/api/v1/services/audio/asr/transcription",
                "https://dashscope-intl.aliyuncs.com/api/v1/tasks/")
    return ("https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription",
            "https://dashscope.aliyuncs.com/api/v1/tasks/")


def _submit_and_poll(submit_url, task_base, key, payload, out_dir):
    print("    提交异步任务...")
    try:
        submit = _http_json(submit_url,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "X-DashScope-Async": "enable",
                     "X-DashScope-OssResourceResolve": "enable"},
            data=json.dumps(payload).encode(), method="POST", timeout=60)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        if _is_quota_error(body) or e.code in (402, 429):
            raise QuotaExhausted(f"HTTP {e.code}: {body[:300]}")
        sys.exit(f"[错误] 提交失败 HTTP {e.code}：{body[:400]}")
    # 提交返回里若带额度类错误码
    if _is_quota_error(json.dumps(submit, ensure_ascii=False)) and not (submit.get("output") or {}).get("task_id"):
        raise QuotaExhausted(json.dumps(submit, ensure_ascii=False)[:300])
    task_id = (submit.get("output") or {}).get("task_id")
    if not task_id:
        sys.exit(f"[错误] 未拿到 task_id：{json.dumps(submit, ensure_ascii=False)[:400]}")
    print(f"    task_id = {task_id}，轮询中（长音频约 2-5 分钟）...")
    for _ in range(360):
        time.sleep(10)
        q = _http_json(task_base + task_id,
                       headers={"Authorization": f"Bearer {key}"}, timeout=30)
        st = (q.get("output") or {}).get("task_status")
        print(f"\r    状态: {st}   ", end="", flush=True)
        if st == "SUCCEEDED":
            print(); return q
        if st in ("FAILED", "CANCELED", "UNKNOWN"):
            detail = json.dumps(q, ensure_ascii=False)
            if _is_quota_error(detail):
                raise QuotaExhausted(detail[:300])
            sys.exit(f"\n[错误] 任务 {st}：{detail[:500]}")
    sys.exit("\n[错误] 轮询超时（>1 小时）。")


def _extract_segments(result, out_dir, want_speaker):
    """从任务结果里取 transcription_url -> transcripts[].sentences[]。
    兼容两种外层结构：output.result(单数, qwen filetrans) 与 output.results(复数数组, fun-asr)。
    want_speaker=True 时，说话人变化处给文本加【说话人N】前缀。"""
    segments, last_spk = [], None
    out = result.get("output", {})
    # 单数 result（qwen3-asr-flash-filetrans）或 复数 results（fun-asr）
    results = out.get("results")
    if results is None:
        single = out.get("result")
        results = [single] if single else []
    if isinstance(results, dict):
        results = [results]
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("subtask_status") == "FAILED":
            print(f"    [警告] 子任务失败：{item.get('code')} {item.get('message')}")
            continue
        trans_url = item.get("transcription_url") or item.get("url")
        data = None
        if trans_url:
            try:
                data = _http_json(trans_url, headers={}, timeout=60)
            except Exception as e:
                print(f"    [警告] 拉取转写结果失败：{e}")
        else:
            data = item
        if not data:
            continue
        for tr in (data.get("transcripts") or []):
            sents = tr.get("sentences") or []
            for s in sents:
                text = s.get("text") or s.get("Text") or ""
                spk = s.get("speaker_id", s.get("speakerId"))
                if want_speaker and spk is not None and spk != last_spk:
                    text = f"【说话人{spk}】{text}"
                    last_spk = spk
                bt = s.get("begin_time", s.get("beginTime", 0)) or 0
                et = s.get("end_time", s.get("endTime", 0)) or 0
                segments.append((bt / 1000.0, et / 1000.0, text))
            if not sents and tr.get("text"):
                segments.append((0, 0, tr["text"]))
    if not segments:
        raw = os.path.join(out_dir, "dashscope_raw_result.json")
        with open(raw, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        inner = os.path.join(out_dir, "dashscope_transcription.json")
        # 若拿到了内层 JSON 但没解析出，也存一份便于排查
        try:
            if trans_url and data:
                with open(inner, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                sys.exit(f"[错误] 拿到结果但未解析出文本，已存 {inner} 供排查（把它发给我即可）。")
        except Exception:
            pass
        sys.exit(f"[错误] 未解析出文本，已存原始返回到 {raw} 供排查。")
    return segments


def _check_url_and_key(audio_url):
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        sys.exit("[缺配置] 请设置 DASHSCOPE_API_KEY（百炼 API Key）。\n"
                 "         获取：https://bailian.console.aliyun.com/ -> API-KEY")
    # audio_url 可能是公网 http(s) URL，或上传本地文件后得到的 file_id://... 引用，二者皆可
    if not audio_url:
        sys.exit("[用法错误] 缺少音频来源（公网 URL 或 已上传 file_id://...）。")
    return key


# ---------------- 本地文件上传 DashScope（B站直链带 referer/时效 → 云端 ASR 拉不到时的桥） ----------------
def _upload_local_file(audio_path, region, model):
    """本地音频走 DashScope 文件上传凭证接口：getPolicy 拿临时凭证 -> 直传 OSS -> 返回 oss:// URL。
    ASR 提交时需带 X-DashScope-OssResourceResolve: enable 才能解析 oss:// 引用。"""
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        sys.exit("[缺配置] 上传本地文件需要 DASHSCOPE_API_KEY（百炼 API Key）。")
    if not os.path.isfile(audio_path):
        sys.exit(f"[错误] 本地音频文件不存在：{audio_path}")
    base = "https://dashscope-intl.aliyuncs.com/api/v1/uploads" if region == "intl" \
        else "https://dashscope.aliyuncs.com/api/v1/uploads"
    # 1) 获取上传凭证
    policy_url = f"{base}?action=getPolicy&model={urllib.parse.quote(model)}"
    try:
        pol = _http_json(policy_url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="GET", timeout=30)
    except urllib.error.HTTPError as e:
        sys.exit(f"[错误] 获取上传凭证失败 HTTP {e.code}：{e.read().decode('utf-8','ignore')[:300]}")
    d = pol.get("data") or {}
    host = d.get("upload_host"); policy = d.get("policy"); sig = d.get("signature")
    ak = d.get("oss_access_key_id"); udir = d.get("upload_dir")
    acl = d.get("x_oss_object_acl", "private"); forbid = d.get("x_oss_forbid_overwrite", "true")
    if not (host and policy and sig and ak and udir):
        sys.exit(f"[错误] 上传凭证字段缺失：{json.dumps(pol, ensure_ascii=False)[:300]}")
    # 2) 直传 OSS
    fname = os.path.basename(audio_path)
    key_path = f"{udir}/{fname}"
    with open(audio_path, "rb") as f:
        raw = f.read()
    boundary = "----wboss" + str(int(time.time() * 1000))
    fields = [("OSSAccessKeyId", ak), ("Signature", sig), ("policy", policy),
              ("x-oss-object-acl", acl), ("x-oss-forbid-overwrite", forbid),
              ("key", key_path), ("success_action_status", "200")]
    parts = []
    for nm, vl in fields:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{nm}\"\r\n\r\n{vl}\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"{fname}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
    parts.append(raw)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(host, data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    print(f"    [上传] 本地音频 -> OSS ({len(raw)//1024} KB)...")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        sys.exit(f"[错误] OSS 上传失败 HTTP {e.code}：{e.read().decode('utf-8','ignore')[:300]}")
    if code != 200:
        sys.exit(f"[错误] OSS 上传返回状态码 {code}")
    oss_url = f"oss://{key_path}"
    print(f"    [上传] 完成，oss_url={oss_url}")
    return oss_url


# ---------------- 后端：qwen（qwen3-asr-flash-filetrans） ----------------
def run_qwen(audio_url, out_dir, model, language, region, merge=True):
    if not audio_url.lower().startswith("http"):
        audio_url = _upload_local_file(audio_url, region, model)
    key = _check_url_and_key(audio_url)
    submit_url, task_base = _dashscope_urls(region)
    params = {"channel_id": [0], "enable_itn": True, "enable_words": True}
    if language:
        params["language"] = language
    payload = {"model": model, "input": {"file_url": audio_url}, "parameters": params}
    print(f"[转录] Qwen {model} ({region})")
    result = _submit_and_poll(submit_url, task_base, key, payload, out_dir)
    write_outputs(out_dir, _extract_segments(result, out_dir, want_speaker=False), merge=merge)


# ---------------- 后端：funasr（fun-asr，说话人分离 + 热词） ----------------
def run_funasr(audio_url, out_dir, model, language, region, diarize, speaker_count, vocabulary_id, merge=True):
    if not audio_url.lower().startswith("http"):
        audio_url = _upload_local_file(audio_url, region, model)
    key = _check_url_and_key(audio_url)
    submit_url, task_base = _dashscope_urls(region)
    params = {"channel_id": [0]}
    if diarize:
        params["diarization_enabled"] = True
        if speaker_count:
            params["speaker_count"] = speaker_count
    if vocabulary_id:
        params["vocabulary_id"] = vocabulary_id
    if language:
        # 带上 en 有助于中英混杂识别（paraformer-v2 尤其受益）
        params["language_hints"] = [language, "en"] if language != "en" else ["en"]
    payload = {"model": model, "input": {"file_urls": [audio_url]}, "parameters": params}
    print(f"[转录] {model} ({region})，说话人分离={'开' if diarize else '关'}")
    if diarize:
        print("    注意：开启说话人分离建议音频≤2小时，且仅支持单声道。")
    result = _submit_and_poll(submit_url, task_base, key, payload, out_dir)
    write_outputs(out_dir, _extract_segments(result, out_dir, want_speaker=diarize), merge=merge)


# ---------------- 后端：通用 OpenAI 兼容 API ----------------
def run_api(audio, out_dir, model, language, merge=True):
    base = os.environ.get("ASR_API_BASE", "").rstrip("/")
    key = os.environ.get("ASR_API_KEY", "")
    if not base or not key:
        sys.exit("[缺配置] 请设置 ASR_API_BASE 和 ASR_API_KEY")
    if audio.lower().startswith("http"):
        sys.exit("[用法错误] 该后端需本地文件，请先下载音频。")
    url = f"{base}/audio/transcriptions"
    print(f"[转录] API 后端 {url}, model={model}")
    boundary = "----xyznote" + str(int(time.time()))
    with open(audio, "rb") as f:
        audio_bytes = f.read()
    parts = []
    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{name}"\r\n\r\n{value}\r\n'.encode())
    field("model", model)
    if language:
        field("language", language)
    field("response_format", "verbose_json")
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f'filename="{os.path.basename(audio)}"\r\nContent-Type: audio/m4a\r\n\r\n'.encode()
        + audio_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(url, data=b"".join(parts),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.loads(r.read().decode("utf-8"))
    segs = data.get("segments")
    collected = ([(s.get("start", 0), s.get("end", 0), s.get("text", "")) for s in segs]
                 if segs else [(0, 0, data.get("text", ""))])
    write_outputs(out_dir, collected, merge=merge)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="?", default=None,
                    help="本地音频路径，或（qwen/funasr）公网音频 URL；也可用 --from-meta 自动读取")
    ap.add_argument("--from-meta", default=None,
                    help="给出含 meta.json 的目录（如 ./_work），自动读取其中的 audio_url，无需手动传链接")
    ap.add_argument("--from-raw", default=None,
                    help="从已保存的 dashscope_raw_result.json 恢复解析（任务已成功、不重新转录、不重复计费）")
    ap.add_argument("--resume-task", default=None,
                    help="续查已提交的 DashScope 异步任务 ID；成功后保存结果并输出逐字稿，不重复提交或计费")
    ap.add_argument("--out", default="./_work")
    ap.add_argument("--backend", choices=["qwen", "funasr", "paraformer", "api"],
                    default="qwen")
    ap.add_argument("--model", default=None,
                    help="qwen默认 qwen3-asr-flash-filetrans；funasr默认 fun-asr；paraformer默认 paraformer-v2；api默认 whisper-large-v3")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--region", choices=["cn", "intl"], default="cn",
                    help="云端地域：cn=北京，intl=新加坡（API Key 不同）")
    # funasr / paraformer 专用（两者用法一致）
    ap.add_argument("--diarize", action="store_true", help="funasr/paraformer: 开启说话人分离")
    ap.add_argument("--speaker-count", type=int, default=None, help="说话人数量提示(2-100)")
    ap.add_argument("--vocabulary-id", default=None, help="热词表ID(vocabulary_id)，funasr/paraformer 支持")
    ap.add_argument("--fallback", default=None,
                    help="自动降级：给一串后端顺序（逗号分隔，如 paraformer,funasr,qwen），"
                         "某个额度用尽/欠费时自动换下一个重试；其它错误不切、直接报错。设置后覆盖 --backend。")
    ap.add_argument("--no-merge", action="store_true",
                    help="关闭 ASR 句子碎片自动合并（默认开启：把 a.m./p.m. 等误拆片段并回上一句）。"
                         "若发现过度合并，加此参数重跑。")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    # 恢复模式：从已成功的 raw 结果解析，不重新转录
    if args.from_raw:
        try:
            with open(args.from_raw, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            sys.exit(f"[错误] 读取 {args.from_raw} 失败：{e}")
        print(f"[恢复] 从 {args.from_raw} 解析已完成的转录结果...")
        write_outputs(args.out, _extract_segments(raw, args.out, want_speaker=args.diarize),
                      merge=not args.no_merge)
        return

    if args.resume_task:
        key = os.environ.get("DASHSCOPE_API_KEY")
        if not key:
            sys.exit("[错误] 缺少 DASHSCOPE_API_KEY，无法续查异步任务。")
        _, task_base = _dashscope_urls(args.region)
        print(f"[续查] task_id = {args.resume_task}，轮询中...")
        for _ in range(360):
            q = _http_json(task_base + args.resume_task,
                           headers={"Authorization": f"Bearer {key}"}, timeout=30)
            st = (q.get("output") or {}).get("task_status")
            print(f"\r    状态: {st}   ", end="", flush=True)
            if st == "SUCCEEDED":
                print()
                raw = os.path.join(args.out, "dashscope_raw_result.json")
                with open(raw, "w", encoding="utf-8") as f:
                    json.dump(q, f, ensure_ascii=False, indent=2)
                write_outputs(args.out, _extract_segments(q, args.out, want_speaker=args.diarize),
                              merge=not args.no_merge)
                return
            if st in ("FAILED", "CANCELED", "UNKNOWN"):
                sys.exit(f"\n[错误] 任务 {st}：{json.dumps(q, ensure_ascii=False)[:500]}")
            time.sleep(10)
        sys.exit("\n[错误] 续查轮询超时（>1 小时）。")

    # 解析音频来源：--from-meta 优先（自动读 meta.json 的 audio_url）
    audio = args.audio
    if args.from_meta:
        meta_path = os.path.join(args.from_meta, "meta.json")
        try:
            with open(meta_path, encoding="utf-8") as f:
                _m = json.load(f)
            audio = _m.get("audio_path") or _m.get("audio_url")
        except Exception as e:
            sys.exit(f"[错误] 读取 {meta_path} 失败：{e}")
        if not audio:
            sys.exit(f"[错误] {meta_path} 里没有 audio_path/audio_url（可能是付费/需登录单集）。")
    if not audio:
        sys.exit("[用法] 需给出音频：本地路径、公网URL，或 --from-meta ./_work")

    def _run(backend):
        if backend == "qwen":
            run_qwen(audio, args.out, args.model or "qwen3-asr-flash-filetrans",
                     args.language, args.region, merge=not args.no_merge)
        elif backend == "funasr":
            run_funasr(audio, args.out, args.model or "fun-asr", args.language,
                       args.region, args.diarize, args.speaker_count, args.vocabulary_id,
                       merge=not args.no_merge)
        elif backend == "paraformer":
            # paraformer 与 fun-asr 用法一致（同接口/同参数），仅换模型名
            run_funasr(audio, args.out, args.model or "paraformer-v2", args.language,
                       args.region, args.diarize, args.speaker_count, args.vocabulary_id,
                       merge=not args.no_merge)
        else:
            run_api(audio, args.out, args.model or "whisper-large-v3", args.language,
                    merge=not args.no_merge)

    # 后端链：--fallback 优先（自动降级），否则单个 --backend
    chain = [b.strip() for b in args.fallback.split(",") if b.strip()] if args.fallback else [args.backend]
    if args.diarize and "qwen" in chain:
        print("[提示] qwen 不支持说话人分离；若降级到 qwen，该期将不带【说话人N】标注。")
    for i, backend in enumerate(chain):
        try:
            if len(chain) > 1:
                print(f"[后端 {i+1}/{len(chain)}] 尝试 {backend} ...")
            _run(backend)
            return  # 成功即结束
        except QuotaExhausted as e:
            if i + 1 < len(chain):
                print(f"[降级] {backend} 额度用尽/欠费，自动改用下一个：{chain[i+1]}\n    （原因：{str(e)[:120]}）")
            else:
                sys.exit(f"[错误] 所有后端额度均已用尽/欠费（最后尝试 {backend}）。请充值或换地域后重试。")


if __name__ == "__main__":
    main()
