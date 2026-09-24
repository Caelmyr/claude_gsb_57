"""官方题解 API（按题目分片存储）。

权限模型：
  - 发布：管理员（admin）或授权用户（judge 角色）；
  - 编辑/删除：admin 可管理全部题解，judge 只能管理自己发布的；
  - 阅读：所有人（含未登录访客），普通用户只读。

比赛保护：题目若属于任一「尚未结束」的竞赛（未开始或进行中），
其题解对普通用户隐藏，待比赛结束后自动公开；管理员与授权用户
不受限制，便于赛间提前录入与维护。
"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth, get_current_user
from backend.judge.ranking import contest_status
from backend.storage import read_json, locked_update, list_files
from backend.utils import now_iso, gen_id, sanitize_id, truncate

solutions_bp = Blueprint("solutions", __name__)

# 可发布题解的角色：管理员 / 授权用户
AUTHOR_ROLES = ("admin", "judge")

# 题解正文长度上限（字符）
MAX_CONTENT_LEN = 20000


def _solutions_path(problem_id):
    return os.path.join(config.SOLUTIONS_DIR, f"{sanitize_id(problem_id)}.json")


def _load_solutions(problem_id):
    data = read_json(_solutions_path(problem_id))
    return (data or {}).get("solutions", []) if data else []


def _problem_exists(problem_id):
    return os.path.exists(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"))


def _can_publish(user):
    return user is not None and user.get("role") in AUTHOR_ROLES


def _can_modify(user, solution):
    """admin 可改任意题解；授权用户只能改自己发布的。"""
    if user is None:
        return False
    if user.get("role") == "admin":
        return True
    return user.get("role") in AUTHOR_ROLES and solution.get("author") == user.get("id")


def _active_contest(problem_id):
    """返回包含该题且尚未结束的竞赛（无则 None）。"""
    for cid in list_files(config.CONTESTS_DIR):
        c = read_json(os.path.join(config.CONTESTS_DIR, f"{cid}.json"))
        if not c:
            continue
        ids = [p.get("problem_id") for p in c.get("problems", [])]
        if problem_id in ids and contest_status(c) != "ended":
            return c
    return None


@solutions_bp.get("/problems/<problem_id>/solutions")
def list_solutions(problem_id):
    if not _problem_exists(problem_id):
        return err("题目不存在", 404)
    user = get_current_user()
    contest = _active_contest(problem_id)
    if contest and not _can_publish(user):
        # 比赛未结束：对普通用户隐藏题解内容，仅告知公开时间
        return ok({
            "hidden": True,
            "contest_title": contest.get("title"),
            "contest_end_time": contest.get("end_time"),
            "total": 0,
            "items": [],
        })
    solutions = _load_solutions(problem_id)
    solutions = sorted(solutions, key=lambda s: s.get("created_at", ""), reverse=True)
    return ok({"hidden": False, "total": len(solutions), "items": solutions})


@solutions_bp.post("/problems/<problem_id>/solutions")
@require_auth
def create_solution(problem_id):
    if not _can_publish(request.user):
        return err("需要管理员或授权用户权限", 403, 403)
    if not _problem_exists(problem_id):
        return err("题目不存在", 404)
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title:
        return err("题解标题不能为空", 400)
    if not content:
        return err("题解内容不能为空", 400)
    solution = {
        "id": gen_id("s"),
        "problem_id": problem_id,
        "title": title,
        "content": truncate(content, MAX_CONTENT_LEN),
        "author": request.user["id"],
        "author_name": request.user.get("nickname") or request.user.get("username", ""),
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }

    def _upd(d):
        if d is None:
            d = {"problem_id": problem_id, "solutions": []}
        d.setdefault("solutions", []).append(solution)
        return d

    locked_update(_solutions_path(problem_id), _upd, default=None)
    return ok(solution)


@solutions_bp.put("/problems/<problem_id>/solutions/<solution_id>")
@require_auth
def update_solution(problem_id, solution_id):
    data = request.get_json(silent=True) or {}
    state = {"status": 404, "solution": None}

    def _upd(d):
        if not d:
            return d
        for s in d.get("solutions", []):
            if s.get("id") != solution_id:
                continue
            if not _can_modify(request.user, s):
                state["status"] = 403
                return d
            if "title" in data:
                title = (data.get("title") or "").strip()
                if not title:
                    state["status"] = 400
                    return d
                s["title"] = title
            if "content" in data:
                content = (data.get("content") or "").strip()
                if not content:
                    state["status"] = 400
                    return d
                s["content"] = truncate(content, MAX_CONTENT_LEN)
            s["updated_at"] = now_iso()
            state["status"] = 0
            state["solution"] = s
            return d
        return d

    locked_update(_solutions_path(problem_id), _upd, default=None)
    if state["status"] == 404:
        return err("题解不存在", 404)
    if state["status"] == 403:
        return err("无权修改该题解", 403, 403)
    if state["status"] == 400:
        return err("题解标题或内容不能为空", 400)
    return ok(state["solution"])


@solutions_bp.delete("/problems/<problem_id>/solutions/<solution_id>")
@require_auth
def delete_solution(problem_id, solution_id):
    state = {"status": 404}

    def _upd(d):
        if not d:
            return d
        for s in d.get("solutions", []):
            if s.get("id") != solution_id:
                continue
            if not _can_modify(request.user, s):
                state["status"] = 403
                return d
            d["solutions"] = [x for x in d.get("solutions", [])
                              if x.get("id") != solution_id]
            state["status"] = 0
            return d
        return d

    locked_update(_solutions_path(problem_id), _upd, default=None)
    if state["status"] == 404:
        return err("题解不存在", 404)
    if state["status"] == 403:
        return err("无权删除该题解", 403, 403)
    return ok()
