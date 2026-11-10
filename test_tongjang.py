# /// script
# requires-python = ">=3.10"
# dependencies = ["openpyxl", "xlrd"]
# ///
import unittest

import tongjang as t


def hist(no, payee, detail="2-1.일반수용비", line="직접사업비(공급가액)", out=1000, bal=0, memo="적요", n=None, m=None, kind="인터넷출금이체", gubun=None):
    return t.row_dict([no, f"2026.03.{no:02d}\n 10:00:00", kind, payee, "", out, 0, bal, "서울", gubun, "2.직접비", detail, m, n, line, memo], t.FIRST_ROW + no - 1)


class Suggest(unittest.TestCase):
    def test_repeated_payee_is_high(self):
        rows = [hist(1, "가게"), hist(2, "가게")]
        got = t.suggest(t.row_dict([None, "2026.03.03", "인터넷출금이체", "가게", "", 500, 0, 0], 0), rows)
        self.assertEqual(got["L"]["value"], "2-1.일반수용비")
        self.assertEqual(got["L"]["confidence"], "high")
        self.assertEqual(got["K"]["value"], "2.직접비")

    def test_unknown_payee_only_low(self):
        got = t.suggest(t.row_dict([None, "2026.03.03", "인터넷출금이체", "처음", "", 500, 0, 0], 0), [hist(1, "가게")])
        self.assertEqual(got["L"]["confidence"], "low")

    def test_cancelled_rows_do_not_vote(self):
        got = t.suggest(t.row_dict([None, "d", "x", "가게", "", 1, 0, 0], 0), [hist(1, "가게", gubun="취소")])
        self.assertEqual(got["L"]["confidence"], "none")

    def test_vat_is_candidate_only_with_history(self):
        target = t.row_dict([None, "d", "x", "가게", "", 1100, 0, 0], 0)
        self.assertIsNone(t.suggest(target, [])["vat"])
        got = t.suggest(target, [hist(1, "가게", n=100, m=1000)])["vat"]
        self.assertEqual((got["M"], got["N"]), (1000, 100))


class Plan(unittest.TestCase):
    def bank(self, dt, out, bal):
        return t.row_dict([None, dt, "인터넷출금이체", "가게", "", out, 0, bal], 0)

    def test_drops_rows_already_in_sheet(self):
        history = [hist(1, "가게", bal=9000)]
        same = self.bank(history[0]["dt"], 1000, 9000)
        new = self.bank("2026.03.05\n 11:00:00", 1000, 8000)
        result = t.plan(history, [same, new])
        self.assertEqual(result["alreadyInSheet"], 1)
        self.assertEqual([r["no"] for r in result["rows"]], [2])
        self.assertEqual(result["rows"][0]["row"], t.FIRST_ROW + 1)
        self.assertEqual(result["balanceIssues"], [])

    def test_balance_gap_is_reported_and_blocks(self):
        history = [hist(1, "가게", bal=9000)]
        result = t.plan(history, [self.bank("2026.03.05", 1000, 5000)])
        self.assertEqual(len(result["balanceIssues"]), 1)
        with self.assertRaises(t.ToolError):
            t.finalize(result, {}, {"row": 10, "values": []}, "작성자")

    def test_json_history_cannot_start_above_data(self):
        import json, tempfile, pathlib

        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "h.json"
            path.write_text(json.dumps({"startRow": 1, "values": []}), encoding="utf-8")
            with self.assertRaises(t.ToolError):
                t.load_history(path)


class Formula(unittest.TestCase):
    def test_shifts_relative_only(self):
        source = "=VLOOKUP($B40, '통장내역(A1)'!$A$1:$Z$299, 3)&K39&\"B7\""
        self.assertEqual(t.shift_formula(source, 2), "=VLOOKUP($B42, '통장내역(A1)'!$A$1:$Z$299, 3)&K41&\"B7\"")


