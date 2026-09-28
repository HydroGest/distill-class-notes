#!/usr/bin/env python3
"""蒸馏 (Distill) 服务:上传 + 控制台 API
手机/PC 浏览器访问:http://<服务器IP>:8000
- /           上传页
- /console.html 控制台(PWA)
- /api/*      控制台 API
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
import yaml
from flask import Flask, request, jsonify, send_from_directory, send_file, Response

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.yaml"
STATUS_FILE = BASE / "data" / "status.json"
PIPELINE_LOG = BASE / "logs" / "pipeline.log"

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".amr", ".3gp"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"}


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


cfg = load_config()
INBOX = Path(cfg["paths"]["inbox"])
NOTES = Path(cfg["paths"]["notes"])
COURSES_FILE = BASE / "data" / "courses.json"
SYSTEM_COURSES = {"待分类", "废纸篓", "未分类"}  # 系统分类,不可删


def load_courses():
    """课程列表:data/courses.json 优先,缺失时从 config.yaml 种子生成"""
    if COURSES_FILE.exists():
        try:
            data = json.loads(COURSES_FILE.read_text(encoding="utf-8"))
            if data.get("courses"):
                return data["courses"]
        except Exception:  # noqa: BLE001
            pass
    courses = list(cfg["courses"])
    COURSES_FILE.write_text(json.dumps({"courses": courses}, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    return courses


def save_courses(courses):
    COURSES_FILE.write_text(json.dumps({"courses": courses}, ensure_ascii=False, indent=1),
                            encoding="utf-8")


DEFAULT_COURSE = cfg.get("upload", {}).get("default_course", "待分类")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1GB


def safe_path(root: Path, rel: str) -> Path | None:
    """把相对路径解析到 root 下,防路径穿越;非法返回 None"""
    if not rel:
        return None
    p = (root / rel).resolve()
    if not str(p).startswith(str(root.resolve())):
        return None
    return p


# ---------- 页面 ----------

@app.route("/")
def index():
    return send_from_directory(BASE / "static" / "app", "index.html")


@app.route("/console.html")
def console():
    return send_from_directory(BASE / "static" / "app", "index.html")


@app.route("/static/<path:name>")
def static_files(name):
    return send_from_directory(BASE / "static", name)


# ---------- 上传 ----------

@app.route("/api/courses")
def api_courses():
    courses = load_courses()
    return jsonify({"courses": courses, "default": DEFAULT_COURSE,
                    "archive_courses": [c for c in courses if c != "待分类"],
                    "system_courses": sorted(SYSTEM_COURSES)})


@app.route("/api/courses", methods=["POST"])
def api_courses_add():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "课程名不能为空"}), 400
    if len(name) > 20:
        return jsonify({"error": "课程名太长(≤20字)"}), 400
    if name in SYSTEM_COURSES:
        return jsonify({"error": f"「{name}」是系统分类,不能添加"}), 400
    courses = load_courses()
    if name in courses:
        return jsonify({"error": f"「{name}」已存在"}), 409
    courses.append(name)
    save_courses(courses)
    (INBOX / name).mkdir(parents=True, exist_ok=True)  # 同时建收件箱目录
    return jsonify({"ok": True, "courses": courses})


@app.route("/api/courses", methods=["DELETE"])
def api_courses_delete():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if name in SYSTEM_COURSES:
        return jsonify({"error": "系统分类不能删除"}), 400
    courses = load_courses()
    if name not in courses:
        return jsonify({"error": "课程不存在"}), 404
    # 有文件/笔记时禁止删除,防止数据丢失
    inbox_files = [p for p in (INBOX / name).rglob("*") if p.is_file()] if (INBOX / name).exists() else []
    note_files = [p for p in (NOTES / name).rglob("*") if p.is_file()] if (NOTES / name).exists() else []
    if inbox_files or note_files:
        return jsonify({"error": f"「{name}」下还有 {len(inbox_files) + len(note_files)} 个文件,不能删除"}), 409
    courses.remove(name)
    save_courses(courses)
    return jsonify({"ok": True, "courses": courses})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    course = request.form.get("course", "").strip()
    if course not in load_courses():
        course = DEFAULT_COURSE
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "没有收到文件"}), 400

    saved, errors = [], []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target_dir = INBOX / course
    target_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        if not f.filename:
            continue
        name = f.filename.replace("\\", "/").split("/")[-1]
        safe = "".join(c for c in name if c not in '\\/:*?"<>|\n\r')
        if not safe:
            safe = "unnamed"
        dest = target_dir / f"{stamp}-{safe}"
        try:
            f.save(dest)
            saved.append(dest.relative_to(INBOX).as_posix())
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")

    return jsonify({"saved": saved, "errors": errors, "course": course})


@app.route("/api/recent")
def api_recent():
    items = []
    for p in sorted(INBOX.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
        if p.is_file():
            items.append({"path": p.relative_to(INBOX).as_posix(),
                          "size": p.stat().st_size,
                          "time": time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime))})
    return jsonify(items)


@app.route("/api/run", methods=["POST"])
def api_run():
    """手动触发流水线(后台进程)"""
    try:
        proc = subprocess.run(["pgrep", "-f", r"python\s+.*pipeline\.py"],
                              capture_output=True, text=True, timeout=3)
        if proc.stdout.strip():
            return jsonify({"ok": False, "error": "流水线已在运行中"}), 409
    except Exception:  # noqa: BLE001
        pass
    env = dict(os.environ)
    env.update({"HF_ENDPOINT": "https://hf-mirror.com", "HF_HUB_DISABLE_XET": "1"})
    logf = open(BASE / "logs" / "pipeline.log", "a")
    subprocess.Popen([sys.executable, "pipeline.py"], cwd=str(BASE), env=env,
                     stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    return jsonify({"ok": True})


# ---------- 控制台:状态 ----------

@app.route("/api/status")
def api_status():
    import subprocess
    status = {}
    if STATUS_FILE.exists():
        status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    # 进程存活检测:状态说 running 但进程没了 → 中断
    try:
        proc = subprocess.run(["pgrep", "-f", r"python\s+.*pipeline\.py"],
                              capture_output=True, text=True, timeout=3)
        alive = bool(proc.stdout.strip())
    except Exception:  # noqa: BLE001
        alive = False
    if status.get("running") and not alive:
        status["running"] = False
        status["interrupted"] = True
        status["last_result"] = "interrupted"
    inbox_files = [p for p in INBOX.rglob("*") if p.is_file()]
    status["inbox_count"] = len(inbox_files)
    status["inbox_by_course"] = {}
    for p in inbox_files:
        rel = p.relative_to(INBOX)
        course = rel.parts[0] if len(rel.parts) > 1 else "?"
        status["inbox_by_course"][course] = status["inbox_by_course"].get(course, 0) + 1
    status["draft_count"] = len(list((NOTES / "未分类").glob("*/*.md"))) if (NOTES / "未分类").exists() else 0
    if PIPELINE_LOG.exists():
        status["log_tail"] = PIPELINE_LOG.read_text(encoding="utf-8").splitlines()[-8:]
    return jsonify(status)


@app.route("/api/log")
def api_log():
    n = int(request.args.get("n", 30))
    lines = PIPELINE_LOG.read_text(encoding="utf-8").splitlines() if PIPELINE_LOG.exists() else []
    return jsonify(lines[-n:])


# ---------- 控制台:草稿校对 ----------

@app.route("/api/drafts")
def api_drafts():
    drafts = []
    unclassified = NOTES / "未分类"
    if unclassified.exists():
        for session_dir in sorted(unclassified.iterdir()):
            if not session_dir.is_dir():
                continue
            mds = list(session_dir.glob("*.md"))
            if not mds:
                continue
            md = mds[0]
            txt = md.read_text(encoding="utf-8")
            title = session_dir.name
            for line in txt.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            att_dir = session_dir / "附件"
            attachments = [p.name for p in att_dir.iterdir() if p.is_file()] if att_dir.exists() else []
            drafts.append({
                "path": str(session_dir.relative_to(NOTES)),
                "name": session_dir.name,
                "title": title,
                "md": md.name,
                "attachments": attachments,
                "updated": time.strftime("%m-%d %H:%M", time.localtime(md.stat().st_mtime)),
            })
    return jsonify(drafts)


@app.route("/api/note")
def api_note():
    rel = request.args.get("path", "")
    p = safe_path(NOTES, rel)
    if p is None or not (p.is_dir() or p.is_file()):
        return jsonify({"error": "非法路径"}), 400
    if p.is_dir():
        mds = list(p.glob("*.md"))
        if not mds:
            return jsonify({"error": "该目录没有笔记"}), 404
        md = mds[0]
        session_dir = p
    else:
        md = p
        session_dir = p.parent
    content = md.read_text(encoding="utf-8")
    # 预览时隐藏 frontmatter(--- ... ---),同时读出课程
    course = ""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) == 3:
            fm = parts[1]
            m = re.search(r"^课程:\s*([^#\n]+)", fm, re.M)
            if m:
                course = m.group(1).strip()
            content = parts[2].lstrip("\n")
    att_dir = session_dir / "附件"
    attachments = []
    if att_dir.exists():
        for p in sorted(att_dir.iterdir()):
            if p.is_file():
                suffix = p.suffix.lower()
                kind = "image" if suffix in IMAGE_EXTS else ("audio" if suffix in AUDIO_EXTS else "file")
                attachments.append({
                    "name": p.name,
                    "kind": kind,
                    "size": p.stat().st_size,
                    "thumb": f"/api/thumb?path={rel}/附件/{p.name}" if kind == "image" else None,
                    "url": f"/api/file?path={rel}/附件/{p.name}",
                })
    status = ""
    raw = md.read_text(encoding="utf-8")
    ms = re.search(r"^状态:\s*(\S+)", raw, re.M)
    if ms:
        status = ms.group(1)
    title = md.name
    for line in content.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return jsonify({"path": rel, "name": md.name, "title": title,
                    "content": content, "course": course,
                    "date": time.strftime("%m-%d", time.localtime(md.stat().st_mtime)),
                    "updated": int(md.stat().st_mtime),
                    "status": status,
                    "attachments": attachments})


@app.route("/api/thumb")
def api_thumb():
    rel = request.args.get("path", "")
    fp = safe_path(NOTES, rel)
    if fp is None or not fp.is_file():
        return "", 404
    try:
        from PIL import Image
        import io
        img = Image.open(fp)
        img.thumbnail((360, 360))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=80)
        return Response(buf.getvalue(), mimetype="image/jpeg")
    except Exception:  # noqa: BLE001
        return "", 500


@app.route("/api/file")
def api_file():
    rel = request.args.get("path", "")
    fp = safe_path(NOTES, rel)
    if fp is None or not fp.is_file():
        return "", 404
    mime = {"srt": "text/plain", "txt": "text/plain; charset=utf-8",
            "m4a": "audio/mp4", "mp3": "audio/mpeg", "wav": "audio/wav",
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
    return Response(fp.read_bytes(), mimetype=mime.get(fp.suffix.lower().lstrip("."), "application/octet-stream"))


@app.route("/api/archive", methods=["POST"])
def api_archive():
    data = request.get_json(force=True)
    rel = data.get("path", "")
    course = data.get("course", "").strip()
    session_dir = safe_path(NOTES, rel)
    if session_dir is None or not session_dir.is_dir():
        return jsonify({"error": "非法路径"}), 400
    archive_courses = [c for c in load_courses() if c != "待分类"]
    if course not in archive_courses:
        return jsonify({"error": f"课程无效,可选:{archive_courses}"}), 400
    for md in session_dir.glob("*.md"):
        txt = md.read_text(encoding="utf-8")
        txt = re.sub(r"^课程:.*$", f"课程: {course}", txt, count=1, flags=re.M)
        txt = re.sub(r"^状态:.*$", "状态: 🟢 定稿", txt, count=1, flags=re.M)
        md.write_text(txt, encoding="utf-8")
    dest = NOTES / course / session_dir.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # 目标已存在(同节课先到的素材已成篇)→ 自动合并,不报错
        dest_att = dest / "附件"
        dest_att.mkdir(parents=True, exist_ok=True)
        src_att = session_dir / "附件"
        moved = 0
        if src_att.exists():
            for f in src_att.iterdir():
                if f.is_file():
                    t = dest_att / f.name
                    if t.exists():
                        t = dest_att / f"{f.stem}-dup{f.suffix}"
                    f.rename(t)
                    moved += 1
        src_md = next(session_dir.glob("*.md"), None)
        dst_md = next(dest.glob("*.md"), None)
        if src_md and dst_md:
            extra = src_md.read_text(encoding="utf-8")
            if extra.startswith("---"):
                parts = extra.split("---", 2)
                if len(parts) == 3:
                    extra = parts[2]
            body = dst_md.read_text(encoding="utf-8")
            body += f"\n\n## 📎 合并笔记(来自 {session_dir.name})\n" + demote_headings(extra.strip()) + "\n"
            body = re.sub(r"^状态:.*$", "状态: 🟡 草稿", body, count=1, flags=re.M)
            dst_md.write_text(body, encoding="utf-8")
        import shutil
        shutil.rmtree(session_dir, ignore_errors=True)
        # 合并后的完整笔记移回未分类,等待重新校对
        back = NOTES / "未分类" / dest.name
        if back.exists():
            back = NOTES / "未分类" / f"{dest.name}-2"
        try:
            dest.rename(back)
        except Exception:  # noqa: BLE001
            back = dest
        return jsonify({"ok": True, "merged": True,
                        "to": str(back.relative_to(NOTES)),
                        "attachments_moved": moved})

    session_dir.rename(dest)
    return jsonify({"ok": True, "to": str(dest.relative_to(NOTES))})


def demote_headings(text: str) -> str:
    """把追加进来的整篇笔记的标题降一级(# → ##),保持合并后结构平整"""
    return "\n".join("#" + ln if re.match(r"^#{1,4}\s", ln) else ln
                     for ln in text.splitlines())


@app.route("/api/merge", methods=["POST"])
def api_merge():
    """校对时手动合并:把 src 草稿并入 dst 笔记(附件 + 内容),删除 src"""
    data = request.get_json(force=True)
    src = safe_path(NOTES, data.get("src", ""))
    dst = safe_path(NOTES, data.get("dst", ""))
    if src is None or dst is None or not src.is_dir() or not dst.is_dir() or src == dst:
        return jsonify({"error": "非法路径"}), 400
    dst_md = next(dst.glob("*.md"), None)
    if dst_md is None:
        return jsonify({"error": "目标不是一篇笔记"}), 400
    dst_att = dst / "附件"
    dst_att.mkdir(parents=True, exist_ok=True)
    src_att = src / "附件"
    moved = 0
    if src_att.exists():
        for f in src_att.iterdir():
            if f.is_file():
                t = dst_att / f.name
                if t.exists():
                    t = dst_att / f"{f.stem}-dup{f.suffix}"
                f.rename(t)
                moved += 1
    src_md = next(src.glob("*.md"), None)
    if src_md:
        extra = src_md.read_text(encoding="utf-8")
        if extra.startswith("---"):
            parts = extra.split("---", 2)
            if len(parts) == 3:
                extra = parts[2]
        body = dst_md.read_text(encoding="utf-8")
        body += f"\n\n## 📎 合并笔记(来自 {src.name})\n" + demote_headings(extra.strip()) + "\n"
        body = re.sub(r"^状态:.*$", "状态: 🟡 草稿", body, count=1, flags=re.M)
        dst_md.write_text(body, encoding="utf-8")
    import shutil
    shutil.rmtree(src, ignore_errors=True)
    # 目标若已归档,移回未分类等待重新校对
    if dst.parent.name != "未分类":
        back = NOTES / "未分类" / dst.name
        if back.exists():
            back = NOTES / "未分类" / f"{dst.name}-2"
        try:
            dst.rename(back)
            dst = back
        except Exception:  # noqa: BLE001
            pass
    return jsonify({"ok": True, "to": str(dst.relative_to(NOTES)), "attachments_moved": moved})


# ---------- 笔记管理:重命名 / 移动课程 / 废纸篓 / 导出 ----------

def safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "-", title).strip(" .")
    return name[:60] or "笔记"


