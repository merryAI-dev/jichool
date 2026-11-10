# /// script
# requires-python = ">=3.10"
# ///
import os
import tempfile
import unittest

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

    def test_forget(self):
        learn.record(self.data, "expense", "memo", "가게", "A")
        self.assertEqual(learn.forget(self.data, "expense", "memo", "가게"), 1)
        self.assertEqual(learn.recall(self.data, "expense", "memo", "가게")["status"], "none")


if __name__ == "__main__":
    unittest.main()
