"""标准题解 API（每题一篇，按题目分片存储）。

权限模型：
  - 普通用户：只读；
  - 管理员（admin）与授权用户（judge）：可发布/编辑/删除。

可见性：
  题目处于任何「未开始或进行中」的竞赛中时，题解对普通用户隐藏
  （不返回正文）；管理员/授权用户可预览并看到隐藏提示。
  全部关联竞赛结束后自动公开。
"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, get_current_user, require_solution_editor
from backend.judge.ranking import contest_status
from backend.storage import read_json, atomic_write_json, list_files
from backend.utils import now_iso, gen_id, sanitize_id

solutions_bp = Blueprint("solutions", __name__)

MAX_TITLE_LEN = 200
MAX_CONTENT_LEN = 100_000
MAX_CODE_LEN = 40_000


def _problem_path(problem_id):
    return os.path.join(config.PROBLEMS_DIR, f"{sanitize_id(problem_id)}.json")


def _solution_path(problem_id):
    return os.path.join(config.SOLUTIONS_DIR, f"{sanitize_id(problem_id)}.json")


def _contest_problem_ids(contest):
    """兼容竞赛题目列表的两种写法：字符串 ID 或 {"problem_id": ...}。"""
    ids = []
    for item in contest.get("problems", []):
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            pid = item.get("problem_id")
            if pid:
                ids.append(pid)
    return ids


def blocking_contests(problem_id):
    """返回导致题解必须隐藏的竞赛（未开始或进行中且包含该题），按结束时间升序。"""
    blocked = []
    for cid in list_files(config.CONTESTS_DIR):
        c = read_json(os.path.join(config.CONTESTS_DIR, f"{cid}.json"))
        if not c or not c.get("visible", True):
            continue
        if problem_id not in _contest_problem_ids(c):
            continue
        if contest_status(c) in ("upcoming", "running"):
            blocked.append(c)
    blocked.sort(key=lambda c: c.get("end_time") or "")
    return blocked


def _viewer_can_preview(user):
    return bool(user and user.get("role") in ("admin", "judge"))


@solutions_bp.get("/problems/<problem_id>/solution")
def get_solution(problem_id):
    problem = read_json(_problem_path(problem_id))
    if not problem:
        return err("题目不存在", 404)

    user = get_current_user()
    solution = read_json(_solution_path(problem_id))
    active = blocking_contests(problem_id)

    # 比赛期间：普通用户拿不到正文；管理员/授权用户可预览
    if active:
        unlock_at = active[0].get("end_time")
        contest_titles = [c.get("title", c.get("id")) for c in active]
        if not _viewer_can_preview(user) or solution is None:
            return ok({
                "exists": False if solution is None else _viewer_can_preview(user),
                "hidden": True,
                "unlock_at": unlock_at,
                "contest_titles": contest_titles,
            })
        out = dict(solution)
        out["hidden"] = True
        out["unlock_at"] = unlock_at
        out["contest_titles"] = contest_titles
        out["preview"] = True
        return ok(out)

    if solution is None:
        return ok({"exists": False, "hidden": False})
    out = dict(solution)
    out["hidden"] = False
    return ok(out)


@solutions_bp.put("/problems/<problem_id>/solution")
@require_solution_editor
def put_solution(problem_id):
    problem = read_json(_problem_path(problem_id))
    if not problem:
        return err("题目不存在", 404)

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    code = data.get("code") or ""
    language = (data.get("language") or "").strip()
    if not title:
        return err("题解标题不能为空", 400)
    if not content and not code.strip():
        return err("题解内容不能为空", 400)
    if language and language not in config.LANGUAGES:
        return err("不支持的语言", 400)

    existing = read_json(_solution_path(problem_id))
    if existing:
        solution = dict(existing)
        solution.update({
            "title": title[:MAX_TITLE_LEN],
            "content": content[:MAX_CONTENT_LEN],
            "code": code[:MAX_CODE_LEN],
            "language": language or None,
            "updated_at": now_iso(),
            "updater_id": request.user["id"],
            "updater_name": request.user.get("nickname") or request.user.get("username", ""),
        })
    else:
        solution = {
            "id": gen_id("s"),
            "problem_id": problem_id,
            "title": title[:MAX_TITLE_LEN],
            "content": content[:MAX_CONTENT_LEN],
            "code": code[:MAX_CODE_LEN],
            "language": language or None,
            "author_id": request.user["id"],
            "author_name": request.user.get("nickname") or request.user.get("username", ""),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "updater_id": request.user["id"],
            "updater_name": request.user.get("nickname") or request.user.get("username", ""),
        }
    atomic_write_json(_solution_path(problem_id), solution)

    active = blocking_contests(problem_id)
    out = dict(solution)
    out["hidden"] = bool(active)
    out["preview"] = bool(active)
    if active:
        out["unlock_at"] = active[0].get("end_time")
        out["contest_titles"] = [c.get("title", c.get("id")) for c in active]
    return ok(out)


@solutions_bp.delete("/problems/<problem_id>/solution")
@require_solution_editor
def delete_solution(problem_id):
    path = _solution_path(problem_id)
    if not os.path.exists(path):
        return err("题解不存在", 404)
    solution = read_json(path) or {}
    is_admin = request.user.get("role") == "admin"
    is_author = solution.get("author_id") == request.user["id"]
    if not (is_admin or is_author):
        return err("只能删除自己发布的题解", 403, 403)
    os.remove(path)
    return ok()