@app.route("/api/rename", methods=["POST"])
def api_rename():
    data = request.get_json(force=True)
    rel = data.get("path", "")
    title = data.get("title", "").strip()
    session_dir = safe_path(NOTES, rel)
    if session_dir is None or not session_dir.is_dir() or not title:
        return jsonify({"error": "非法路径或标题为空"}), 400
    mds = list(session_dir.glob("*.md"))
    if not mds:
        return jsonify({"error": "该目录没有笔记"}), 404
    md = mds[0]
    content = md.read_text(encoding="utf-8")
    lines = content.splitlines()
    found = False
    for i, ln in enumerate(lines):
        if ln.startswith("# ") and not ln.startswith("## "):
            lines[i] = f"# {title}"
            found = True
            break
    content = "\n".join(lines) if found else content + f"\n# {title}\n"
    md.write_text(content, encoding="utf-8")
    # 重命名文件:日期前缀 + 安全化标题
    m = re.search(r"^日期:\s*(\S+)", content, re.M)
    prefix = m.group(1) if m else session_dir.name.split("_")[0]
    new_md = session_dir / f"{prefix}-{safe_filename(title)}.md"
    i = 1
    while new_md.exists() and new_md != md:
        new_md = session_dir / f"{prefix}-{safe_filename(title)}-{i}.md"
        i += 1
    if new_md != md:
        md.rename(new_md)
    return jsonify({"ok": True, "name": new_md.name})