class History(unittest.TestCase):
    def write(self, folder, header, rows=()):
        import json, pathlib

        path = pathlib.Path(folder) / "h.json"
        path.write_text(json.dumps({"startRow": t.HEADER_ROW, "values": [header, *rows]}), encoding="utf-8")
        return path

    def test_reads_rows_below_header(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            rows = t.load_history(self.write(folder, t.BANK_HEADERS, [[1, "2026.03.01", "x", "가게"]]))
        self.assertEqual(rows[0]["row"], t.FIRST_ROW)

    def test_different_layout_stops(self):
        import tempfile

        changed = list(t.BANK_HEADERS)
        changed[11] = "다른칸"
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(t.ToolError, "L열"):
            t.load_history(self.write(folder, changed))


class Layout(unittest.TestCase):
    moved = ["통장번호", "메모칸", *t.BANK_HEADERS[1:]]

    def letters(self):
        return [t.col_letter(i + 1) if i else "A" for i in range(16)]

    def write(self, folder, payload):
        import json, pathlib

        path = pathlib.Path(folder) / "h.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_report_suggests_moved_columns_and_keeps_extra(self):
        report = t.column_report(self.moved)
        self.assertFalse(report["same"])
        self.assertEqual(report["columns"][:3], ["A", "C", "D"])
        self.assertEqual(report["unmatchedHeaders"], [{"column": "B", "header": "메모칸"}])

    def test_report_marks_missing_for_the_user(self):
        cut = list(t.BANK_HEADERS)
        cut[11] = "다른칸"
        report = t.column_report(cut)
        self.assertEqual(report["differences"], [{"field": "세목", "expected": "L", "suggested": None}])

    def test_same_layout_has_nothing_to_ask(self):
        self.assertTrue(t.column_report(t.BANK_HEADERS)["same"])

    def test_without_confirmation_stops(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder, self.assertRaises(t.ToolError):
            t.load_history(self.write(folder, {"startRow": 5, "columns": self.letters(), "values": [self.moved]}))

    def test_confirmed_layout_reads_from_its_own_header_row(self):
        import tempfile

        line = [1, "메모", "2026.03.01", "x", "가게", "", 1000, 0, 500]
        with tempfile.TemporaryDirectory() as folder:
            rows = t.load_history(self.write(folder, {"startRow": 5, "columns": self.letters(), "confirmedHeader": self.moved, "values": [self.moved, line]}))
        self.assertEqual((rows[0]["row"], rows[0]["payee"], rows[0]["out"]), (6, "가게", 1000))

    def test_changed_header_asks_again(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(t.ToolError, "달라졌습니다"):
            t.load_history(self.write(folder, {"startRow": 5, "columns": self.letters(), "confirmedHeader": t.BANK_HEADERS, "values": [self.moved]}))

    def planned(self, layout):
        history = [hist(1, "가게", bal=9000)]
        result = t.plan(history, [t.row_dict([None, "2026.03.05", "체크카드", "가게", "", 1000, 0, 8000], 0)])
        return {**result, "layout": layout}

    def test_finalize_splits_around_skipped_column(self):
        template = {"row": 20, "values": ["", 17], "header": ["작성자", "No.", "", "", "", "지출구분"]}
        result = t.finalize(self.planned({"columns": self.letters(), "bankTab": "다른탭"}), {}, template, "작성자")
        self.assertEqual([(r["tab"], r["range"]) for r in result["ranges"][:2]], [("다른탭", "A10:A10"), ("다른탭", "C10:Q10")])
        self.assertEqual(result["overwrite"]["다른탭"], [0, 14])
        self.assertEqual((result["formatCopy"]["startColumn"], result["formatCopy"]["endColumn"]), (0, 17))

    def test_usage_layout_stops_until_answered(self):
        template = {"row": 20, "values": ["", "", 17], "header": ["작성자", "메모", "No.", "", "", "", "지출구분"]}
        with self.assertRaisesRegex(t.ToolError, "B열"):
            t.finalize(self.planned({}), {"card": "법인카드"}, template, "작성자")
        result = t.finalize(self.planned({}), {"card": "법인카드", "usageColumns": {"No.": "C", "지출구분": "G"}}, template, "작성자")
        self.assertEqual(result["ranges"][-1]["range"], "G5:G5")
        self.assertEqual(result["overwrite"][t.USAGE_TAB], [6])


class Learning(unittest.TestCase):
    def setUp(self):
        import os, tempfile

        self.folder = tempfile.TemporaryDirectory()
        os.environ["MYSC_MEMORY"] = os.path.join(self.folder.name, "memory.json")
        self.template = {"row": 20, "values": ["", 17], "header": ["작성자", "No.", "", "", "", "지출구분"]}

    def tearDown(self):
        import os

        os.environ.pop("MYSC_MEMORY")
        self.folder.cleanup()

    def planned(self, payee="새가게"):
        history = [hist(1, "가게", bal=9000)]
        return t.attach_learned(t.plan(history, [t.row_dict([None, "2026.03.05", "체크카드", payee, "", 1000, 0, 8000], 0)]), "시트")

    def test_answer_is_asked_once_then_filled(self):
        answer = {"rows": {"2": {"K": "2.직접비", "L": "2-6.업무추진비", "O": "직접사업비(공급가액)", "P": "회의비_식대"}}, "acceptVat": True}
        first = t.finalize(self.planned(), answer, self.template, "작성자")
        self.assertEqual(first["asked"], 4)
        self.assertEqual({i["source"] for i in first["learn"]}, {"answer"})
        t.reflect(first, "시트", {"작성자": "작성자", "카드": "법인카드(1)"})
        second = t.finalize(self.planned(), {}, self.template, "작성자")
        self.assertEqual(second["ranges"][0]["values"][0][10:12], ["2.직접비", "2-6.업무추진비"])
        self.assertEqual(second["ranges"][0]["values"][0][15], "회의비_식대")
        self.assertEqual(second["asked"], 0)
        self.assertEqual(t.remembered("시트", "카드"), "법인카드(1)")

    def test_correction_replaces_and_split_memory_asks_again(self):
        import learn

        memory = learn.load()
        learn.record(memory, "tongjang", "P", "새가게", "회의비_A", "answer", scope="시트")
        learn.record(memory, "tongjang", "P", "새가게", "회의비_B", "answer", scope="시트")
        learn.save(memory)
        self.assertEqual(self.planned()["rows"][0]["learned"]["P"]["status"], "mixed")
        self.assertEqual(t.finalize(self.planned(), {}, self.template, "작성자")["ranges"][0]["values"][0][15], "")
        memory = learn.load()
        learn.record(memory, "tongjang", "P", "새가게", "회의비_B", "correction", replaced="회의비_A", scope="시트")
        learn.save(memory)
        self.assertEqual(self.planned()["rows"][0]["learned"]["P"]["value"], "회의비_B")
        self.assertEqual(self.planned()["rows"][0]["learned"]["P"]["status"], "settled")

    def test_other_sheet_does_not_inherit(self):
        import learn

        memory = learn.load()
        learn.record(memory, "tongjang", "P", "새가게", "회의비_A", "answer", scope="다른시트")
        learn.save(memory)
        self.assertEqual(self.planned()["rows"][0]["learned"], {})


class BankTables(unittest.TestCase):
    def test_kb_table(self):
        matrix = [["계좌", "x"], ["거래일시", "적요", "의뢰인/수취인", "내통장표시내용", "출금금액", "입금금액", "잔액", "취급점", "구분"], ["2026-03-05 11:00:00", "인터넷출금이체", "가게", "표시", "1,000", "0", "9,000", "서울", ""], ["합계"]]
        rows = t.normalize_bank(matrix, "kb")["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dt"], "2026.03.05\n 11:00:00")
        self.assertEqual((rows[0]["out"], rows[0]["bal"]), (1000, 9000))

    def test_hana_joins_date_and_time(self):
        matrix = [["거래일자", "구분", "적요", "입금액", "출금액", "잔액", "거래시간", "거래점"], ["2026-01-31", "체크카드", "가맹점", "0", "500", "100", "13:05:09", "자금결제부"]]
        row = t.normalize_bank(matrix, "hana")["rows"][0]
        self.assertEqual(row["dt"], "2026.01.31\n 13:05:09")
        self.assertEqual((row["type"], row["payee"], row["out"]), ("체크카드", "가맹점", 500))

    def test_unknown_table_stops(self):
        with self.assertRaises(t.ToolError):
            t.normalize_bank([["날짜", "돈"]], "shinhan")

    def test_html_masked_xls(self):
        import pathlib, tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "a.xls"
            path.write_text("<table><tr><th>거래일자</th></tr><tr><td> 2026 </td></tr></table>", encoding="utf-8")
            self.assertEqual(t.read_matrix(path), [["거래일자"], ["2026"]])


class WriteChecks(unittest.TestCase):
    ranges = {"ranges": [{"tab": t.BANK_TAB, "range": "A10:C10", "values": [[5, "2026.03.05", "x"]]}, {"tab": t.USAGE_TAB, "range": "A7:A7", "values": [["작성자"]]}]}

    def test_prenumbered_column_a_is_allowed(self):
        self.assertEqual(t.guard(self.ranges, [[[5]], []]), [])

    def test_filled_target_is_reported(self):
        self.assertEqual(t.guard(self.ranges, [[[5, "", "누가 씀"]], [["다른사람"]]]), ["통장내역 C10", "사용내역 A7"])

    def test_verify_accepts_formatted_numbers(self):
        ranges = {"ranges": [{"tab": t.BANK_TAB, "range": "F10:G10", "values": [[1000, 0]]}]}
        self.assertEqual(t.verify(ranges, [[["1,000", "0"]]]), [])
        self.assertEqual(t.verify(ranges, [[["999", "0"]]]), ["통장내역 F10"])


class References(unittest.TestCase):
    def test_reference_is_evidence_only(self):
        history = [hist(1, "가게", bal=9000)]
        reference = [hist(7, "작년업체", detail="2-3.용역비"), hist(8, "작년업체", detail="2-3.용역비")]
        new = t.row_dict([None, "2026.03.05", "인터넷출금이체", "작년업체", "", 1000, 0, 8000], 0)
        result = t.plan(history, [new], reference)
        self.assertEqual(result["rows"][0]["no"], 2)
        self.assertEqual(result["rows"][0]["suggest"]["L"]["value"], "2-3.용역비")
        self.assertEqual(result["rows"][0]["suggest"]["L"]["sampleNo"], ["참고 7", "참고 8"])


class Harness(unittest.TestCase):
    def test_pack_hides_answers_and_later_rows(self):
        rows = [hist(1, "가게", memo="첫"), hist(2, "가게", memo="둘")]
        entries = t.eval_pack(rows, {}, 1)[0]["entries"]
        self.assertNotIn("L", entries[0]["target"])
        self.assertEqual(entries[0]["similarEarlier"], [])
        self.assertEqual(entries[1]["similarEarlier"][0]["P"], "첫")

    def test_score_counts_exact_and_close_memo(self):
        rows = [hist(1, "가게", memo="워크숍 다과 구입비"), hist(2, "가게", memo="워크숍 다과 구입비")]
        answers = {r["row"]: {"L": "2-1.일반수용비", "O": "틀림", "P": "워크숍 다과 구입 비용", "confidence": "high"} for r in rows}
        report, review = t.eval_score(rows, answers)
        self.assertEqual(report["columns"]["L"]["correct"], 2)
        self.assertEqual(report["columns"]["O"]["correct"], 0)
        self.assertEqual(report["columns"]["P"]["correct"], 0)
        self.assertEqual(report["columns"]["P"]["close"], 2)
        self.assertEqual(len(review), 2)


class Expenses(unittest.TestCase):
    def item(self, memo="다과비", total=1000, vendor="가게", pay="20260305", state="1", project="예시사업_AC"):
        return {"title": "", "state": state, "form": "1211", "issue": pay, "pay": pay, "card": "", "sum": total, "sup": total, "vat": 0, "memo": memo, "purpose": "회의비", "project": project, "vendor": vendor}

    def plan(self, *rows):
        return {"rows": [{"no": i, "dt": dt, "type": kind, "payee": payee, "out": out} for i, (dt, kind, payee, out) in enumerate(rows, 1)]}

    def test_amount_and_date_make_exact_match(self):
        got = t.attach_expenses(self.plan(("2026.03.06\n 10:00:00", "체크카드", "가게", 1000)), [self.item()])
        self.assertEqual(got["rows"][0]["expense"]["match"], "exact")
        self.assertEqual(got["rows"][0]["expense"]["items"][0]["memo"], "다과비")

    def test_far_date_and_other_amount_do_not_match(self):
        far = t.attach_expenses(self.plan(("2026.04.20", "체크카드", "가게", 1000)), [self.item()])
        other = t.attach_expenses(self.plan(("2026.03.05", "체크카드", "가게", 999)), [self.item()])
        self.assertEqual(far["rows"][0]["expense"]["match"], "none")
        self.assertEqual(other["rows"][0]["expense"]["match"], "none")

    def test_same_amount_without_vendor_signal_is_a_choice(self):
        items = [self.item("가", vendor="갑"), self.item("나", vendor="을")]
        got = t.attach_expenses(self.plan(("2026.03.05", "체크카드", "병", 1000)), items)
        self.assertEqual(got["rows"][0]["expense"]["match"], "candidates")
        self.assertEqual(len(got["rows"][0]["expense"]["items"]), 2)

    def test_vendor_breaks_tie(self):
        items = [self.item("가", vendor="갑"), self.item("나", vendor="을")]
        got = t.attach_expenses(self.plan(("2026.03.05", "체크카드", "을", 1000)), items)
        self.assertEqual(got["rows"][0]["expense"]["items"][0]["memo"], "나")

    def test_same_vendor_prefers_nearer_date_but_equal_dates_stay_a_choice(self):
        row = ("2026.03.05", "체크카드", "가게", 1000)
        near = [self.item("가", pay="20260305"), self.item("나", pay="20260303")]
        self.assertEqual(t.attach_expenses(self.plan(row), near)["rows"][0]["expense"]["items"][0]["memo"], "가")
        tied = [self.item("가"), self.item("나")]
        self.assertEqual(t.attach_expenses(self.plan(row), tied)["rows"][0]["expense"]["match"], "candidates")

    def test_one_expense_line_is_used_once(self):
        got = t.attach_expenses(self.plan(("2026.03.05", "체크카드", "가게", 1000), ("2026.03.05", "체크카드", "가게", 1000)), [self.item()])
        self.assertEqual([r["expense"]["match"] for r in got["rows"]], ["exact", "none"])

    def test_project_filter_and_cancelled_documents(self):
        row = ("2026.03.05", "체크카드", "가게", 1000)
        self.assertEqual(t.attach_expenses(self.plan(row), [self.item(project="다른사업")], "예시사업")["rows"][0]["expense"]["match"], "none")
        self.assertEqual(t.attach_expenses(self.plan(row), [self.item(state="-3")])["rows"][0]["expense"]["match"], "none")


class PaymentMethod(unittest.TestCase):
    def test_card_types_use_the_sheet_card(self):
        self.assertEqual(t.payment_method("체크카드", "법인카드(0000)"), "법인카드(0000)")
        self.assertEqual(t.payment_method("인터넷출금이체", "법인카드(0000)"), "계좌이체")

    def test_finalize_adds_method_column_and_format_copy(self):
        history = [hist(1, "가게", bal=9000)]
        planned = t.plan(history, [t.row_dict([None, "2026.03.05", "체크카드", "가게", "", 1000, 0, 8000], 0)])
        result = t.finalize(planned, {"card": "법인카드(0000)"}, {"row": 20, "values": ["", 17], "header": ["작성자", "No.", "", "", "", "지출구분"]}, "작성자")
        self.assertEqual(result["ranges"][-1]["range"], "F5:F5")
        self.assertEqual(result["ranges"][-1]["values"], [["법인카드(0000)"]])
        self.assertEqual(result["formatCopy"]["sourceRow"], t.FIRST_ROW)
        self.assertEqual(t.guard({"ranges": [result["ranges"][-1]]}, [[["확인필요"]]]), [])


class UsageRows(unittest.TestCase):
    def test_prebuilt_rows_get_author_only(self):
        template = {"row": 20, "values": ["a", 17, "=VLOOKUP($B20,X,2)"]}
        ranges, _ = t.usage_rows(template, [16, 17, 18], "작성자")
        self.assertEqual([r["range"] for r in ranges], ["A19:A20", "A21:D21", "F21:W21"])
        self.assertEqual(ranges[1]["values"][0][:3], ["작성자", 18, "=VLOOKUP($B21,X,2)"])


class Finalize(unittest.TestCase):
    def test_low_confidence_stays_blank_and_vat_needs_acceptance(self):
        history = [hist(1, "가게", bal=9000, n=100, m=1000), hist(2, "가게", bal=8000, n=100, m=1000)]
        planned = t.plan(history, [t.row_dict([None, "2026.03.05", "인터넷출금이체", "가게", "", 1100, 0, 6900], 0)])
        template = {"row": 11, "values": ["a", 2, "=VLOOKUP($B11,X!$A$1:$Z$299,2)"], "header": ["작성자", "No.", "", "", "", "지출구분"]}
        result = t.finalize(planned, {}, template, "작성자")
        bank = result["ranges"][0]
        self.assertEqual(bank["range"], f"A{t.FIRST_ROW + 2}:P{t.FIRST_ROW + 2}")
        self.assertEqual(bank["values"][0][11], "2-1.일반수용비")
        self.assertEqual(bank["values"][0][12], "")
        self.assertEqual(result["unanswered"][0]["missing"], ["M", "P"])
        accepted = t.finalize(planned, {"acceptVat": True, "rows": {"3": {"P": "새 적요"}}}, template, "작성자")
        self.assertEqual(accepted["ranges"][0]["values"][0][12:14], [1000, 100])
        self.assertEqual(accepted["unanswered"], [])
        usage = accepted["ranges"][1]
        self.assertEqual(usage["range"], "A12:D12")
        self.assertEqual(usage["values"][0][2], "=VLOOKUP($B12,X!$A$1:$Z$299,2)")
        self.assertEqual(accepted["ranges"][2]["range"], "F12:W12")


if __name__ == "__main__":
    unittest.main()
