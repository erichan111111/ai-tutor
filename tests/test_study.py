"""Offline product regression tests: data survives restarts; progress is learner-owned."""
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ai_tutor.config import DEFAULT_CONFIG
from ai_tutor.features.base import BaseFeature, FeatureContext
from ai_tutor.llm.mock import MockProvider
from ai_tutor.llm.registry import LLMClient
from ai_tutor.service import AgentService
from ai_tutor.storage import Storage
from ai_tutor.webapp import create_server


class StudyFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.storage = Storage(self.path)
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["app"]["data_dir"] = self.tmp.name
        self.svc = AgentService(config, self.storage, LLMClient({"mock": MockProvider("mock-offline")}, "mock"))
        self.study = self.svc.study
        self.today = "2026-10-12"

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def course(self, **changes):
        payload = dict(name="计算机网络", goal="闭卷解释关键概念", exam_date="2026-10-22",
                       daily_minutes=60, topics=["TCP", "拥塞控制", "TCP"])
        payload.update(changes)
        return self.study.save_course(payload)

    def note(self, **changes):
        payload = dict(title="TCP", question="三次握手解决什么问题？", answer="双方确认收发能力", reflection="注意方向")
        payload.update(changes)
        return self.study.add_note(payload, self.today)


class StudyTests(StudyFixture, unittest.TestCase):

    def test_course_validation_and_persistence(self):
        course = self.course()
        self.assertEqual(course["topics"], ["TCP", "拥塞控制"])
        for changes in ({"name":" "}, {"exam_date":"2026-02-30"}, {"daily_minutes":True},
                        {"daily_minutes":60.5}, {"daily_minutes":10}, {"topics":[]}, {"topics":[None]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.course(**changes)
        self.storage.close()
        self.storage = Storage(self.path)
        from ai_tutor.study import StudyWorkspace
        reopened = StudyWorkspace(self.storage)
        self.assertEqual(reopened.course(course["id"])["goal"], "闭卷解释关键概念")

    def test_plan_budget_capacity_and_repeat_clicks(self):
        course = self.course(daily_minutes=30, exam_date=self.today)
        custom = self.study.add_task(dict(course_id=course["id"], title="教材习题", due_date=self.today, minutes=25))
        planned = self.study.plan(course["id"], self.today)
        self.assertTrue(planned["warning"])
        daily = {}
        for task in planned["tasks"]:
            daily[task["due_date"]] = daily.get(task["due_date"], 0) + task["minutes"]
        self.assertTrue(all(m <= 30 for m in daily.values()))
        before = [t["id"] for t in planned["tasks"]]
        self.study.update_task(custom["id"], {"done":True})
        self.assertEqual(before, [t["id"] for t in self.study.plan(course["id"], self.today)["tasks"]])
        with self.assertRaises(ValueError):
            self.study.plan(course["id"], "2026-10-13")

    def test_parallel_plan_requests_do_not_duplicate(self):
        course = self.course()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.study.plan(course["id"], self.today), range(4)))
        self.assertEqual(len(results[-1]["tasks"]), 6)
        self.assertEqual(len(self.study.tasks(course["id"])), 6)

    def test_task_complete_undo_and_reschedule(self):
        c = self.course()
        task = self.study.add_task(dict(course_id=c["id"],title="教材练习",due_date=self.today,minutes=25))
        completed = self.study.update_task(task["id"], {"done":True})
        self.assertTrue(completed["completed_at"])
        self.assertEqual(self.study.snapshot(self.today)["summary"]["completed_tasks"], 1)
        self.study.update_task(task["id"], {"done":False, "due_date":"2026-10-13"})
        self.assertEqual(self.study.snapshot(self.today)["due_tasks"], [])
        edited = self.study.update_task(task["id"], {"title":"选做重点题", "minutes":15})
        self.assertEqual((edited["title"],edited["minutes"]), ("选做重点题",15))
        with self.assertRaises(ValueError):
            self.study.update_task(task["id"], {"done":"false"})

    def test_archive_pauses_reminders_and_restore_preserves_records(self):
        c = self.course()
        self.study.plan(c["id"], self.today)
        self.note(course_id=c["id"])
        self.study.archive_course(c["id"], True)
        snapshot = self.study.snapshot(self.today)
        self.assertEqual(snapshot["due_tasks"], [])
        self.assertEqual(snapshot["due_notes"], [])
        self.assertEqual(len(snapshot["notes"]), 1)
        self.study.archive_course(c["id"], False)
        self.assertEqual(len(self.study.snapshot(self.today)["due_notes"]), 1)

    def test_saved_ai_answer_has_server_verified_provenance(self):
        c = self.course()
        answer = self.svc.ask("帮我解释 TCP", module="M4", course_id=c["id"])
        self.assertIn("课程：计算机网络", answer["prompt"])
        self.assertEqual(answer["session"]["meta"]["course_id"], c["id"])
        payload = dict(title="握手", course_id=c["id"], source_turn_id=answer["turn_id"], answer="伪造", simulated=False)
        note = self.study.add_note(payload, self.today)
        self.assertEqual(note["answer"], answer["answer"])
        self.assertTrue(note["simulated"])
        self.assertEqual(note["id"], self.study.add_note(payload, self.today)["id"])
        self.assertEqual(self.study.notes()[0]["source_session_id"], answer["session_id"])
        other = self.course(name="高等数学")
        with self.assertRaises(ValueError):
            self.study.add_note({**payload, "course_id":other["id"]}, self.today)
        with self.assertRaises(LookupError):
            self.study.add_note(dict(title="无来源",source_turn_id="missing"), self.today)

    def test_review_intervals_and_daily_duplicate_guard(self):
        n = self.note()
        first = self.study.review(n["id"], "good", self.today)
        self.assertEqual(first["due_date"], "2026-10-15")
        with self.assertRaises(ValueError):
            self.study.review(n["id"], "again", self.today)
        second = self.study.review(n["id"], "good", "2026-10-15")
        self.assertEqual(second["interval_days"], 6)
        third = self.study.review(n["id"], "hard", "2026-10-21")
        self.assertEqual(third["interval_days"], 2)
        last = self.study.review(n["id"], "again", "2026-10-23")
        self.assertEqual(last["due_date"], "2026-10-24")
        self.assertEqual(last["review_count"], 4)
        self.assertEqual(self.study.snapshot("2026-10-23")["summary"]["reviews_today"], 1)

    def test_note_edit_delete_and_export(self):
        self.svc.config["llm"]["models"]["deepseek"]["api_key"] = "PRIVATE_TEST_KEY_DO_NOT_EXPORT"
        self.course()
        n = self.note()
        self.study.review(n["id"], "good", self.today)
        edited = self.study.edit_note(n["id"], dict(title="新标题",reflection="新错因"))
        self.assertEqual(edited["answer"], n["answer"])
        exported = self.study.export()
        self.assertEqual(exported["format"], "ai-tutor-study-export")
        self.assertEqual(len(exported["reviews"]), 1)
        self.assertNotIn("PRIVATE_TEST_KEY_DO_NOT_EXPORT", json.dumps(exported))
        self.study.delete_note(n["id"])
        self.assertEqual(self.study.export()["reviews"], [])

    def test_long_session_export_is_not_truncated(self):
        s = self.storage.create_session(title="长会话")
        for i in range(205):
            self.storage.add_turn(s["id"], role="assistant", user_prompt=str(i), output_raw="a")
        self.assertEqual(len(self.study.export()["sessions"][0]["turns"]), 205)

    def test_resume_keeps_question_answer_pairs_and_course(self):
        c = self.course()
        answer = self.svc.ask("解释递归",module="M4",course_id=c["id"])
        ctx = FeatureContext(storage=self.storage,client=self.svc.client,config=self.svc.config,
                             router=self.svc.router,session_id=answer["session_id"])
        history = BaseFeature().history_messages(ctx)
        self.assertEqual([(m.role,m.content) for m in history], [("user","解释递归"),("assistant",answer["answer"])])
        continued = self.svc.ask("再举个例子",module="M4",session_id=answer["session_id"])
        self.assertIn("课程：计算机网络", continued["prompt"])
        self.assertEqual(len(continued["session"]["turns"]),2)
        with self.assertRaises(LookupError):
            self.svc.ask("测试",session_id="missing")