@app.route("/api/save", methods=["POST"])
def api_save():
    """网页编辑器保存:保留 frontmatter,替换正文,同步标题与文件名"""
    data = request.get_json(force=True)
    rel = data.get("path", "")
    title = (data.get("title") or "").strip()
    body = data.get("body") or ""
    if not title:
        return jsonify({"error": "标题不能为空"}), 400
    p = safe_path(NOTES, rel)
    if p is None or not p.is_dir():
        return jsonify({"error": "非法路径"}), 400
    mds = list(p.glob("*.md"))
    if not mds:
        return jsonify({"error": "没有笔记"}), 404
    md = mds[0]
    content = md.read_text(encoding="utf-8")
    parts = content.split("---\n", 2)
    fm = parts[1] if len(parts) >= 3 else ""
    new_content = f"---\n{fm}---\n# {title}\n\n{body.rstrip()}\n"
    md.write_text(new_content, encoding="utf-8")
    # 文件名同步:日期前缀 + 标题
    m = re.search(r"^日期:\s*(\S+)", fm, re.M)
    prefix = m.group(1) if m else p.name.split("_")[0]
    new_md = p / f"{prefix}-{safe_filename(title)}.md"
    i = 1
    while new_md.exists() and new_md != md:
        new_md = p / f"{prefix}-{safe_filename(title)}-{i}.md"
        i += 1
    if new_md != md:
        md.rename(new_md)
    return jsonify({"ok": True, "name": new_md.name})


