# /// script
# requires-python = ">=3.10"
# ///
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import learn


class Memory(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        os.environ["MYSC_MEMORY"] = os.path.join(self.folder.name, "m.json")
        self.data = learn.load()

    def tearDown(self):
        os.environ.pop("MYSC_MEMORY")
        self.folder.cleanup()

    def test_none_once_settled(self):
        self.assertEqual(learn.recall(self.data, "expense", "memo", "가게")["status"], "none")
        learn.record(self.data, "expense", "memo", "가 게", "야근 식대")
        self.assertEqual(learn.recall(self.data, "expense", "memo", "가게")["status"], "once")
        learn.record(self.data, "expense", "memo", "가게", "야근 식대", "accepted")
        self.assertEqual(learn.recall(self.data, "expense", "memo", "가게"), {"status": "settled", "value": "야근 식대", "options": [{"value": "야근 식대", "times": 2, "last": learn.today()}]})

    def test_two_live_values_ask(self):
        learn.record(self.data, "expense", "memo", "택시", "야근 택시비")
        learn.record(self.data, "expense", "memo", "택시", "미팅 택시비")
        self.assertEqual(learn.recall(self.data, "expense", "memo", "택시")["status"], "mixed")

    def test_correction_retires_old_value(self):
        learn.record(self.data, "expense", "project", "가게", "A")
        learn.record(self.data, "expense", "project", "가게", "B", "correction", replaced="A")
        self.assertEqual(learn.recall(self.data, "expense", "project", "가게")["value"], "B")
        self.assertEqual(learn.recall(self.data, "expense", "project", "가게")["status"], "once")

    def test_always_settles_at_once(self):
        learn.record(self.data, "expense", "memo", "가게", "A")
        learn.record(self.data, "expense", "memo", "가게", "B", "always")
        self.assertEqual(learn.recall(self.data, "expense", "memo", "가게")["status"], "settled")

    def test_scope_falls_back_to_global_only(self):
        learn.record(self.data, "evidence", "폴더규칙", "-", "No.만", "always")
        learn.record(self.data, "evidence", "폴더규칙", "-", "날짜_거래처", "always", scope="시트A")
        self.assertEqual(learn.recall(self.data, "evidence", "폴더규칙", "-", "시트A")["value"], "날짜_거래처")
        self.assertEqual(learn.recall(self.data, "evidence", "폴더규칙", "-", "시트B")["value"], "No.만")

    def test_brief_collects_notes_facts_and_runs(self):
        learn.note(self.data, "*", "preference", "짧게 말한다")
        learn.note(self.data, "*", "preference", "짧게  말한다")
        learn.record(self.data, "tongjang", "작성자", "-", "닉네임", "always", scope="시트")
        learn.log_run(self.data, "tongjang", "시트", 3, 2)
        learn.save(self.data)
        got = learn.brief(learn.load(), "tongjang", "시트")
        self.assertEqual(got["notes"], [{"kind": "preference", "text": "짧게 말한다", "hits": 2}])
        self.assertEqual(got["facts"], {"작성자": "닉네임"})
        self.assertEqual(got["recentRuns"][0]["asked"], 2)
        if os.name != "nt":
            self.assertEqual(os.stat(learn.memory_path()).st_mode & 0o077, 0)

    def test_review_finds_corrections_splits_and_stalled_questions(self):
        learn.record(self.data, "expense", "project", "가게", "A")
        learn.record(self.data, "expense", "project", "가게", "B", "correction", replaced="A")
        learn.record(self.data, "expense", "memo", "택시", "야근")
        learn.record(self.data, "expense", "memo", "택시", "미팅")
        learn.log_run(self.data, "expense", None, 5, 3)
        learn.log_run(self.data, "expense", None, 5, 4)
        got = learn.review(self.data, "expense")
        self.assertEqual(got["corrected"], [{"field": "project", "key": "가게", "corrections": 1}])
        self.assertEqual(got["split"][0]["key"], "택시")
        self.assertTrue(got["askedNotFalling"])

    def test_adopted_proposal_reaches_next_run_with_its_evidence(self):
        item = learn.propose(self.data, "expense", "domain", "택시는 시각으로 먼저 나눈다", "3회 정정")
        self.assertEqual(learn.brief(self.data, "expense")["notes"], [])
        learn.decide(self.data, item["id"], True)
        self.assertEqual(learn.brief(self.data, "expense")["notes"][0]["because"], ["3회 정정"])
        self.assertEqual(learn.review(self.data, "expense")["open"], [])

    def test_rejected_proposal_is_not_raised_again(self):
        item = learn.propose(self.data, "expense", "general", "표를 줄인다", "근거")
        learn.decide(self.data, item["id"], False, "표가 필요함")
        self.assertTrue(learn.propose(self.data, "expense", "general", "표를  줄인다", "새 근거")["blocked"])
        self.assertEqual(learn.review(self.data, "expense")["rejected"], [{"text": "표를 줄인다", "reason": "표가 필요함"}])
        self.assertEqual(len(self.data["proposals"]), 1)

    def test_repeat_proposal_gathers_evidence(self):
        learn.propose(self.data, "expense", "general", "묶어서 묻는다", "1회차")
        got = learn.propose(self.data, "expense", "general", "묶어서 묻는다", "2회차")
        self.assertEqual(got["because"], ["1회차", "2회차"])

    def test_forget(self):
        learn.record(self.data, "expense", "memo", "가게", "A")
        self.assertEqual(learn.forget(self.data, "expense", "memo", "가게"), 1)
        self.assertEqual(learn.recall(self.data, "expense", "memo", "가게")["status"], "none")


class ConsoleOutput(unittest.TestCase):
    """Windows 콘솔·파이프 인코딩을 흉내 내고 실제 명령 출력을 UTF-8로 읽는다."""

    encodings = ("cp949", "cp1252")

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def run_script(self, encoding, script, *args):
        env = {**os.environ, "PYTHONIOENCODING": encoding, "PYTHONUTF8": "0", "MYSC_MEMORY": str(self.path / "memory.json")}
        done = subprocess.run([sys.executable, "-I", str(Path(__file__).parent / script), *args], capture_output=True, env=env, timeout=60)
        return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")

    def test_learn_prints_korean(self):
        for encoding in self.encodings:
            with self.subTest(encoding):
                code, _, error = self.run_script(encoding, "learn.py", "record", "--flow", "expense", "--field", "memo", "--key", "가게", "--value", "야근 식대")
                self.assertEqual((code, error), (0, ""))
                code, out, _ = self.run_script(encoding, "learn.py", "recall", "--flow", "expense", "--field", "memo", "--key", "가게")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["value"], "야근 식대")

    def test_character_outside_the_console_encoding_survives(self):
        for encoding in self.encodings:
            with self.subTest(encoding):
                self.run_script(encoding, "learn.py", "note", "--flow", "*", "--kind", "preference", "--text", "표는 짧게 🙂")
                code, out, _ = self.run_script(encoding, "learn.py", "brief", "--flow", "*")
                self.assertEqual(code, 0)
                self.assertIn("🙂", out)

    def test_tongjang_prints_korean_on_success_and_failure(self):
        history = self.path / "history.json"
        history.write_text(json.dumps({"startRow": 8, "values": [["통장번호", "다른칸"]]}, ensure_ascii=False), encoding="utf-8")
        for encoding in self.encodings:
            with self.subTest(encoding):
                code, out, _ = self.run_script(encoding, "tongjang.py", "columns", str(history))
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["unmatchedHeaders"][0]["header"], "다른칸")
                code, _, error = self.run_script(encoding, "tongjang.py", "status", str(history))
                self.assertEqual(code, 2)
                self.assertIn("양식이 다릅니다", error)

    def test_files_saved_with_a_windows_bom_are_read(self):
        items = self.path / "items.json"
        items.write_text(json.dumps([{"field": "memo", "key": "가게"}], ensure_ascii=False), encoding="utf-8-sig")
        code, out, error = self.run_script("cp949", "learn.py", "recall-many", "--flow", "expense", str(items))
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(out)[0]["status"], "none")
        history = self.path / "history.json"
        history.write_text(json.dumps({"startRow": 8, "values": [["통장번호"]]}, ensure_ascii=False), encoding="utf-8-sig")
        code, out, error = self.run_script("cp949", "tongjang.py", "columns", str(history))
        self.assertEqual((code, error), (0, ""))
        self.assertFalse(json.loads(out)["same"])


if __name__ == "__main__":
    unittest.main()
