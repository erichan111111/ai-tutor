"""本地存储层（SQLite）。

对应阶段二"交付物整理"与阶段三"记录方式"的要求：
  测试序号 / 模块 / 测试主题 / 两版输出评价 / 完整保留提示词原文与输出原文。

所有对话、提示词原文、输出原文、模型元信息与评测成绩都会持久化，
既可以在 Web 界面里回看，也可以用 `python -m ai_tutor report` 导出 Markdown 报告。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .utils import new_id, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    subject      TEXT NOT NULL DEFAULT '',
    module       TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    meta         TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS turns (
    id             TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    module         TEXT NOT NULL DEFAULT '',
    role           TEXT NOT NULL,
    mode           TEXT NOT NULL DEFAULT '',
    prompt_raw     TEXT NOT NULL DEFAULT '',
    system_prompt  TEXT NOT NULL DEFAULT '',
    user_prompt    TEXT NOT NULL DEFAULT '',
    output_raw     TEXT NOT NULL DEFAULT '',
    provider       TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    elapsed_ms     INTEGER NOT NULL DEFAULT 0,
    prompt_tokens  INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    simulated      INTEGER NOT NULL DEFAULT 0,
    degraded       INTEGER NOT NULL DEFAULT 0,
    degraded_reason TEXT NOT NULL DEFAULT '',
    structured     TEXT NOT NULL DEFAULT '',
    metrics        TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    module      TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_interactions (
    id          TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    case_type   TEXT NOT NULL,
    module      TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    question    TEXT NOT NULL DEFAULT '',
    mode        TEXT NOT NULL DEFAULT '',
    answer      TEXT NOT NULL DEFAULT '',
    mode_detected TEXT NOT NULL DEFAULT '',
    flags       TEXT NOT NULL DEFAULT '[]',
    provider    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    config      TEXT NOT NULL DEFAULT '{}',
    metrics     TEXT NOT NULL DEFAULT '{}',
    detail      TEXT NOT NULL DEFAULT '{}',
    file_path   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_risk_case ON risk_interactions(case_id);
CREATE INDEX IF NOT EXISTS idx_eval_kind ON eval_runs(kind, created_at);
"""

_TURN_FIELDS = [
    "id", "session_id", "seq", "module", "role", "mode", "prompt_raw", "system_prompt",
    "user_prompt", "output_raw", "provider", "model", "elapsed_ms", "prompt_tokens",
    "completion_tokens", "simulated", "degraded", "degraded_reason", "structured",
    "metrics", "created_at",
]

_JSON_FIELDS = {"meta", "structured", "metrics", "data", "flags", "config", "detail"}


