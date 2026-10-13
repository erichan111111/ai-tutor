"""Persistent learning workspace. Planning and review never require an LLM."""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from .utils import new_id, now_iso


SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, exam_date TEXT NOT NULL,
    daily_minutes INTEGER NOT NULL, goal TEXT NOT NULL, topics TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS study_tasks (
    id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id),
    title TEXT NOT NULL, topic TEXT NOT NULL, kind TEXT NOT NULL,
    due_date TEXT NOT NULL, minutes INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT NOT NULL DEFAULT '', plan_key TEXT,
    created_at TEXT NOT NULL, UNIQUE(course_id, plan_key)
);
CREATE TABLE IF NOT EXISTS study_notes (
    id TEXT PRIMARY KEY, course_id TEXT REFERENCES courses(id),
    title TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
    reflection TEXT NOT NULL DEFAULT '', source_turn_id TEXT UNIQUE,
    simulated INTEGER NOT NULL DEFAULT 0, degraded INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '', due_date TEXT NOT NULL,
    interval_days INTEGER NOT NULL DEFAULT 0, review_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS study_reviews (
    id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES study_notes(id) ON DELETE CASCADE,
    rating TEXT NOT NULL, reviewed_on TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(note_id, reviewed_on)
);
CREATE INDEX IF NOT EXISTS idx_study_tasks_due ON study_tasks(course_id, due_date);
CREATE INDEX IF NOT EXISTS idx_study_notes_due ON study_notes(due_date);
"""


def text_field(value: Any, label: str, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}需要填写文字")
    value = value.strip()
    if required and not value:
        raise ValueError(f"请填写{label}")
    if len(value) > limit:
        raise ValueError(f"{label}最多 {limit} 字")
    return value


def integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label}应为 {minimum}–{maximum} 的整数")
    return value


def day(value: Any) -> str:
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("日期格式应为 YYYY-MM-DD") from None
    return value


class StudyWorkspace:
    def __init__(self, storage):
        self.db = storage
        with storage._lock:
            storage._conn.executescript(SCHEMA)
            storage._conn.commit()

    def course(self, course_id: str) -> dict:
        course_id = text_field(course_id, "课程编号", 100)
        row = self.db._row("SELECT * FROM courses WHERE id = ?", [course_id])
        if not row:
            raise LookupError("课程不存在")
        row["topics"] = json.loads(row["topics"])
        return row

    def courses(self) -> list:
        rows = self.db._rows("SELECT * FROM courses ORDER BY archived, exam_date, created_at")
        for row in rows:
            row["topics"] = json.loads(row["topics"])
        return rows

    def save_course(self, payload: dict, course_id: str | None = None) -> dict:
        name = text_field(payload.get("name", ""), "课程名称", 80)
        goal = text_field(payload.get("goal", ""), "学习目标", 500, False)
        exam = day(payload.get("exam_date"))
        minutes = integer(payload.get("daily_minutes", 60), "每日学习时间", 15, 480)
        topics = payload.get("topics", [])
        if not isinstance(topics, list) or not 1 <= len(topics) <= 30:
            raise ValueError("请填写 1–30 个复习主题，每行一个")
        topics = list(dict.fromkeys(text_field(t, "复习主题", 100) for t in topics))
        values = [name, exam, minutes, goal, json.dumps(topics, ensure_ascii=False)]
        if course_id:
            self.course(course_id)
            self.db._exec("UPDATE courses SET name=?, exam_date=?, daily_minutes=?, goal=?, topics=? WHERE id=?",
                          values + [course_id])
        else:
            course_id = new_id()
            self.db._exec("INSERT INTO courses (id,name,exam_date,daily_minutes,goal,topics,created_at) VALUES (?,?,?,?,?,?,?)",
                          [course_id] + values + [now_iso()])
        return self.course(course_id)

    def archive_course(self, course_id: str, archived: bool) -> dict:
        self.course(course_id)
        if not isinstance(archived, bool):
            raise ValueError("archived 应为布尔值")
        self.db._exec("UPDATE courses SET archived=? WHERE id=?", [int(archived), course_id])
        return self.course(course_id)

    def tasks(self, course_id: str | None = None) -> list:
        if course_id:
            self.course(course_id)
        return self.db._rows(
            "SELECT t.*, c.name AS course_name, c.archived FROM study_tasks t JOIN courses c ON c.id=t.course_id "
            + ("WHERE t.course_id=? " if course_id else "") + "ORDER BY t.due_date, t.created_at, t.rowid",
            [course_id] if course_id else [])

    def add_task(self, payload: dict) -> dict:
        course = self.course(payload.get("course_id"))
        if course["archived"]:
            raise ValueError("请先恢复课程再添加任务")
        title = text_field(payload.get("title", ""), "任务名称", 200)
        topic = text_field(payload.get("topic", title), "主题", 200)
        due = day(payload.get("due_date"))
        minutes = integer(payload.get("minutes", 25), "任务时长", 5, 480)
        task_id = new_id()
        self.db._exec("INSERT INTO study_tasks (id,course_id,title,topic,kind,due_date,minutes,created_at) VALUES (?,?,?,?,?,?,?,?)",
                      [task_id, course["id"], title, topic, "custom", due, minutes, now_iso()])
        return self.db._row("SELECT * FROM study_tasks WHERE id=?", [task_id])

    def update_task(self, task_id: str, payload: dict) -> dict:
        task_id = text_field(task_id, "任务编号", 100)
        row = self.db._row("SELECT * FROM study_tasks WHERE id=?", [task_id])
        if not row:
            raise LookupError("任务不存在")
        allowed = {"done", "due_date", "title", "minutes"}
        if not payload or set(payload) - allowed:
            raise ValueError("仅支持完成状态、任务日期、标题与时长")
        done = payload.get("done", bool(row["done"]))
        if not isinstance(done, bool):
            raise ValueError("done 应为布尔值")
        due = day(payload.get("due_date", row["due_date"]))
        title = text_field(payload.get("title", row["title"]), "任务名称", 200)
        minutes = integer(payload.get("minutes", row["minutes"]), "任务时长", 5, 480)
        completed = (row["completed_at"] or now_iso()) if done else ""
        self.db._exec("UPDATE study_tasks SET done=?, due_date=?, completed_at=?, title=?, minutes=? WHERE id=?",
                      [int(done), due, completed, title, minutes, task_id])
        return self.db._row("SELECT * FROM study_tasks WHERE id=?", [task_id])

    def plan(self, course_id: str, today: str) -> dict:
        today = day(today)
        course = self.course(course_id)
        if course["archived"]:
            raise ValueError("请先恢复课程再安排复习")
        start, exam = date.fromisoformat(today), date.fromisoformat(course["exam_date"])
        if exam < start:
            raise ValueError("考试日期已过，请先更新课程日期")
        if (exam - start).days > 365:
            raise ValueError("请将考试日期设置在未来一年内")
        # One plan per course. Repeated clicks never duplicate or erase the learner's work.
        with self.db._lock, self.db._conn:
            existing = self.db._row("SELECT COUNT(*) AS n FROM study_tasks WHERE course_id=? AND plan_key IS NOT NULL", [course_id])
            if not existing["n"]:
                cursor_day = start
                occupied = {}
                for task in self.tasks(course_id):
                    if not task["done"]:
                        occupied[task["due_date"]] = occupied.get(task["due_date"], 0) + task["minutes"]
                stages = [("understand", "梳理", 20), ("practice", "练习", 25), ("recall", "闭卷自测", 15)]
                for index, topic in enumerate(course["topics"]):
                    for kind, label, estimate in stages:
                        duration = min(estimate, course["daily_minutes"])
                        while occupied.get(cursor_day.isoformat(), 0) + duration > course["daily_minutes"]:
                            cursor_day += timedelta(days=1)
                        self.db._conn.execute(
                            "INSERT INTO study_tasks (id,course_id,title,topic,kind,due_date,minutes,plan_key,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            [new_id(), course_id, f"{label} · {topic}", topic, kind, cursor_day.isoformat(), duration,
                             f"{index}:{kind}", now_iso()])
                        occupied[cursor_day.isoformat()] = occupied.get(cursor_day.isoformat(), 0) + duration
        tasks = self.tasks(course_id)
        after_exam = sum(not t["done"] and t["due_date"] > course["exam_date"] for t in tasks)
        return {"tasks": tasks, "warning": f"有 {after_exam} 个任务排在考试之后。请减少主题、增加可用时间或手动调整日期。" if after_exam else "",
                "basis": "根据你填写的主题安排梳理、练习、闭卷自测；这是可调整的时间安排，不是掌握程度诊断。"}

    def notes(self, course_id: str | None = None) -> list:
        if course_id:
            self.course(course_id)
        return self.db._rows(
            "SELECT n.*, c.name AS course_name, COALESCE(c.archived,0) AS archived, "
            "(SELECT session_id FROM turns WHERE id=n.source_turn_id) AS source_session_id FROM study_notes n "
            "LEFT JOIN courses c ON c.id=n.course_id " + ("WHERE n.course_id=? " if course_id else "")
            + "ORDER BY n.due_date, n.created_at DESC", [course_id] if course_id else [])

    def add_note(self, payload: dict, today: str) -> dict:
        today = day(today)
        course_id = payload.get("course_id") or None
        if course_id:
            self.course(course_id)
        title = text_field(payload.get("title", ""), "笔记标题", 200)
        reflection = text_field(payload.get("reflection", ""), "错因或复习重点", 4000, False)
        source = payload.get("source_turn_id") or None
        simulated, degraded, model = 0, 0, "手工笔记"
        if source:
            source = text_field(source, "回答编号", 100)
            turn = self.db._row("SELECT * FROM turns WHERE id=?", [source])
            if not turn:
                raise LookupError("原始回答不存在")
            session = self.db.get_session(turn["session_id"])
            source_course = session["meta"].get("course_id")
            if source_course and source_course != course_id:
                raise ValueError("笔记课程需要与原始会话一致")
            question, answer = turn["user_prompt"], turn["output_raw"]
            simulated, degraded, model = turn["simulated"], turn["degraded"], turn["model"]
        else:
            question = text_field(payload.get("question", ""), "自测问题", 8000)
            answer = text_field(payload.get("answer", ""), "参考答案", 50000)
        with self.db._lock, self.db._conn:
            if source:
                existing = self.db._row("SELECT * FROM study_notes WHERE source_turn_id=?", [source])
                if existing:
                    return existing
            note_id = new_id()
            self.db._conn.execute(
                "INSERT INTO study_notes (id,course_id,title,question,answer,reflection,source_turn_id,simulated,degraded,model,due_date,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [note_id, course_id, title, question, answer, reflection, source, simulated, degraded, model, today, now_iso()])
        return self.db._row("SELECT * FROM study_notes WHERE id=?", [note_id])

    def review(self, note_id: str, rating: str, today: str) -> dict:
        note_id = text_field(note_id, "笔记编号", 100)
        today = day(today)
        if rating not in ("again", "hard", "good"):
            raise ValueError("请选择还不会、有点难或已掌握")
        with self.db._lock, self.db._conn:
            note = self.db._row("SELECT * FROM study_notes WHERE id=?", [note_id])
            if not note:
                raise LookupError("笔记不存在")
            if self.db._row("SELECT id FROM study_reviews WHERE note_id=? AND reviewed_on=?", [note_id, today]):
                raise ValueError("今天已记录这张卡片的自评，明天再复习吧")
            interval = 1 if rating == "again" else (2 if rating == "hard" else min(60, max(3, note["interval_days"] * 2)))
            due = (date.fromisoformat(today) + timedelta(days=interval)).isoformat()
            self.db._conn.execute("INSERT INTO study_reviews (id,note_id,rating,reviewed_on,created_at) VALUES (?,?,?,?,?)",
                                  [new_id(), note_id, rating, today, now_iso()])
            self.db._conn.execute("UPDATE study_notes SET interval_days=?,due_date=?,review_count=review_count+1 WHERE id=?",
                                  [interval, due, note_id])
        return self.db._row("SELECT * FROM study_notes WHERE id=?", [note_id])

    def edit_note(self, note_id: str, payload: dict) -> dict:
        note_id = text_field(note_id, "笔记编号", 100)
        if not self.db._row("SELECT id FROM study_notes WHERE id=?", [note_id]):
            raise LookupError("笔记不存在")
        title = text_field(payload.get("title", ""), "笔记标题", 200)
        reflection = text_field(payload.get("reflection", ""), "错因或复习重点", 4000, False)
        self.db._exec("UPDATE study_notes SET title=?,reflection=? WHERE id=?", [title, reflection, note_id])
        return self.db._row("SELECT * FROM study_notes WHERE id=?", [note_id])

    def delete_note(self, note_id: str) -> dict:
        note_id = text_field(note_id, "笔记编号", 100)
        if not self.db._row("SELECT id FROM study_notes WHERE id=?", [note_id]):
            raise LookupError("笔记不存在")
        self.db._exec("DELETE FROM study_notes WHERE id=?", [note_id])
        return {"deleted": True}

    def snapshot(self, today: str) -> dict:
        today = day(today)
        courses, tasks, notes = self.courses(), self.tasks(), self.notes()
        active_tasks = [t for t in tasks if not t["archived"]]
        due_tasks = [t for t in active_tasks if not t["done"] and t["due_date"] <= today]
        due_notes = [n for n in notes if not n["archived"] and n["due_date"] <= today]
        for course in courses:
            own = [t for t in tasks if t["course_id"] == course["id"]]
            course["task_count"] = len(own)
            course["completed_count"] = sum(t["done"] for t in own)
            course["days_left"] = (date.fromisoformat(course["exam_date"]) - date.fromisoformat(today)).days
            course["over_capacity"] = any(not t["done"] and t["due_date"] > course["exam_date"] for t in own)
        reviews_today = self.db._row("SELECT COUNT(*) AS n FROM study_reviews r JOIN study_notes n ON n.id=r.note_id "
                                     "LEFT JOIN courses c ON c.id=n.course_id WHERE reviewed_on=? AND COALESCE(c.archived,0)=0", [today])["n"]
        return {"today": today, "courses": courses, "tasks": tasks, "notes": notes,
                "due_tasks": due_tasks, "due_notes": due_notes,
                "summary": {"active_courses": sum(not c["archived"] for c in courses),
                            "completed_tasks": sum(t["done"] for t in active_tasks),
                            "total_tasks": len(active_tasks), "due_reviews": len(due_notes),
                            "reviews_today": reviews_today, "due_minutes": sum(t["minutes"] for t in due_tasks)}}

    def export(self) -> dict:
        # No configuration or API credentials. A consistent snapshot, including original answers.
        with self.db._lock:
            return {"format": "ai-tutor-study-export", "version": 1, "exported_at": now_iso(),
                    "courses": self.courses(), "tasks": self.tasks(), "notes": self.notes(),
                    "reviews": self.db._rows("SELECT * FROM study_reviews ORDER BY created_at"),
                    "sessions": [self.db.get_session(s["id"]) for s in self.db._rows("SELECT id FROM sessions ORDER BY created_at")]}