class HTTPTests(StudyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = create_server(port=0,service=self.svc)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def request(self, path, payload=None, raw=None, headers=None):
        req = Request(self.base+path, data=raw if raw is not None else json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type":"application/json", **(headers or {})})
        try:
            response = urlopen(req,timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_http_learning_journey(self):
        status,c = self.request("/api/study/course",dict(name="高数",goal="会做极限",exam_date="2026-10-22",daily_minutes=60,topics=["极限"]))
        self.assertEqual(status,200)
        status,plan = self.request("/api/study/plan",dict(course_id=c["id"],today=self.today))
        self.assertEqual(len(plan["tasks"]),3)
        status,answer = self.request("/api/ask",dict(question="解释极限",module="M4",course_id=c["id"]))
        self.assertEqual(status,200)
        status,note = self.request("/api/study/note",dict(title="极限",course_id=c["id"],source_turn_id=answer["turn_id"],today=self.today))
        self.assertEqual(status,200)
        status,snapshot = self.request("/api/study?today="+self.today)
        self.assertEqual(snapshot["summary"]["due_reviews"],1)
        status,_ = self.request("/api/study/review",dict(note_id=note["id"],rating="good",today=self.today))
        self.assertEqual(status,200)
        status,export = self.request("/api/study/export")
        self.assertEqual(export["notes"][0]["review_count"],1)

    def test_bad_requests_and_cross_origin(self):
        for raw in (b"not-json", b"[]"):
            self.assertEqual(self.request("/api/study/course", raw=raw)[0],400)
        self.assertEqual(self.request("/api/study?today=wrong")[0],400)
        self.assertEqual(self.request("/api/study/course",dict(name=""))[0],400)
        self.assertEqual(self.request("/api/study/plan",dict(course_id=[],today=self.today))[0],400)
        self.assertEqual(self.request("/api/study/plan",dict(course_id="missing",today=self.today))[0],404)
        self.assertEqual(self.request("/api/study/course",{},headers={"Origin":"https://unrelated.example"})[0],403)
        self.assertEqual(self.request("/api/study",headers={"Host":"unrelated.example"})[0],403)
        self.assertEqual(self.request("/api/study/course",{},headers={"Content-Length":str(49*1024*1024)})[0],400)


if __name__ == "__main__":
    unittest.main()