@app.route("/api/move", methods=["POST"])
def api_move():
    data = request.get_json(force=True)
    rel = data.get("path", "")
    course = data.get("course", "").strip()
    session_dir = safe_path(NOTES, rel)
    if session_dir is None or not session_dir.is_dir():
        return jsonify({"error": "非法路径"}), 400
    archive_courses = [c for c in load_courses() if c != "待分类"]
    if course not in archive_courses:
        return jsonify({"error": f"课程无效,可选:{archive_courses}"}), 400
    for md in session_dir.glob("*.md"):
        txt = md.read_text(encoding="utf-8")
        txt = re.sub(r"^课程:.*$", f"课程: {course}", txt, count=1, flags=re.M)
        md.write_text(txt, encoding="utf-8")
    dest = NOTES / course / session_dir.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return jsonify({"error": f"目标课程已有同名笔记「{session_dir.name}」"}), 409
    session_dir.rename(dest)
    return jsonify({"ok": True, "to": str(dest.relative_to(NOTES))})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    """移入废纸篓(可恢复)"""
    data = request.get_json(force=True)
    rel = data.get("path", "")
    session_dir = safe_path(NOTES, rel)
    if session_dir is None or not session_dir.is_dir():
        return jsonify({"error": "非法路径"}), 400
    trash = NOTES / "废纸篓"
    trash.mkdir(exist_ok=True)
    dest = trash / session_dir.name
    i = 1
    while dest.exists():
        dest = trash / f"{session_dir.name}-{i}"
        i += 1
    session_dir.rename(dest)
    return jsonify({"ok": True, "to": str(dest.relative_to(NOTES))})


