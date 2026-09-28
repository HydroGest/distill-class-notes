#!/usr/bin/env python3
"""蒸馏 (Distill) 流水线
每晚 cron 运行:扫描 inbox → 时间聚簇分组 → STT 转写录音 → VL 结构化照片 → LLM 整合草稿

用法:
  python3 pipeline.py             # 处理 inbox 全部素材
  python3 pipeline.py --dry-run   # 只做分组预览,不处理
  python3 pipeline.py --verbose   # 详细日志
"""
import argparse
import base64
import fcntl
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.yaml"
STATUS = BASE / "data" / "status.json"
LOCK = BASE / "data" / "pipeline.lock"

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".amr", ".3gp"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"}
SKIP_DIRS = {"废纸篓", ".git"}


# ---------- 工具 ----------

def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def log(msg, verbose=False):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def acquire_lock():
    """进程锁:防止手动触发与 cron 同时跑(重叠时直接退出)"""
    f = open(LOCK, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        return None


def write_status(**kw):
    """写运行状态供控制台读取(合并写入)"""
    try:
        import json
        old = {}
        if STATUS.exists():
            old = json.loads(STATUS.read_text(encoding="utf-8"))
        old.update(kw)
        old["updated_at"] = datetime.now().isoformat(timespec="seconds")
        STATUS.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def scan_inbox(inbox: Path):
    """收集 inbox 下所有文件,跳过废纸篓等目录"""
    files = []
    for p in sorted(inbox.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(inbox).parts):
            continue
        files.append(p)
    return files


def effective_time(p: Path) -> float:
    """文件的"真实时间":录音时间/拍摄时间,优先于上传时间。
    上传页会给文件名加 20260915-003613- 前缀,取最后一个时间戳(即原始文件名里的)。
    """
    name = p.name
    matches = re.findall(r"(\d{8})[-_](\d{4,6})", name)
    if matches:
        ymd, hm = matches[-1]
        try:
            return datetime.strptime(ymd + hm[:4], "%Y%m%d%H%M").timestamp()
        except ValueError:
            pass
    # 图片 EXIF 拍摄时间
    if p.suffix.lower() in IMAGE_EXTS:
        try:
            from PIL import Image
            from PIL.ExifTags import TAGS
            img = Image.open(p)
            exif = img.getexif()
            for tag, val in exif.items():
                if TAGS.get(tag) == "DateTimeOriginal":
                    return datetime.strptime(str(val)[:19], "%Y:%m:%d %H:%M:%S").timestamp()
        except Exception:  # noqa: BLE001
            pass
    return p.stat().st_mtime


def group_sessions(files, gap_minutes, inbox: Path):
    """时间聚簇:一节课 = 同一课程目录 + 同一天(课间休息不算新课)。
    跨天 → 新会话;不同课程目录 → 各自成会话,永不混组。"""
    by_course: dict[str, list[Path]] = {}
    for f in files:
        by_course.setdefault(_course_of(f, inbox), []).append(f)

    sessions = []
    for course in sorted(by_course):
        fl = sorted(by_course[course], key=effective_time)
        cur, cur_date = [], None
        for f in fl:
            d = datetime.fromtimestamp(effective_time(f)).date()
            if cur_date is not None and d != cur_date:
                sessions.append(cur)
                cur = []
            cur.append(f)
            cur_date = d
        if cur:
            sessions.append(cur)
    return sessions


def _course_of(f: Path, inbox: Path) -> str:
    """inbox 下的课程子目录名(如 inbox/思政课/xxx → 思政课);没进课程目录的按待分类"""
    try:
        parts = f.relative_to(inbox).parts
    except ValueError:
        return "待分类"
    if len(parts) < 2 or parts[0] in SKIP_DIRS:
        return "待分类"
    return parts[0]


def fmt_time(ts):
    h = int(ts // 3600)
    m = int(ts % 3600 // 60)
    s = int(ts % 60)
    ms = int((ts - int(ts)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------- STT ----------

_stt_models: dict = {}

# 热词提取的常见停用词(避免把虚词送进 funasr)
_STOPWORDS = set(
    "的了是在我和有就都也不又于而与之及或很被把让给向从到对为按照以便此外但是而且并且不过不然不但不如才曾经"
    "除非从来当然等等地都反而否则或者即使既然假如尽管就就是可可是况且例如另外马上吗嘛呢能其实请然而然后任何"
    "如果若甚至时候使得所以它们他她它通过同样哇万完全万一为了为什么无论先显得现在呀也一边一直已以及因因为由于"
    "有于再在这这个这些这样真真是正正在只只要只有至于只好主要自己着没有什么一个我们你们他们这个那个那些怎么"
    "怎样那么哪里这里那里什么为什么如何谁哪个哪些每各两多少全几乎大约左右超过以上以下之间之后之前当中其中然后"
    "两个一个分钟今天问题东西方面情况内容时候老师学生同学然后所以因为如果可以进行对于以及已经知道觉得认为应该"
    "可能必须非常比较特别相关一些一次一直一样一定一起每课每个每次需要没有"
)

# LaTeX 命令/算子(数学笔记里的记号噪声,不是热词)
_LATEX_OPS = {
    "text", "frac", "dfrac", "cfrac", "times", "cdot", "left", "right", "lim", "cos", "sin",
    "tan", "cot", "theta", "alpha", "beta", "gamma", "delta", "varepsilon", "epsilon", "infty",
    "pmatrix", "bmatrix", "begin", "end", "over", "sum", "int", "partial", "vec", "hat", "sqrt",
    "approx", "leq", "geq", "neq", "rightarrow", "leftarrow", "forall", "exists", "quad", "qquad",
    "mathrm", "mathbb", "textbf", "underline", "ominus", "oplus", "otimes", "pm", "mp",
    "ldots", "cdots", "colon", "in", "cases", "array", "align", "substack", "limits", "middle",
    "big", "Big", "bigg", "Bigg", "tag", "label", "ref", "displaystyle", "textstyle",
}

# 英文停用词(英语课笔记里的虚词)
_EN_STOPS = {
    "of", "the", "is", "and", "in", "to", "for", "a", "an", "with", "at", "from", "by", "on",
    "as", "are", "was", "were", "be", "been", "being", "this", "that", "these", "those", "it",
    "its", "not", "no", "or", "so", "if", "then", "than", "when", "where", "which", "who",
    "whom", "whose", "we", "you", "your", "our", "their", "they", "he", "she", "his", "her",
    "them", "there", "here", "about", "into", "over", "under", "between", "through", "during",
    "before", "after", "above", "below", "again", "further", "once", "only", "own", "same",
    "too", "very", "just", "also", "more", "most", "other", "some", "such", "would",
    "could", "should", "may", "might", "must", "shall", "will", "can", "has", "have", "had",
    "do", "does", "did", "doing", "being",
}


def build_course_hotwords(cfg) -> dict[str, list[str]]:
    """从已归档笔记按课程提取高频术语 → data/hotwords/{course}.txt;
    default.txt = 配置热词 ∪ 全库高频词。流水线每次运行前调用,越用越准。"""
    import jieba
    from collections import Counter

    stt = cfg["pipeline"]["stt"]
    top = int(stt.get("hotword_top", 40))
    min_freq = int(stt.get("hotword_min_freq", 2))
    notes = Path(cfg["paths"]["notes"])
    hw_dir = BASE / "data" / "hotwords"
    hw_dir.mkdir(parents=True, exist_ok=True)

    counts: dict[str, Counter] = {}
    for course_dir in sorted(p for p in notes.iterdir()
                             if p.is_dir() and p.name not in ("未分类", "废纸篓")):
        counter: Counter = Counter()
        for md in course_dir.rglob("*.md"):
            txt = md.read_text(encoding="utf-8", errors="ignore")
            body = txt.split("---", 2)[-1] if txt.startswith("---") else txt
            for w in jieba.cut(body):
                w = w.strip()
                if len(w) < 2 or len(w) > 10 or w in _STOPWORDS:
                    continue
                if not re.search(r"[\u4e00-\u9fffA-Za-z]", w):
                    continue
                if w in _LATEX_OPS or w in _EN_STOPS:
                    continue
                if re.fullmatch(r"[a-zA-Z]+", w) and w.lower() in _EN_STOPS:
                    continue
                counter[w] += 1
        if counter:
            counts[course_dir.name] = counter

    result: dict[str, list[str]] = {}
    global_counter: Counter = Counter()
    for course, counter in counts.items():
        terms = [w for w, n in counter.most_common(top) if n >= min_freq]
        if terms:
            (hw_dir / f"{course}.txt").write_text("\n".join(terms), encoding="utf-8")
            result[course] = terms
        global_counter.update(counter)

    cfg_hw = [w for w in (stt.get("hotwords") or []) if w]
    all_terms = cfg_hw + [w for w, n in global_counter.most_common(top) if n >= min_freq]
    (hw_dir / "default.txt").write_text(
        "\n".join(dict.fromkeys(all_terms)), encoding="utf-8")
    return result


def _hotword_file(cfg, course: str) -> Path | None:
    """按课程选热词文件:该课文件 → default.txt → 配置热词兜底"""
    stt = cfg["pipeline"]["stt"]
    hw_dir = BASE / "data" / "hotwords"
    if course and course not in ("待分类", "未分类"):
        cand = hw_dir / f"{course}.txt"
        if cand.exists():
            return cand
    fallback = hw_dir / "default.txt"
    if fallback.exists():
        return fallback
    cfg_hw = stt.get("hotwords")
    if cfg_hw:
        hw_dir.mkdir(parents=True, exist_ok=True)
        fallback.write_text("\n".join(cfg_hw), encoding="utf-8")
        return fallback
    return None


def get_stt_model(cfg, engine: str | None = None):
    """按引擎分别缓存模型(funasr 与 faster-whisper 可同时存在,互不干扰)"""
    stt = cfg["pipeline"]["stt"]
    engine = engine or stt["engine"]
    if engine == "faster-whisper":
        key = f"fw:{stt.get('model', 'small')}"
    elif engine == "funasr":
        key = f"fa:{stt.get('funasr_model', 'paraformer-zh')}"
    else:
        raise RuntimeError(f"未知 STT 引擎: {engine}")
    if key in _stt_models:
        return _stt_models[key]
    if engine == "faster-whisper":
        from faster_whisper import WhisperModel
        _stt_models[key] = WhisperModel(stt.get("model", "small"), device="cpu",
                                        compute_type=stt.get("compute_type", "int8"))
    elif engine == "funasr":
        from funasr import AutoModel
        _stt_models[key] = AutoModel(
            model=stt.get("funasr_model", "paraformer-zh"),
            vad_model=stt.get("funasr_vad", "fsmn-vad"),
            punc_model=stt.get("funasr_punc", "ct-punc"),
            disable_update=True,
        )
    return _stt_models[key]


def _funasr_segments(r0: dict) -> list[dict]:
    """paraformer 返回逐字符时间戳(毫秒),按标点切句生成 SRT 段"""
    text = r0.get("text", "") or ""
    ts = r0.get("timestamp") or []
    if not ts:
        return [{"start": 0.0, "end": 0.0, "text": text.strip()}] if text.strip() else []
    segs, buf, s0 = [], "", None
    for i, ch in enumerate(text):
        if i >= len(ts):
            break
        s, e = ts[i]
        if s0 is None:
            s0 = s
        buf += ch
        if ch in "。！？!?…":
            segs.append({"start": s0 / 1000.0, "end": e / 1000.0, "text": buf.strip()})
            buf, s0 = "", None
    if buf.strip():
        segs.append({"start": (s0 or 0) / 1000.0, "end": ts[-1][1] / 1000.0, "text": buf.strip()})
    return segs


def _detect_lang(audio_path: Path, cfg) -> str | None:
    """用 whisper 快速检测语言(只解码首窗 ~30s),返回 'zh'/'en'/None(不确定)"""
    try:
        m = get_stt_model(cfg, "faster-whisper")
        segs, info = m.transcribe(str(audio_path), language=None, beam_size=1,
                                  condition_on_previous_text=False, vad_filter=True)
        next(iter(segs), None)  # 只消费首段,触发语言检测
        if info.language_probability >= 0.55:
            return info.language
    except Exception as e:  # noqa: BLE001
        log(f"  🌐 语言检测失败: {e}")
    return None


def transcribe(audio_path: Path, cfg, course: str | None = None) -> tuple[list, float] | None:
    """返回 (segments, duration_sec);失败返回 None。
    course 用于路由:english_courses 里的课走 faster-whisper;未知课程自动检测语言。"""
    try:
        stt = cfg["pipeline"]["stt"]
        en_courses = set(stt.get("english_courses") or [])
        engine = stt["engine"]
        course = (course or "").strip()
        if course in en_courses:
            engine = "faster-whisper"
            log(f"  🌐 {course} 指定英文引擎 → faster-whisper")
        elif course in ("", "待分类", "未分类") and stt.get("auto_lang", True):
            lang = _detect_lang(audio_path, cfg)
            if lang == "en":
                engine = "faster-whisper"
                log("  🌐 检测到英文 → 切 faster-whisper")
            elif lang != "zh":
                log(f"  🌐 语言检测:{lang or '不确定'},默认 {engine}")
        if engine == "faster-whisper":
            model = get_stt_model(cfg, "faster-whisper")
            segments, info = model.transcribe(str(audio_path), language=None,
                                              vad_filter=True, beam_size=1)
            segs = [{"start": s.start, "end": s.end, "text": s.text.strip()}
                    for s in segments]
            log(f"  🌐 识别语言: {info.language} (p={info.language_probability:.2f})")
            return segs, info.duration
        else:
            model = get_stt_model(cfg, "funasr")
            kw = dict(input=str(audio_path), batch_size_s=300)
            hw = _hotword_file(cfg, course)
            if hw:
                kw["hotword"] = str(hw)
            res = model.generate(**kw)
            r0 = res[0]
            return _funasr_segments(r0), r0.get("duration", 0)
    except Exception as e:  # noqa: BLE001
        log(f"  ⚠️ STT 失败 {audio_path.name}: {e}")
        return None


def write_srt(segments, path: Path):
    lines = []
    for i, s in enumerate(segments, 1):
        lines.append(f"{i}\n{fmt_time(s['start'])} --> {fmt_time(s['end'])}\n{s['text']}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_txt(segments, path: Path):
    text = "".join(s["text"] for s in segments)
    path.write_text(text, encoding="utf-8")
    return text


# ---------- VL / LLM ----------

def _api_client(cfg):
    from openai import OpenAI
    return OpenAI(base_url=cfg["api"]["base_url"], api_key=cfg["api"]["api_key"])


def _img_data_url(p: Path, max_side: int = 1024):
    """照片转 base64;先用 Pillow 压到 max_side 内,VL 按分辨率计费,省 10 倍以上"""
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".bmp": "image/bmp", ".heic": "image/heic"}
    data = p.read_bytes()
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        if img.mode in ("RGB", "L"):
            img.save(buf, format="JPEG", quality=85)
            mime_type = "image/jpeg"
        else:
            img.save(buf, format="PNG")
            mime_type = "image/png"
        data = buf.getvalue()
        mime_type = mime_type
    except Exception:  # noqa: BLE001 — PIL 打不开就发原图
        mime_type = mime.get(p.suffix.lower(), "image/jpeg")
    b64 = base64.b64encode(data).decode()
    return f"data:{mime_type};base64,{b64}"


def vl_analyze_image(client, img_path: Path, cfg) -> str:
    """单张照片 → 完整内容识别;失败返回空串"""
    prompt = ("你是课堂笔记助手。这是一张课堂上拍摄的照片(板书/PPT/手写笔记)。"
              "请完整识别并抄录照片中的内容:标题、定义、公式、定理、例题的完整过程"
              "(题目→步骤→结论)。数学公式用 LaTeX($...$)。"
              "不要概括成短要点,不要省略推导步骤;看不清的地方标注 (看不清楚)。")
    try:
        r = client.chat.completions.create(
            model=cfg["api"]["vision_model"],
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _img_data_url(img_path)}},
            ]}],
            max_tokens=1000,
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        log(f"  ⚠️ VL 失败 {img_path.name}: {e}")
        return ""


def llm_integrate(client, cfg, transcript: str, image_points: list[str],
                  course_hint: str) -> str:
    """录音转写 + 照片识别 → 草稿正文 Markdown(课程由上传时的目录决定,不让模型猜)"""
    photo_block = "\n".join(f"- {p}" for p in image_points if p)
    prompt = f"""你是课堂笔记整理助手。下面是一节课的真实素材:课堂录音转写 + 板书/PPT/手写笔记照片的完整识别文本。

请把这一节课写成「能直接复习的课堂笔记」——记录知识本身,而不是概括这节课讲了什么。

要求:
1. 结构由素材决定:录音为主的课按老师讲的顺序组织;照片为主的课按知识点/例题组织。先把每个知识点的定义/公式/定理写全(公式用 LaTeX),再展开证明思路或例题。
2. 例题完整展开:题目 → 思路 → 步骤 → 结论,逐步写,不要省略中间推导。
3. 老师强调的话、答题规范、易错提醒,插在对应知识点旁边,用「⚠️」标出。
4. 素材里没有的内容不要编造;素材缺失、看不清的部分标"(待确认)"。
5. 严禁总结腔:不要出现「本课介绍了」「核心要点」「综上所述」「总而言之」,不要只列标题不给内容,不要压缩成 3-8 条干条。

素材:
一、课堂录音转写(口语化、有重复,正常):
{transcript[:9000] if transcript else "(无录音)"}

二、PPT/板书照片完整识别:
{photo_block[:8000] if photo_block else "(无照片)"}

只输出正文 Markdown(第一行是 # 主题标题),不要客套话。"""
    # 推理模型(Kimi-K2.6)会把大量 token 花在隐藏思考上,正文被挤到空/截断
    # → 预算加大(20000)+ 记录思考量与 finish_reason 便于诊断
    for attempt in range(3):
        try:
            r = client.chat.completions.create(
                model=cfg["api"]["text_model"],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=20000,
            )
            m = r.choices[0].message
            content = (m.content or "").strip()
            rc = getattr(m, "reasoning_content", None) or ""
            fr = r.choices[0].finish_reason
            log(f"  LLM: 正文{len(content)}字 思考{len(rc)}字 finish={fr}")
            if content:
                return content
            log(f"  ⚠️ LLM 返回空正文(第{attempt+1}次),重试…")
        except Exception as e:  # noqa: BLE001
            log(f"  ⚠️ LLM 整合失败(第{attempt+1}次): {e}")
        time.sleep(2)
    return ""


# ---------- 草稿落盘 ----------

def draft_note(cfg, session_dir: Path, course, date_str, topic,
               stt_text, image_points, llm_body, has_api):
    header = f"""---
课程: {course or "未分类"}          # ⚠️ 待确认
日期: {date_str}
来源: 附件/ 目录(录音、照片)
状态: 🟡 草稿
---

# {topic or "未命名课程"}
"""
    if llm_body:
        lines = llm_body.splitlines()
        # LLM 输出首行是 "# 主题",与下方标题重复,去掉
        if lines and lines[0].startswith("# "):
            llm_body = "\n".join(lines[1:]).strip("\n")
        body = llm_body
    else:
        body = "## 核心要点\n"
        if stt_text:
            body += f"录音转写摘要:\n> {stt_text[:500]}…\n\n"
        for p in image_points:
            if p:
                body += f"- {p}\n"
        if not has_api:
            body += "\n> ⚠️ 未配置 API key,照片未做 VL 识别。\n"
        body += "\n## ⚠️ 易错点\n无(校对时补)\n\n## 待补\n- [ ] 对照录音核对\n"
    note = session_dir / f"{date_str}-{topic or '未命名'}.md"
    note.write_text(header + "\n" + body, encoding="utf-8")
    return note


# ---------- 补充素材合并 ----------

def _audio_span(p: Path):
    """录音的真实区间 [开始, 结束];失败返回 None"""
    try:
        import av
        with av.open(str(p)) as c:
            dur = float(c.duration) / av.time_base if c.duration else 0.0
        if dur > 0:
            t = effective_time(p)
            return t, t + dur
    except Exception:  # noqa: BLE001
        pass
    return None


def existing_sessions(notes: Path) -> dict:
    """索引已有会话(未分类草稿 + 已归档),记录时间区间(录音用起止,其余用拍摄/录入时间)"""
    found = {}
    for course_dir in sorted(notes.iterdir()):
        if not course_dir.is_dir():
            continue
        for sd in sorted(course_dir.iterdir()):
            if not sd.is_dir():
                continue
            mds = list(sd.glob("*.md"))
            if not mds:
                continue
            spans = []
            att = sd / "附件"
            if att.exists():
                for f in att.iterdir():
                    if not f.is_file():
                        continue
                    if f.suffix.lower() in AUDIO_EXTS:
                        sp = _audio_span(f)
                        if sp:
                            spans.append(sp)
                            continue
                    t = effective_time(f)
                    spans.append((t, t))
            if not spans:
                # 无附件(如纯文本草稿)→ 没有可信的时间锚点,标记 None,
                # 绝不参与时间合并(否则会用 md 修改时间乱并)
                spans = None
            if spans is None:
                found[sd] = {"dir": sd, "t0": None, "t1": None,
                             "archived": course_dir.name != "未分类",
                             "course": _md_course(mds[0]) or course_dir.name,
                             "md": mds[0]}
                continue
            found[sd] = {"dir": sd, "t0": min(s[0] for s in spans),
                         "t1": max(s[1] for s in spans),
                         "archived": course_dir.name != "未分类",
                         "course": _md_course(mds[0]) or course_dir.name,
                         "md": mds[0]}
    return found


def _md_course(md: Path) -> str:
    """从笔记 frontmatter 读课程(未归档草稿的真实课程在 frontmatter 里)"""
    try:
        for line in md.read_text(encoding="utf-8").splitlines()[:12]:
            m = re.match(r"^课程:\s*([^#\s][^#]*?)\s*$", line.strip())
            if m:
                return m.group(1).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def split_by_existing(sess, existing: dict, gap_minutes: int, hint: str | None = None):
    """把新文件分给已有会话,其余留作新会话。

    规则(防止跨课程误并入):
    - hint 为真实课程时,只匹配同课程的会话;没有同课程会话 → 全部留作新会话
    - 无附件会话(时间锚点 None)→ 不参与时间合并
    - 时间容差: 会话区间前后 30 分钟内(容不下另一个课的素材)
    - 上传没选课程(待分类/未分类)时: 课程未知 → 只接受严格落在会话区间内的文件,
      不放开 ±30 分钟容差(否则跨课程误并)
    - 补充手写笔记照片: 同课程且同一天(即使晚上拍的)也算同一节课
    """
    assigned = {}
    leftover = []
    strict_span = bool(hint) and hint not in ("待分类", "废纸篓", "未分类")
    for f in sess:
        t = effective_time(f)
        d = datetime.fromtimestamp(t).date()
        g = min(gap_minutes or 60, 30) * 60
        cands = list(existing.items())
        if strict_span:
            same = [it for it in cands if it[1]["course"] == hint]
            if not same:
                leftover.append(f)
                continue
            cands = same
        best, best_dist = None, None
        for d2, ex in cands:
            if ex["t0"] is None:
                continue
            in_span = ex["t0"] <= t <= ex["t1"]
            if in_span or (not strict_span and ex["t0"] - g <= t <= ex["t1"] + g):
                dist = 0 if in_span else min(abs(t - ex["t0"]), abs(t - ex["t1"]))
                if best_dist is None or dist < best_dist:
                    best, best_dist = d2, dist
            elif strict_span and ex["course"] == hint and \
                    datetime.fromtimestamp(ex["t0"]).date() == d and \
                    datetime.fromtimestamp(t).hour >= 17:
                # 同课程同一天的晚间(≥17点)补拍手写笔记 → 算同一节课;白天错开的不算
                # 同课程同一天(如晚上补拍手写笔记)→ 接受,距离按跨度算
                dist = min(abs(t - ex["t0"]), abs(t - ex["t1"]))
                if best_dist is None or dist < best_dist:
                    best, best_dist = d2, dist
        if best is not None:
            assigned.setdefault(best, []).append(f)
        else:
            leftover.append(f)
    return assigned, leftover


def merge_into(ex, files, cfg, client, has_api, notes):
    """补充素材并入已有会话:移附件、VL 新照片、转写新录音、正文追加段落;
    已定稿的会话追加后移回未分类,等待重新校对"""
    att = ex["dir"] / "附件"
    att.mkdir(parents=True, exist_ok=True)
    new_imgs, new_auds = [], []
    for f in files:
        dest = att / f.name
        if dest.exists():
            dest = att / f"{f.stem}-dup{f.suffix}"
        f.rename(dest)
        if f.suffix.lower() in IMAGE_EXTS:
            new_imgs.append(dest)
        elif f.suffix.lower() in AUDIO_EXTS:
            new_auds.append(dest)
    md = ex["md"]
    parts = [f"## 📎 补充材料(自动并入于 {datetime.now():%m-%d %H:%M})"]
    if client and new_imgs:
        pts = [f"- {p.name}: {vl_analyze_image(client, p, cfg)}" for p in new_imgs[:40]]
        parts.append("### 补充照片\n" + "\n".join(pts))
    if new_auds:
        txts = []
        for a in new_auds:
            res = transcribe(a, cfg, ex.get("course") or "")
            if res:
                segs, dur = res
                write_srt(segs, a.with_suffix(".srt"))
                txts.append(write_txt(segs, a.with_suffix(".txt")))
        if txts:
            parts.append("### 补充录音\n" + "\n\n".join(txts)[:3000])
    if len(parts) > 1:
        content = md.read_text(encoding="utf-8")
        was_done = "🟢 定稿" in content
        content += "\n\n" + "\n\n".join(parts) + "\n"
        if was_done:
            content = re.sub(r"^状态:.*$", "状态: 🟡 草稿", content, count=1, flags=re.M)
        md.write_text(content, encoding="utf-8")
        if was_done and ex["dir"].parent.name != "未分类":
            back = notes / "未分类" / ex["dir"].name
            if back.exists():
                back = notes / "未分类" / f"{ex['dir'].name}-2"
            ex["dir"].rename(back)
    return len(new_imgs), len(new_auds)


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser(description="蒸馏流水线")
    ap.add_argument("--dry-run", action="store_true", help="只预览分组")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    inbox = Path(cfg["paths"]["inbox"])
    notes = Path(cfg["paths"]["notes"])
    gap = cfg["pipeline"]["stt"].get("session_gap_minutes", 60) or \
        cfg["pipeline"].get("session_gap_minutes", 60)
    has_api = bool(cfg["api"].get("api_key") and "注册后" not in str(cfg["api"].get("api_key")))

    lock = acquire_lock()
    if lock is None:
        log("已有流水线实例在运行,本次退出")
        return

    # 热词补充:从已归档笔记按课程提取高频术语(零成本,越用越准)
    try:
        hw = build_course_hotwords(cfg)
        if hw:
            log("🔥 热词库已更新: " + ", ".join(f"{c}×{len(v)}" for c, v in hw.items()))
    except Exception as e:  # noqa: BLE001
        log(f"  ⚠️ 热词生成失败(继续运行): {e}")

    files = scan_inbox(inbox)
    if not files:
        log("inbox 为空,无事可做")
        write_status(running=False, last_result="inbox-empty")
        return
    sessions = group_sessions(files, gap, inbox)
    log(f"inbox 共 {len(files)} 个文件,聚簇为 {len(sessions)} 个会话")

    if args.dry_run:
        existing = existing_sessions(notes)
        si = 0
        for sess in sessions:
            assigned, leftover = split_by_existing(sess, existing, gap, _course_of(sess[0], inbox))
            for d, fs in assigned.items():
                log(f"  🔗 并入 {d.relative_to(notes)}: {len(fs)} 个文件")
            if leftover:
                si += 1
                t0 = datetime.fromtimestamp(effective_time(leftover[0])).strftime("%m-%d %H:%M")
                t1 = datetime.fromtimestamp(effective_time(leftover[-1])).strftime("%H:%M")
                aud = [p.name for p in leftover if p.suffix.lower() in AUDIO_EXTS]
                img = [p.name for p in leftover if p.suffix.lower() in IMAGE_EXTS]
                log(f"  会话{si}: {t0}~{t1} | 录音{len(aud)} {aud[:2]} | 照片{len(img)} {img[:3]}")
        return

    if not has_api:
        log("⚠️ 未配置 API key,VL/LLM 环节跳过(注册通义/智谱后填入 config.yaml)")

    client = _api_client(cfg) if has_api else None

    # 补充素材合并:新文件拍摄/录音时间落在已有会话区间内 → 并入,不另开新笔记
    existing = existing_sessions(notes)
    merged, new_sessions = {}, []
    for sess in sessions:
        assigned, leftover = split_by_existing(sess, existing, gap, _course_of(sess[0], inbox))
        for d, fs in assigned.items():
            merged.setdefault(d, []).extend(fs)
        if leftover:
            new_sessions.append(leftover)
    if merged:
        log(f"🔗 {sum(len(fs) for fs in merged.values())} 个文件将并入已有会话")
    sessions = new_sessions

    write_status(running=True, started_at=datetime.now().isoformat(timespec="seconds"),
                 total_sessions=len(sessions), current_session=0, stage="started",
                 last_result=None, merged_sessions=len(merged))

    try:
        # 先处理并入(补充照片/录音)
        for d, fs in merged.items():
            write_status(stage="merge", file=str(d.relative_to(notes)))
            ni, na = merge_into(existing[d], fs, cfg, client, has_api, notes)
            log(f"  🔗 并入 {d.relative_to(notes)}: +{ni} 照片, +{na} 录音")

        for si, sess in enumerate(sessions, 1):
            audios = [p for p in sess if p.suffix.lower() in AUDIO_EXTS]
            images = [p for p in sess if p.suffix.lower() in IMAGE_EXTS]
            date_str = datetime.fromtimestamp(effective_time(sess[0])).strftime("%Y-%m-%d")
            log(f"会话{si}: {len(audios)} 录音, {len(images)} 照片 ({date_str})")
            write_status(current_session=si, session_name=f"{date_str}_{si:02d}",
                         stage="transcribe", files_total=len(audios) + len(images))

            out_dir = notes / "未分类" / f"{date_str}_{si:02d}"
            # 课程 = 上传时选的课程目录(待分类/未分类 → 未分类),决定 STT 引擎路由
            sess_course = _course_of(sess[0], inbox)
            if sess_course in ("待分类", "未分类"):
                sess_course = "未分类"
            i = 2
            while out_dir.exists():  # 防止与已有会话目录撞名
                out_dir = notes / "未分类" / f"{date_str}_{i:02d}"
                i += 1
            att_dir = out_dir / "附件"
            att_dir.mkdir(parents=True, exist_ok=True)

            # 1) STT
            stt_text = ""
            for ai, a in enumerate(audios, 1):
                log(f"  转写 {a.name} …")
                write_status(stage="transcribe", file=a.name, progress=f"{ai}/{len(audios)}")
                t0 = time.time()
                result = transcribe(a, cfg, sess_course)
                if result:
                    segs, duration = result
                    base = att_dir / a.stem
                    write_srt(segs, base.with_suffix(".srt"))
                    stt_text = write_txt(segs, base.with_suffix(".txt"))
                    log(f"    完成 ({time.time()-t0:.0f}s, {len(segs)} 段)")
                else:
                    log(f"    转写失败,跳过")

            # 2) VL
            image_points = []
            if client and images:
                log(f"  VL 识别 {len(images)} 张照片 …")
                write_status(stage="vl", progress=f"0/{len(images)}")
                for i, img in enumerate(images[:40], 1):  # 单会话上限 40 张
                    pt = vl_analyze_image(client, img, cfg)
                    image_points.append(f"{img.name}: {pt}")
                    write_status(stage="vl", file=img.name, progress=f"{i}/{min(len(images),40)}")
                    if args.verbose:
                        log(f"    [{i}/{min(len(images),40)}] {img.name}")

            # 3) LLM 整合(热词提取零 LLM;笔记整合正常走 LLM)
            llm_body = ""
            topic = ""
            if client and (stt_text or image_points):
                log("  LLM 整合草稿 …")
                write_status(stage="llm")
                llm_body = llm_integrate(client, cfg, stt_text, image_points, "")
                # 从正文提主题(第一行 # 后的内容)
                for line in llm_body.splitlines():
                    if line.startswith("# "):
                        topic = line[2:].strip()
                        break
            elif not client:
                log("  跳过整合(无 API key)")

            # 4) 移入附件 + 写草稿(课程 = 上传时选的课程目录,不再靠模型猜)
            for f in sess:
                f.rename(att_dir / f.name)
            note = draft_note(cfg, out_dir, sess_course, date_str, topic,
                              stt_text, image_points, llm_body, has_api)
            log(f"  ✅ 草稿: {note.relative_to(BASE)}")
            write_status(stage="done", progress=None, file=None)

        write_status(running=False, finished_at=datetime.now().isoformat(timespec="seconds"),
                     last_result="ok")
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        write_status(running=False, last_result="error", error=str(e))
        raise


if __name__ == "__main__":
    main()
