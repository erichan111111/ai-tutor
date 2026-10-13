"""Web 界面（零依赖，基于标准库 http.server）。

启动：
    python -m ai_tutor web              # 默认 http://127.0.0.1:8848
    python -m ai_tutor web --port 9000 --open

提供：
    · 问答工作台（六大模块、模型与任务路由选择、图片上传/粘贴/拖拽、质检结果可视化）
    · 提示词库浏览（含五要素方法论与每模块的精细提示原文）
    · 风险实证面板（用例列表、一键执行、量化指标与关键发现）
    · 会话与产出物回看

REST API（全部返回 JSON，可被其它前端或脚本复用）：
    GET  /api/health
    GET  /api/models
    GET  /api/routes
    GET  /api/modules
    GET  /api/prompts[?module=M1]
    GET  /api/detect?q=...
    POST /api/ask          # 支持 images: ["路径"] 或 ["data:image/png;base64,..."]
    POST /api/risk
    POST /api/session
    GET  /api/session/{id}
    GET  /api/sessions
    GET  /api/evals
    GET  /api/risk/records
    GET  /api/report
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import APP_NAME, TEAM, __version__
from .service import AgentService, get_service

STATIC_DIR = Path(__file__).resolve().parent / "web"

SAFE_FILES = {"index.html": "text/html; charset=utf-8"}


def _json_default(value: Any) -> Any:
    return str(value)


class Handler(BaseHTTPRequestHandler):
    server_version = "AITutor/" + __version__
    service: AgentService  # 由 create_server 注入

    # ------------------------------------------------------------ 基础
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if os.environ.get("AI_TUTOR_WEB_VERBOSE"):
            sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_text("404 Not Found", 404)
            return
        try:
            path.resolve().relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send_text("403 Forbidden", 403)
            return
        data = path.read_bytes()
        ctype = SAFE_FILES.get(path.name) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 48 * 1024 * 1024:
            raise ValueError("请求过大，请减少图片数量或大小")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("请求内容应为 JSON 对象")
            return data
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("请求不是有效的 JSON") from None

    # ------------------------------------------------------------ 路由
    def _valid_host(self) -> bool:
        # Loopback deployments must not expose local records through DNS rebinding.
        if self.server.server_address[0] not in ("127.0.0.1", "::1"):
            return True
        try:
            host = urlparse("http://" + self.headers.get("Host", ""))
            return host.hostname in ("127.0.0.1", "localhost", "::1") and (host.port or 80) == self.server.server_port
        except ValueError:
            return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._valid_host():
            self._send_json({"error": "请通过本机地址访问应用"}, 403)
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/api/study":
                self._send_json(self.service.study.snapshot(query.get("today", date.today().isoformat())))
                return
            if path == "/api/study/export":
                self._send_json(self.service.study.export())
                return
            if path == "/":
                self._send_file(STATIC_DIR / "index.html")
                return
            if path.startswith("/static/"):
                self._send_file(STATIC_DIR / path[len("/static/"):])
                return
            if path == "/api/health":
                self._send_json(self.service.health())
                return
            if path == "/api/models":
                self._send_json({"models": self.service.models()})
                return
            if path == "/api/routes":
                self._send_json({"routes": self.service.routes()})
                return
            if path == "/api/modules":
                self._send_json({"modules": self.service.modules(),
                                 "methodology": self.service.prompt_library()["methodology"]})
                return
            if path == "/api/prompts":
                self._send_json(self.service.prompt_library(module=query.get("module") or None))
                return
            if path == "/api/detect":
                self._send_json(self.service.detect(query.get("q", "")))
                return
            if path == "/api/sessions":
                self._send_json({"sessions": self.service.list_sessions(limit=int(query.get("limit", 50)))})
                return
            if path.startswith("/api/session/"):
                session = self.service.get_session(path.split("/")[-1])
                if session is None:
                    self._send_json({"error": "会话不存在"}, 404)
                else:
                    self._send_json(session)
                return
            if path == "/api/evals":
                self._send_json({"runs": self.service.eval_runs(kind=query.get("kind") or None,
                                                                limit=int(query.get("limit", 20)))})
                return
            if path == "/api/risk/records":
                self._send_json({"records": self.service.risk_records(case_id=query.get("case_id") or None,
                                                                      limit=int(query.get("limit", 100)))})
                return
            if path == "/api/report":
                self._send_text(self.service.report(limit=int(query.get("limit", 10))),
                                content_type="text/markdown; charset=utf-8")
                return
            self._send_json({"error": f"未知路径 {path}"}, 404)
        except LookupError as exc:
            self._send_json({"error": str(exc)}, 404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._valid_host():
            self._send_json({"error": "请通过本机地址访问应用"}, 403)
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self._send_json({"error": "请从本应用页面提交请求"}, 403)
                return
            payload = self._read_body()
            today = payload.get("today", date.today().isoformat())
            study = self.service.study
            if path == "/api/study/course":
                self._send_json(study.save_course(payload, payload.get("id")))
                return
            if path == "/api/study/archive":
                self._send_json(study.archive_course(payload.get("course_id"), payload.get("archived")))
                return
            if path == "/api/study/plan":
                self._send_json(study.plan(payload.get("course_id"), today))
                return
            if path == "/api/study/task":
                self._send_json(study.add_task(payload))
                return
            if path == "/api/study/task/update":
                self._send_json(study.update_task(payload.get("id"), {k: v for k, v in payload.items() if k != "id"}))
                return
            if path == "/api/study/note":
                self._send_json(study.add_note(payload, today))
                return
            if path == "/api/study/note/update":
                self._send_json(study.edit_note(payload.get("id"), payload))
                return
            if path == "/api/study/note/delete":
                self._send_json(study.delete_note(payload.get("id")))
                return
            if path == "/api/study/review":
                self._send_json(study.review(payload.get("note_id"), payload.get("rating"), today))
                return
            if path == "/api/ask":
                question = str(payload.get("question", "")).strip()
                images = payload.get("images") or payload.get("image") or None
                if isinstance(images, str):
                    images = [images] if images else None
                if not question and not images:
                    self._send_json({"error": "question 与 images 不能同时为空"}, 400)
                    return
                result = self.service.ask(
                    question,
                    module=payload.get("module", "auto"),
                    model=payload.get("model") or None,
                    session_id=payload.get("session_id") or None,
                    use_router=bool(payload.get("use_router")),
                    temperature=payload.get("temperature"),
                    images=images,
                    subject=payload.get("subject", ""),
                    course_id=payload.get("course_id") or None,
                    days=int(payload.get("days", 5) or 5),
                    student_answer=payload.get("student_answer", ""),
                    answers=payload.get("answers", ""),
                    stage=payload.get("stage", "auto"),
                    level=payload.get("level", "零基础"),
                    student_reply=payload.get("student_reply", ""),
                    scenario=payload.get("scenario", ""),
                    round_index=int(payload.get("round_index", 0) or 0),
                )
                self._send_json(result)
                return
            if path == "/api/ablate":
                # 评测层接口：提示词消融对照（前端不暴露入口）
                question = str(payload.get("question", "")).strip()
                if not question:
                    self._send_json({"error": "question 不能为空"}, 400)
                    return
                self._send_json(self.service.compare(
                    question, module=payload.get("module", "auto"),
                    model=payload.get("model") or None,
                    use_router=bool(payload.get("use_router"))))
                return
            if path == "/api/risk":
                result = self.service.run_risk(
                    kind=payload.get("kind", "pair"),
                    model=payload.get("model") or None,
                    rounds=int(payload.get("rounds", 3) or 3),
                    models=payload.get("models") or None,
                )
                self._send_json(result)
                return
            if path == "/api/session":
                self._send_json(self.service.create_session(
                    title=payload.get("title", ""), subject=payload.get("subject", ""),
                    module=payload.get("module", "")))
                return
            self._send_json({"error": f"未知路径 {path}"}, 404)
        except LookupError as exc:
            self._send_json({"error": str(exc)}, 404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)


def create_server(host: str = "127.0.0.1", port: int = 8848,
                  service: Optional[AgentService] = None) -> ThreadingHTTPServer:
    svc = service or get_service()
    handler = type("BoundHandler", (Handler,), {"service": svc})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8848, open_browser: bool = False,
          service: Optional[AgentService] = None) -> int:
    httpd = create_server(host, port, service)
    actual_host, actual_port = httpd.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/"
    print("=" * 74)
    print(f"  {APP_NAME}　v{__version__}")
    print(f"  {TEAM}")
    print("=" * 74)
    print(f"  Web 界面：{url}")
    print(f"  数据目录：{(service or get_service()).data_dir}")
    if not (service or get_service()).client.usable_names():
        print("  提示：当前未配置 API Key，答疑为离线预设演示；课程与复习记录可正常使用。")
    print("  按 Ctrl+C 停止服务。")
    print("=" * 74)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务……")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