@app.route("/api/export")
def api_export():
    """打包笔记(md + 附件)为 zip 下载"""
    rel = request.args.get("path", "")
    p = safe_path(NOTES, rel)
    if p is None or not p.is_dir():
        return jsonify({"error": "非法路径"}), 400
    mds = list(p.glob("*.md"))
    if not mds:
        return jsonify({"error": "没有笔记"}), 404
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(mds[0], arcname=mds[0].name)
        att = p / "附件"
        if att.exists():
            for f in sorted(att.iterdir()):
                if f.is_file():
                    z.write(f, arcname=f"附件/{f.name}")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{p.name}.zip",
                     mimetype="application/zip")


@app.route("/sw.js")
def sw_js():
    """根作用域 Service Worker(PWA 安装需要 start_url 在 SW 作用域内)"""
    return send_from_directory(BASE / "static" / "app", "sw.js",
                               mimetype="application/javascript",
                               max_age=0)


# ---------- 控制台:笔记浏览 ----------

def _note_summary(txt: str, limit: int = 90) -> str:
    """正文第一段非标题文字,作卡片摘要(跳过 frontmatter 与引用行)"""
    body = txt.split("---\n", 2)[-1] if txt.count("---") >= 2 else txt
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith(">"):
            return s[:limit]
    return ""


