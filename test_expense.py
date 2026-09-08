"""실행: python3 test_expense.py [실제 조회로 생성한 .plan.json 파일]"""
import copy
import contextlib
from email import policy
from email.parser import BytesParser
import hashlib
import io
import json
from argparse import Namespace
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from unittest.mock import patch

import expense as app
from statement import StatementError, match_candidates, read_statement

from expense import (API, Client, ExpenseError, edit, make_plan, native_binding, preflight, read_private,
                     render_native, verify_native_links, verify_receipt, write_private)


def check():
    transaction = {"coCd": "1000", "cardCd": "TEST-CARD", "issDt": "20260908",
                   "issSq": 71, "sunginNb": "TEST-APPROVAL", "sunginAm": 11000,
                   "vatAm": 1000, "appSumAm": 0, "appSupAm": 0, "appVatAm": 0,
                   "appYn": "N", "chainName": "테스트 가맹점", "chainRegnb": "TEST",
                   "payDt": "20261012", "liqWs": "1"}
    previous = {"coCd": "1000", "cardCd": "TEST-CARD", "trNm": "테스트 가맹점",
                "regNb": "TEST", "rmkDc": "사용료", "cashCd": "01", "pjtCd": "P1",
                "payDt": "20260112", "realDepositorFg": "1", "realDepositorDc": "과거 실명"}
    snapshot = {"period": {"from": "20260901", "to": "20260908"},
                "account": {"companyCode": "1000", "userCode": "E1", "userName": "테스트",
                            "deptCode": "D1", "deptName": "테스트 부서"},
                "form": {"formId": 1211, "formDTp": "APB1020_00001", "formNm": "법인카드"},
                "division": {"divCd": "1000", "divNm": "테스트"},
                "gisu": {"gisu": 16}, "cards": [transaction],
                "receipts": [[dict(transaction)]], "holidays": [],
                "payMethods": [{"paymethodFg": "01", "attrCd": "TEST-PAYMENT"}],
                "cashTypes": [{"typeCd": "01", "typeNm": "사용료"}],
                "projects": [{"pjtCd": "P1", "pjtNm": "프로젝트", "frDt": "20260101", "toDt": "20261231"}],
                "history": [{"document": {"linkKey": "old", "isuDt": "20260101"},
                             "head": {"empCd": "E1", "approState": "1"}, "detail": [previous]}]}
    plan = make_plan(snapshot)
    assert plan["rows"][0]["expense"]["attrCd"] == "TEST-PAYMENT"
    for methods in ([], [{"paymethodFg": "01", "attrCd": ""}],
                    [{"paymethodFg": "01", "attrCd": "ONE"}, {"paymethodFg": "01", "attrCd": "TWO"}]):
        invalid = copy.deepcopy(snapshot)
        invalid["payMethods"] = methods
        try:
            make_plan(invalid)
        except ExpenseError:
            pass
        else:
            raise AssertionError("조회되지 않거나 모호한 지급수단 코드를 추정했습니다")
    duplicated = copy.deepcopy(plan)
    duplicated["rows"].append(copy.deepcopy(duplicated["rows"][0]))
    try:
        preflight(Namespace(erp=snapshot["account"]), duplicated)
    except ExpenseError:
        pass
    else:
        raise AssertionError("같은 거래의 중복 결의를 허용했습니다")
    row = plan["rows"][0]["expense"]
    assert (row["cardDt"], str(row["cardSq"]), row["sunginNb"]) == ("20260908", "71", "TEST-APPROVAL")
    assert row["sumAm"] == 11000 and row["supAm"] + row["vatAm"] == row["sumAm"]
    assert row["payDt"] == "20261012" and not row.get("realDepositorFg")
    assert row["pjtCd"] == "P1" and row["rmkDc"] == "사용료"
    ready = copy.deepcopy(plan)
    ready["source"]["cardName"] = "테스트 카드"
    responses = {"/personal/modulecommon/config/getConfig": {"syscfg": []},
                 "/personal/APCodePicker/ApAcashcdTypeCode": snapshot["cashTypes"],
                 API + "0ap00041": snapshot["payMethods"], API + "0ap00050": "",
                 API + "0ap00033": [], API + "0ap00034": [], API + "0ap00039": []}
    current = Namespace(erp=snapshot["account"], post=lambda path, params: responses[path],
                        cards=lambda *args: [transaction], codepicker=lambda *args, **kwargs: snapshot["projects"],
                        receipt=lambda row: snapshot["receipts"][0])
    assert preflight(current, ready)[0]["attrCd"] == "TEST-PAYMENT"
    responses[API + "0ap00041"] = [{"paymethodFg": "01", "attrCd": "CHANGED-PAYMENT"}]
    try:
        preflight(current, ready)
    except ExpenseError as error:
        assert "지급수단 코드" in str(error)
    else:
        raise AssertionError("현재 설정과 달라진 지급수단 코드로 저장을 허용했습니다")
    changed = dict(transaction, sunginNb="WRONG-RECEIPT")
    try:
        verify_receipt(transaction, [changed])
    except ExpenseError:
        pass
    else:
        raise AssertionError("다른 거래의 영수증을 허용했습니다")
    conflicted = copy.deepcopy(snapshot)
    conflicted["history"][0]["detail"].append(dict(previous, rmkDc="다른 업무 목적"))
    assert not make_plan(conflicted)["rows"][0]["expense"]["rmkDc"]
    other_user = copy.deepcopy(snapshot)
    other_user["history"][0]["head"]["empCd"] = "E2"
    assert not make_plan(other_user)["rows"][0]["expense"]["rmkDc"], "다른 사람의 분류를 적용했습니다"
    taxi = copy.deepcopy(snapshot)
    taxi["cards"][0]["chainName"] = "카카오모빌리티"
    taxi["receipts"][0][0]["chainName"] = "카카오모빌리티"
    taxi["receipts"][0][0]["formatedIssDtTime"] = "2026-09-08 23:40:00"
    taxi["history"] = [{"document": {"linkKey": f"{owner}-{number}", "isuDt": "20260831"},
                        "head": {"empCd": owner, "approState": "1"},
                        "detail": [dict(previous, trNm="카카오모빌리티", rmkDc=description)]}
                       for owner, description in (("E1", "야근 택시비"), ("E2", "출장 이동비")) for number in (1, 2)]
    statement = {"count": 1, "total": 11000, "charges": [{"id": "p1r1", "page": 1, "date": "20260908",
                  "merchant": "카카오모빌리티", "amount": 11000, "currency": "KRW"}]}
    taxi_plan = app.prepare_plan(taxi, statement, {}, "20260908")
    assert taxi_plan["rows"][0]["expense"]["rmkDc"] == "야근 택시비"
    assert [q["kind"] for q in app.inspect_plan(taxi_plan)["questions"]] == ["purpose_review"]
    taxi["account"]["userCode"] = "E2"
    assert app.prepare_plan(taxi, statement, {}, "20260908")["rows"][0]["expense"]["rmkDc"] == "출장 이동비"
    with tempfile.TemporaryDirectory() as directory:
        original, output = Path(directory) / "taxi.plan.json", Path(directory) / "confirmed.plan.json"
        write_private(original, taxi_plan)
        for query in (None, "택시"):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                app.context_command(Namespace(plan=original, row=1, query=query))
            context = json.loads(stream.getvalue())
            assert context["approvalAt"] == "2026-09-08 23:40:00" and context["historyMatchCount"] == 2
            assert all(row["document"].startswith("E1-") and row["pjtCd"] == "P1" for row in context["approvedHistory"])
            assert "TEST-CARD" not in stream.getvalue() and "TEST-APPROVAL" not in stream.getvalue()
        with patch.object(app, "Client", return_value=Namespace()):
            try:
                app.draft(Namespace(plan=original, pdf=None))
            except ExpenseError as error:
                assert "확인할 항목" in str(error)
            else:
                raise AssertionError("이번 사용 목적 검토 없이 저장했습니다")
        with contextlib.redirect_stdout(io.StringIO()):
            edit(Namespace(plan=original, out=output, row=1, set=["description=야근 택시비"], evidence=None, page=None, reason=None))
        assert not app.inspect_plan(read_private(output))["questions"]
        assert read_private(output)["profile"] == taxi_plan["profile"]
    print("사용자별 승인 이력 분리·택시 사용 목적 검토·근거 조회·미확인 저장 차단 검증 통과")
    expired = copy.deepcopy(snapshot)
    expired["projects"][0]["toDt"] = "20260831"
    assert make_plan(expired)["rows"][0]["issues"]
    refund = copy.deepcopy(snapshot)
    refund["cards"][0].update(sunginAm=-11000, vatAm=-1000)
    refund["receipts"] = [[dict(refund["cards"][0])]]
    assert make_plan(refund)["rows"][0]["expense"]["sumAm"] == -11000
    requested = []
    client = Client.__new__(Client)
    def post(path, params):
        requested.append((path, copy.deepcopy(params)))
        if path == API + "0ap00008":
            return [{"cardCd": "OTHER", "cardNm": "다른 카드", "cardFg": "1"},
                    {"cardCd": "TEST-CARD", "cardNm": "테스트 카드", "cardFg": "1"}]
        if path == API + "0ap00029":
            assert params["baNbList"] == [{"baNb": "TEST-CARD", "cardFg": "1"}]
            return [transaction]
        return [dict(transaction)]
    client.post = post
    assert len(client.cards("20260901", "20260908", "테스트 카드")) == 1
    try:
        client.cards("20260801", "20260831", "테스트 카드")
    except ExpenseError:
        pass
    else:
        raise AssertionError("선택 기간 밖의 거래를 허용했습니다")
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        original, output, evidence = directory / "original", directory / "edited", directory / "evidence.pdf"
        write_private(original, plan)
        evidence.write_bytes(b"%PDF-1.7\nTEST EVIDENCE\n")
        with contextlib.redirect_stdout(io.StringIO()):
            edit(Namespace(plan=original, out=output, row=1, set=["amount=12000", "vat=1000"],
                           evidence=evidence, page=1, reason="청구명세서 대조"))
        changed = read_private(output)
        assert changed["rows"][0]["expense"]["sumAm"] == 12000
        assert changed["rows"][0]["expense"]["supAm"] == 11000
        assert changed["rows"][0]["expense"]["orgCardSumAm"] == 11000
        assert changed["source"] == plan["source"] and read_private(original) == plan
        assert changed["rows"][0]["amountAdjustment"]["sha256"] == hashlib.sha256(evidence.read_bytes()).hexdigest()
        try:
            edit(Namespace(plan=original, out=directory / "invalid", row=1, set=["amount=12000"],
                           evidence=None, page=None, reason=None))
        except ExpenseError:
            pass
        else:
            raise AssertionError("근거 없이 증빙 금액을 수정했습니다")
    if len(sys.argv) > 1:
        saved = read_private(sys.argv[1])
        rebuilt = make_plan(saved["source"])
        assert rebuilt["rows"], "실제 거래가 없는 파일입니다"
        for item, transaction, receipt in zip(rebuilt["rows"], saved["source"]["cards"], saved["source"]["receipts"]):
            verify_receipt(transaction, receipt)
            assert str(item["expense"]["cardSq"]) == str(transaction["issSq"])
            assert item["expense"]["cardDt"] == transaction["issDt"]
            assert item["expense"]["sumAm"] == int(transaction["sunginAm"]) - int(transaction.get("appSumAm") or 0)
        print(f"실제 카드 거래 {len(rebuilt['rows'])}건: 영수증 식별자·잔액 보존 검증 통과")
        for item in saved["rows"]:
            adjustment = item.get("amountAdjustment")
            if adjustment:
                assert item["expense"]["sumAm"] == adjustment["amount"]
                assert hashlib.sha256(Path(adjustment["evidencePath"]).read_bytes()).hexdigest() == adjustment["sha256"]
        if saved.get("statement"):
            # ponytail: 이번 현대카드 A4 명세서 검증. 서식 변경 시 좌표·열 확인을 다시 한다.
            raw = subprocess.run(["pdftotext", "-bbox", saved["statement"]["file"], "-"], capture_output=True, check=True).stdout
            root = ET.fromstring(raw)
            namespace = {"x": "http://www.w3.org/1999/xhtml"}
            charges = []
            for page in root.findall(".//x:page", namespace):
                words = page.findall("x:word", namespace)
                for word in words:
                    if float(word.get("xMin")) < 100 and re.fullmatch(r"\d{6}", word.text or ""):
                        amounts = [value for value in words if 430 < float(value.get("xMin")) < 480
                                   and 0 < float(value.get("yMin")) - float(word.get("yMin")) < 12
                                   and re.fullmatch(r"[\d,]+", value.text or "")]
                        assert len(amounts) == 1, "청구금액 열을 한 개로 식별할 수 없습니다"
                        charges.append(("20" + word.text, int(amounts[0].text.replace(",", ""))))
            assert len(charges) == saved["statement"]["count"]
            assert sum(amount for _, amount in charges) == saved["statement"]["total"]
            positive = [(item["expense"]["cardDt"], item["expense"]["sumAm"])
                        for item in saved["rows"] if item["expense"]["sumAm"] > 0]
            assert sorted(positive) == sorted(charges), "PDF 청구금액과 검토안이 일치하지 않습니다"
            assert all(item["expense"]["payDt"] == saved["dateChoice"]["payDate"] for item in saved["rows"])
            print(f"실제 PDF {len(charges)}건: 날짜·청구금액·합계 및 수정 근거 검증 통과")
            pdf = Path(saved["statement"]["file"])
            requests = []
            class CaptureUpload:
                def open(self, request, timeout):
                    requests.append(request)
                    return io.BytesIO(b'{"resultCode":0,"resultData":{"list":[{"fileId":"CHECK"}]}}')
            upload = Client({"token": "LOCAL-CHECK", "signKey": "LOCAL-CHECK", "erp": snapshot["account"], "uc": {}})
            upload.opener = CaptureUpload()
            assert upload.post("/ecm/ecm001A01", {"moduleGbn": "EAP"}, pdf=pdf)["list"][0]["fileId"] == "CHECK"
            request = requests[0]
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + request.get_header("Content-type") + "\r\n\r\n").encode() + request.data)
            parts = list(message.iter_parts())
            assert len(parts) == 2 and parts[0].get_payload(decode=True) == b"EAP"
            assert parts[1].get_param("name", header="content-disposition") == "file[]"
            assert parts[1].get_filename() == pdf.name and parts[1].get_payload(decode=True) == pdf.read_bytes()
            params = {"moduleGbn": "EAP", "authKeyMap": '{"docId":1}', "fileSn": [1, 2], "condition": "99"}
            upload.post("/ecm/ecm001A04", params)
            request = requests[-1]
            assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
            assert urllib.parse.parse_qs(request.data.decode()) == {
                "moduleGbn": ["EAP"], "authKeyMap": ['{"docId":1}'], "fileSn": ["1,2"], "condition": ["99"]}
            print("실제 PDF: multipart 업로드 본문의 파일명·내용 보존 검증 통과 (서버 저장은 별도 검증)")
            statement = read_statement(pdf, saved["source"]["period"]["from"][:4] + "-" + saved["source"]["period"]["from"][4:6])
            assert statement["count"] == len(charges) and statement["total"] == sum(amount for _, amount in charges)
            assert [(r["date"], r["amount"]) for r in statement["charges"]] == charges
            example = next(r for r in saved["rows"] if not r.get("excluded") and r["expense"].get("pjtCd") and r["expense"].get("cashCd"))
            adjusted_count = sum(bool(r.get("amountAdjustment")) for r in saved["rows"] if not r.get("excluded"))
            profile = {"schema": 1, "owner": {key: str(saved["source"]["account"][key]) for key in ("companyCode", "userCode")},
                       "cardName": saved["source"]["cardName"], "defaultProject": example["expense"]["pjtCd"],
                       "dates": {"accounting": "month-end", "payment": "today"}, "rules": [], "merchantAliases": []}
            prepared = app.prepare_plan(saved["source"], statement, profile, saved["dateChoice"]["payDate"])
            assert prepared["source"] == saved["source"], "명세서 대조가 카드 승인 원본을 수정했습니다"
            assert len([r for r in prepared["rows"] if r.get("amountAdjustment")]) == adjusted_count
            assert app.inspect_plan(prepared)["questions"], "불확실한 가맹점·적요를 묻지 않았습니다"
            assert any(q["kind"] == "history" and q["options"] for q in app.inspect_plan(prepared)["questions"])
            unmatched = [r for r in statement["charges"] if r["id"] not in {i.get("statementMatch", {}).get("id") for i in prepared["rows"]}]
            for charge in unmatched:
                candidates, _ = match_candidates(charge, [r for r in prepared["rows"] if not r.get("statementMatch")])
                assert len(candidates) == 1, "이번 실제 파일의 확인된 대응관계가 달라졌습니다"
                app.bind_statement(prepared, candidates[0], charge, "테스트: 기존에 사용자 확인한 날짜·청구액 대응관계")
            for row in prepared["rows"]:
                if row["expense"]["sumAm"] < 0:
                    row["excluded"] = "테스트: 이번 사용자 확인한 명세서 밖 취소"
            assert app.statement_questions(prepared) == []
            assert app.inspect_plan(prepared)["total"] == statement["total"]
            bad = copy.deepcopy(prepared)
            bad["rows"][1]["statementMatch"] = copy.deepcopy(bad["rows"][0]["statementMatch"])
            try:
                app.statement_questions(bad)
            except ExpenseError:
                pass
            else:
                raise AssertionError("명세서 중복 연결을 허용했습니다")
            bad = copy.deepcopy(prepared)
            bad["statement"]["charges"][0]["amount"] += 1
            try:
                app.preflight(Namespace(erp=saved["source"]["account"]), bad)
            except ExpenseError:
                pass
            else:
                raise AssertionError("PDF 추출 금액의 임의 변경을 허용했습니다")
            duplicated = [copy.deepcopy(prepared["rows"][0]), copy.deepcopy(prepared["rows"][0])]
            first_charge = next(c for c in statement["charges"] if c["id"] == duplicated[0]["statementMatch"]["id"])
            assert len(match_candidates(first_charge, duplicated)[0]) == 2
            with tempfile.TemporaryDirectory() as directory:
                source_profile, new_profile = Path(directory) / "profile.json", Path(directory) / "new.json"
                write_private(source_profile, profile)
                other = dict(saved["source"]["account"], userCode="OTHER-USER")
                try:
                    app.load_profile(source_profile, other)
                except ExpenseError:
                    pass
                else:
                    raise AssertionError("다른 사용자의 프로필을 사용했습니다")
                with patch.object(app, "Client", return_value=Namespace(erp=saved["source"]["account"])), contextlib.redirect_stdout(io.StringIO()):
                    target = example["expense"]
                    matched = next(r for r in prepared["rows"] if r["identity"] == example["identity"])
                    statement_name = next(c["merchant"] for c in statement["charges"] if c["id"] == matched["statementMatch"]["id"])
                    app.rule_command(Namespace(profile=source_profile, merchant=target["trNm"], description="명시적으로 등록한 적요",
                                               purpose=target["cashCd"], project=target["pjtCd"], statement_merchant=statement_name, out=str(new_profile)))
                registered = app.load_profile(new_profile, saved["source"]["account"])
                assert read_private(source_profile) == profile, "기존 프로필을 덮어썼습니다"
                ruled = app.prepare_plan(saved["source"], statement, registered, saved["dateChoice"]["payDate"])
                matching = [r for r in ruled["rows"] if r["expense"]["trNm"] == target["trNm"]]
                assert matching and all(r["expense"]["rmkDc"] == "명시적으로 등록한 적요" for r in matching)
                confirmed = Path(directory) / "confirmed.plan.json"
                staged = Path(directory) / "staged.plan.json"
                write_private(staged, ruled)
                with contextlib.redirect_stdout(io.StringIO()):
                    edit(Namespace(plan=staged, out=confirmed, row=1, set=["description=이번만 사용하는 적요"], evidence=None, page=None, reason=None))
                assert "rmkDc" in read_private(confirmed)["rows"][0]["confirmedFields"]
                assert read_private(new_profile) == registered, "일회성 답변이 영구 규칙으로 변경됐습니다"
            print("실제 PDF → 재사용 초안: 거래 대조·청구액 수정안·질문·개인 규칙·일회성 확인 검증 통과")
    if len(sys.argv) > 2:
        initial = read_private(sys.argv[2])
        binding = native_binding(initial)
        content = render_native(initial, {"deptName": "검증 부서", "empName": "검증 사용자"}, "<본문 삽입 방지>", "2026-09-08")
        expected = []
        for group in binding["TABLE"]["table2"]["group"]:
            values = urllib.parse.parse_qs(group["items"]["attrNm"]["href"].split("?", 1)[1])
            expected.append({"coCd": values["coCd"][0], "cardDt": values["issDt"][0], "cardSq": values["issSq"][0]})
        verify_native_links(content, expected)
        assert "<본문 삽입 방지>" not in content and "&lt;본문 삽입 방지&gt;" in content
        assert binding["ITEMS"]["totSumAm"] in content
        try:
            verify_native_links(content, expected[:-1])
        except ExpenseError:
            pass
        else:
            raise AssertionError("원본 거래와 다른 결재 본문의 증빙 링크를 허용했습니다")
        print(f"실제 전자결재 양식 {len(expected)}행: 반복 행·영수증 링크·합계 렌더링 검증 통과")
    if len(sys.argv) > 3:
        journal = read_private(sys.argv[3])
        assert journal["status"] == "verified" and journal["docId"]
        verify_native_links(journal["verifiedContent"], [item["expense"] for item in journal["plan"]["rows"]])
        assert journal["eapPayload"]["paramItem"]["doc_sts"] == "10"
        print("실제 저장 후 재조회한 문서의 증빙 링크와 임시보관 요청 상태 검증 통과")
        # 실제 저장 데이터로 HTTP 응답 유실과 체크포인트 재개를 재현한다. 실제 서버에는 쓰지 않는다.
        class CheckpointClient:
            def __init__(self):
                self.erp = journal["plan"]["source"]["account"]
                params = journal["eapPayload"]["paramItem"]
                self.uc = {"compSeq": params["co_id"], "empSeq": params["user_id"], "bizSeq": params["biz_id"],
                           "deptSeq": params["dept_id"], "compName": params["co_nm"], "deptName": params["dept_nm"],
                           "empName": params["user_nm"], "groupSeq": "CHECK"}
                self.writes, self.saved, self.last_response = [], False, None
            def document(self, key):
                assert key == journal["linkKey"]
                return {"head": dict(journal["plan"]["head"], approState="5"),
                        "detail": [r["expense"] for r in journal["plan"]["rows"]]}
            def receipt(self, row):
                source = journal["plan"]["source"]
                return next(receipt for card, receipt in zip(source["cards"], source["receipts"]) if app.identity(card) == app.identity(row))
            def post(self, path, params, **kwargs):
                if path in (API + "0ap01002", API + "0ap01001", "/ecm/ecm001A01", "/eap/eap110A06"):
                    self.writes.append(path)
                if path == "/ecm/ecm001A01":
                    assert Path(kwargs["pdf"]).read_bytes() == Path(journal["pdf"]["path"]).read_bytes()
                    return {"list": [{"fileId": journal["attachments"][0]["fileId"]}]}
                if path == API + "0ap01002":
                    return {key: journal[key] for key in ("linkKey", "approKey")}
                if path == API + "0ap01001":
                    return None
                if path == API + "0ap00020":
                    return [{"approKey": journal["approKey"], "docId": journal["docId"] if self.saved else None, "empCd": self.erp["userCode"]}]
                if path == "/eap/eap110A03":
                    data = copy.deepcopy(initial)
                    if not params.get("docID"):
                        data["resultMap"]["appdocinfo"] = []
                    else:
                        data["resultMap"]["appdocinfo"] = [{"doc_sts": "10", "approkey": journal["approKey"]}]
                        data["resultMap"]["appdoccontent"] = [{"doc_contents": journal["verifiedContent"]}]
                        data["resultMap"]["fileAttachInfo"] = [{"fileKey": journal["verifiedPdf"]["fileKey"]}] if journal.get("verifiedPdf") else []
                    return data
                if path == "/eap/eap110A06":
                    assert params["paramItem"]["doc_sts"] == "10"
                    self.saved = True
                    self.last_response = {"resultCode": 2001}
                    raise ExpenseError("테스트: 저장 후 오류 응답")
                if path == "/ecm/ecm001A04":
                    return {"list": journal["attachments"]}
                if path == "/ecm/ecm001A03":
                    return Path(journal["pdf"]["path"]).read_bytes()
                raise AssertionError(path)
        if len(expected) == len(journal["plan"]["rows"]):
            for state in ("prepared", "pdf_uploaded", "link_saved", "erp_saved", "eap_attempted", "external_doc"):
                simulated, client = copy.deepcopy(journal), CheckpointClient()
                simulated.update(status="erp_saved" if state == "external_doc" else state, docId=None)
                client.saved = state in ("eap_attempted", "external_doc")
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "checkpoint.json"
                    write_private(path, simulated)
                    with patch.object(app, "preflight", return_value=[r["expense"] for r in simulated["plan"]["rows"]]):
                        app.continue_draft(client, simulated, path)
                    before = list(client.writes)
                    app.continue_draft(client, simulated, path)
                    assert client.writes == before and simulated["status"] == "verified"
                    assert before.count("/eap/eap110A06") == (0 if state in ("eap_attempted", "external_doc") else 1)
                    assert (API + "0ap01001" in before) == (state in ("prepared", "pdf_uploaded", "link_saved"))
                    assert ("/ecm/ecm001A01" in before) == (state == "prepared" and bool(journal.get("pdf")))
            print("실제 문서 데이터: 5개 저장 단계 재개·2001 응답 유실·외부 저장 감지·완료 후 무중복 검증 통과")
        for state in ("pdf_upload_attempted", "link_attempted", "erp_attempted"):
            simulated = copy.deepcopy(journal)
            simulated.update(status=state, docId=None)
            client = CheckpointClient()
            try:
                app.continue_draft(client, simulated, Path("NOT-WRITTEN"))
            except ExpenseError:
                pass
            else:
                raise AssertionError("결과 불명확한 쓰기를 재전송했습니다")
            assert client.writes == []
    print("이력 충돌·종료 프로젝트·실명확인 재사용 방지·취소 거래 검증 통과")


if __name__ == "__main__":
    check()