class Storage:
    """线程安全的 SQLite 封装（Web 服务器多线程访问）。"""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------ 内部
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [self._decode(dict(r)) for r in cur.fetchall()]

    def _row(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
        for field in _JSON_FIELDS:
            if field in row and isinstance(row[field], str) and row[field]:
                try:
                    row[field] = json.loads(row[field])
                except json.JSONDecodeError:
                    pass
        return row

    @staticmethod
    def _encode(data: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for key, value in data.items():
            if isinstance(value, (dict, list)) and key in _JSON_FIELDS:
                out[key] = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, bool):
                out[key] = int(value)
            else:
                out[key] = value
        return out

    def _insert(self, table: str, data: Dict[str, Any]) -> str:
        data = self._encode(data)
        data.setdefault("id", new_id())
        data.setdefault("created_at", now_iso())
        cols = ", ".join(data.keys())
        marks = ", ".join("?" for _ in data)
        self._exec(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(data.values()))
        return data["id"]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ sessions
    def create_session(self, title: str = "", subject: str = "", module: str = "",
                       model: str = "", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sid = self._insert("sessions", {
            "title": title or "新会话", "subject": subject, "module": module,
            "model": model, "updated_at": now_iso(), "meta": meta or {},
        })
        return self.get_session(sid)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self._row("SELECT * FROM sessions WHERE id = ?", [session_id])
        if session:
            session["turns"] = self.list_turns(session_id)
            session["artifacts"] = self.list_artifacts(session_id)
        return session

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._rows(
            "SELECT s.*, (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id) AS turn_count "
            "FROM sessions s ORDER BY s.updated_at DESC LIMIT ?", [limit])

    def touch_session(self, session_id: str) -> None:
        self._exec("UPDATE sessions SET updated_at = ? WHERE id = ?", [now_iso(), session_id])

    # ------------------------------------------------------------ turns
    def add_turn(self, session_id: str, role: str, module: str = "", mode: str = "",
                 prompt_raw: str = "", system_prompt: str = "", user_prompt: str = "",
                 output_raw: str = "", result: Optional[Dict[str, Any]] = None,
                 metrics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = result or {}
        seq_row = self._row("SELECT COALESCE(MAX(seq), 0) + 1 AS nxt FROM turns WHERE session_id = ?",
                            [session_id])
        seq = int(seq_row["nxt"]) if seq_row else 1
        payload = {
            "session_id": session_id, "seq": seq, "module": module, "role": role, "mode": mode,
            "prompt_raw": prompt_raw, "system_prompt": system_prompt, "user_prompt": user_prompt,
            "output_raw": output_raw,
            "provider": result.get("provider", ""), "model": result.get("model", ""),
            "elapsed_ms": result.get("elapsed_ms", 0),
            "prompt_tokens": result.get("prompt_tokens", 0),
            "completion_tokens": result.get("completion_tokens", 0),
            "simulated": bool(result.get("simulated")),
            "degraded": bool(result.get("degraded")),
            "degraded_reason": result.get("degraded_reason", ""),
            "structured": result.get("structured") or {},
            "metrics": metrics or {},
        }
        turn_id = self._insert("turns", payload)
        self.touch_session(session_id)
        return self._row("SELECT * FROM turns WHERE id = ?", [turn_id])  # type: ignore[return-value]

    def list_turns(self, session_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM turns WHERE session_id = ? ORDER BY seq ASC"
        return self._rows(sql + (" LIMIT ?" if limit is not None else ""),
                          [session_id, limit] if limit is not None else [session_id])

    def recent_turns(self, session_id: str, limit: int = 8) -> List[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM turns WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
                          [session_id, limit])
        return list(reversed(rows))

    # ------------------------------------------------------------ artifacts
    def add_artifact(self, session_id: str, module: str, title: str, content: str,
                     data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        aid = self._insert("artifacts", {
            "session_id": session_id, "module": module, "title": title,
            "content": content, "data": data or {},
        })
        self.touch_session(session_id)
        return self._row("SELECT * FROM artifacts WHERE id = ?", [aid])  # type: ignore[return-value]

    def list_artifacts(self, session_id: str) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM artifacts WHERE session_id = ? ORDER BY created_at ASC",
                          [session_id])

    # ------------------------------------------------------------ risk
    def add_risk_interaction(self, case_id: str, case_type: str, module: str, title: str,
                             question: str, mode: str, answer: str, mode_detected: str = "",
                             flags: Optional[List[str]] = None, provider: str = "",
                             model: str = "") -> Dict[str, Any]:
        rid = self._insert("risk_interactions", {
            "case_id": case_id, "case_type": case_type, "module": module, "title": title,
            "question": question, "mode": mode, "answer": answer,
            "mode_detected": mode_detected, "flags": flags or [],
            "provider": provider, "model": model,
        })
        return self._row("SELECT * FROM risk_interactions WHERE id = ?", [rid])  # type: ignore[return-value]

    def list_risk_interactions(self, case_id: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        if case_id:
            return self._rows("SELECT * FROM risk_interactions WHERE case_id = ? ORDER BY created_at ASC",
                              [case_id])
        return self._rows("SELECT * FROM risk_interactions ORDER BY created_at DESC LIMIT ?", [limit])

    def latest_risk_by_case(self) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        """按 case_id 汇总最近一次各模式的输出。"""
        out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for row in self.list_risk_interactions(limit=2000):
            out.setdefault(row["case_id"], {}).setdefault(row["mode"], []).append(row)
        return out

    # ------------------------------------------------------------ evals
    def add_eval_run(self, kind: str, title: str, config: Dict[str, Any], metrics: Dict[str, Any],
                     detail: Dict[str, Any], file_path: str = "") -> Dict[str, Any]:
        eid = self._insert("eval_runs", {
            "kind": kind, "title": title, "config": config, "metrics": metrics,
            "detail": detail, "file_path": file_path,
        })
        return self._row("SELECT * FROM eval_runs WHERE id = ?", [eid])  # type: ignore[return-value]

    def list_eval_runs(self, kind: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if kind:
            return self._rows("SELECT * FROM eval_runs WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
                              [kind, limit])
        return self._rows("SELECT * FROM eval_runs ORDER BY created_at DESC LIMIT ?", [limit])

    # ------------------------------------------------------------ 统计
    def stats(self) -> Dict[str, Any]:
        def scalar(sql: str) -> int:
            row = self._row(sql)
            return int(list(row.values())[0]) if row else 0
        return {
            "sessions": scalar("SELECT COUNT(*) FROM sessions"),
            "turns": scalar("SELECT COUNT(*) FROM turns"),
            "artifacts": scalar("SELECT COUNT(*) FROM artifacts"),
            "risk_interactions": scalar("SELECT COUNT(*) FROM risk_interactions"),
            "eval_runs": scalar("SELECT COUNT(*) FROM eval_runs"),
            "db_path": str(self.db_path),
        }