def _session_info(sd, course):
    mds = list(sd.glob("*.md"))
    if not mds:
        return None
    txt = mds[0].read_text(encoding="utf-8")
    title = sd.name
    for line in txt.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    m = re.search(r"^状态:\s*(\S+)", txt, re.M)
    status = m.group(1) if m else ""
    audios = images = 0
    att = sd / "附件"
    if att.exists():
        for f in att.iterdir():
            if f.suffix.lower() in AUDIO_EXTS:
                audios += 1
            elif f.suffix.lower() in IMAGE_EXTS:
                images += 1
    return {
        "name": sd.name, "title": title, "course": course,
        "path": str(sd.relative_to(NOTES)),
        "date": time.strftime("%m-%d", time.localtime(mds[0].stat().st_mtime)),
        "updated": int(mds[0].stat().st_mtime),
        "status": status, "audios": audios, "images": images,
        "summary": _note_summary(txt),
    }


@app.route("/api/notes")
def api_notes():
    tree = []
    for course_dir in sorted(NOTES.iterdir()):
        if not course_dir.is_dir() or course_dir.name in ("未分类", "废纸篓"):
            continue
        sessions = []
        for sd in sorted(course_dir.iterdir()):
            if sd.is_dir():
                info = _session_info(sd, course_dir.name)
                if info:
                    sessions.append(info)
        # 课程根目录下的散装 .md
        for f in sorted(course_dir.glob("*.md")):
            txt = f.read_text(encoding="utf-8")
            title = f.stem
            for line in txt.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            m = re.search(r"^状态:\s*(\S+)", txt, re.M)
            sessions.append({
                "name": f.stem, "title": title, "course": course_dir.name,
                "path": str(f.relative_to(NOTES)),
                "date": time.strftime("%m-%d", time.localtime(f.stat().st_mtime)),
                "updated": int(f.stat().st_mtime),
                "status": m.group(1) if m else "",
                "audios": 0, "images": 0,
                "summary": _note_summary(txt),
            })
        tree.append({"course": course_dir.name, "sessions": sessions})
    return jsonify(tree)


@app.route("/api/search")
def api_search():
    """全文搜索笔记(标题+正文),返回匹配片段"""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    results = []
    for course_dir in sorted(NOTES.iterdir()):
        if not course_dir.is_dir() or course_dir.name == "废纸篓":
            continue
        for sd in sorted(course_dir.iterdir()):
            if sd.is_dir():
                info = _session_info(sd, course_dir.name)
                if info:
                    txt = (sd / [m for m in sd.glob("*.md")][0]).read_text(encoding="utf-8") \
                        if sd.glob("*.md") else ""
                    _maybe_append_search(info, txt, q, results)
            elif sd.suffix.lower() == ".md":
                txt = sd.read_text(encoding="utf-8")
                info = _session_info(sd.parent, course_dir.name)
                _maybe_append_search(info, txt, q, results) if info else None
    results.sort(key=lambda r: r["updated"], reverse=True)
    return jsonify(results[:50])


def _maybe_append_search(info, txt, q, results):
    """标题或正文含 q → 生成带上下文的片段"""
    body = txt.split("---\n", 2)[-1] if txt.count("---") >= 2 else txt
    low, ql = body.lower(), q.lower()
    hit = ql in (info["title"] or "").lower() or ql in low
    if not hit:
        return
    snippet = ""
    if ql in low:
        i = low.find(ql)
        start = max(0, i - 30)
        snippet = ("…" if start > 0 else "") + body[start:i + len(q) + 60].replace("\n", " ") + "…"
    else:
        snippet = _note_summary(body)
    results.append({**info, "snippet": snippet})


if __name__ == "__main__":
    host = cfg["upload"]["host"]
    port = cfg["upload"]["port"]
    print(f"蒸馏服务: http://{host}:{port} (控制台 /console.html)")
    app.run(host=host, port=port, threaded=True)
