"""本地档案：简历历史 + 当前使用的那份。

多份简历各自存一个文件（records/resumes/{id}.json），profile.json 只记"当前用哪份"。
这样换简历不会覆盖旧的那份，经历解析结果也能按简历分开缓存。
"""

import hashlib
import json
import os
import time

from . import scope

ROOT = os.environ.get("INTERVIEW_DATA_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _profile_path() -> str:
    return scope.user_dir("profile.json")


def _resume_dir() -> str:
    return scope.user_dir("resumes")


def _read(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _rid(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 简历历史

def add_resume(text: str, filename: str = "") -> str:
    """存一份简历并设为当前。同一份内容重复上传不会产生新记录。"""
    text = (text or "").strip()
    if not text:
        return ""
    rid = _rid(text)
    path = os.path.join(_resume_dir(), f"{rid}.json")
    old = _read(path, {})
    _write(path, {
        "id": rid,
        "filename": filename or old.get("filename") or "简历",
        "text": text,
        "created_at": old.get("created_at") or time.strftime("%Y-%m-%d %H:%M"),
        "used_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    set_active(rid)
    return rid


def list_resumes() -> list[dict]:
    if not os.path.isdir(_resume_dir()):
        return []
    out = []
    for name in os.listdir(_resume_dir()):
        if not name.endswith(".json"):
            continue
        d = _read(os.path.join(_resume_dir(), name), None)
        if isinstance(d, dict) and d.get("text"):
            out.append({"id": d.get("id", name[:-5]), "filename": d.get("filename", "简历"),
                        "chars": len(d["text"]), "created_at": d.get("created_at", ""),
                        "used_at": d.get("used_at", "")})
    return sorted(out, key=lambda x: x.get("used_at") or "", reverse=True)


def load_resume(rid: str = "") -> str:
    rid = rid or active_id()
    if not rid:
        return ""
    d = _read(os.path.join(_resume_dir(), f"{rid}.json"), {})
    return (d.get("text") or "").strip()


def active_id() -> str:
    return (_read(_profile_path(), {}).get("active_id") or "")


def set_active(rid: str) -> None:
    data = _read(_profile_path(), {})
    data["active_id"] = rid
    data.update({"updated_at": time.strftime("%Y-%m-%d %H:%M")})
    _write(_profile_path(), data)


def delete_resume(rid: str) -> None:
    path = os.path.join(_resume_dir(), f"{rid}.json")
    if os.path.exists(path):
        os.remove(path)
    if active_id() == rid:
        remaining = list_resumes()
        set_active(remaining[0]["id"] if remaining else "")


# ---------------------------------------------------------------- 兼容旧接口

def save_resume(text: str, filename: str = "") -> None:
    add_resume(text, filename)


def load() -> dict:
    """旧的元信息接口（文件名/时间）。"""
    rid = active_id()
    d = _read(os.path.join(_resume_dir(), f"{rid}.json"), {}) if rid else {}
    base = _read(_profile_path(), {})
    if d:
        return {"resume": d.get("text", ""), "resume_from": d.get("filename", ""),
                "updated_at": d.get("used_at") or d.get("created_at", "")}
    return base


def clear_resume() -> None:
    rid = active_id()
    if rid:
        delete_resume(rid)
