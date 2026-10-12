"""실행: python3 test_taxinvoice.py  (네트워크·실제 PDF 없음)"""
import copy
import json
from pathlib import Path
import tempfile

import expense as app
import taxinvoice as tax
from expense import API, ExpenseError, read_private, write_private


# 홈택스 표준 서식을 pdftotext -layout으로 읽은 모양. 번호·이름은 가상값이다.
TAX_TEXT = """
                                             공급받는자 승인번호        20260930-41000015-00000001
                         전자세금계산서               (보관용) 관리번호        202609300000000000000001
              등록번호      111-11-11111                         등록번호     214-88-87695
      공       상호 테스트클라우드 코리아     성명 홍길동      공     상호 주식회사                  성명
               (법인명) 유한회사       (대표자) 홍길동            (법인명) 엠와이소셜컴퍼니          (대표자)
          작성일자          공급가액            세액          수정사유                 비고
          2026-09-30           59,790          5,979                     결제 ID: 0000-0000-0000
          월 일      품목            규격   수량   단가    공급가액      세액     비고
          09 30 디지털 서비스                               59,790     5,979
               합계금액       현금      수표      어음      외상미수금        [영수]
                                                                  이 금액을   청구 함.
                   65,769
"""


def variant(text, **replace):
    for old, new in replace.items():
        text = text.replace(old, new)
    return text


def check_parse():
    invoice = tax.parse_invoice(TAX_TEXT)
    assert invoice["type"] == "세금계산서" and invoice["receipt"] == "영수"
    assert (invoice["issued"], invoice["supply"], invoice["vat"], invoice["total"]) == ("20260930", 59790, 5979, 65769)
    assert invoice["supplier"] == "테스트클라우드 코리아 유한회사" and invoice["buyer"] == "주식회사 엠와이소셜컴퍼니"
    assert invoice["approval"] == "20260930-41000015-00000001" and invoice["supplierRegNb"] == "1111111111"

    # 청구 표시, 날짜 서식 차이
    claimed = tax.parse_invoice(variant(TAX_TEXT, **{"[영수]": "영수", "청구 함.": "[청구] 함.", "2026-09-30 ": "2026.09.30 "}))
    assert claimed["receipt"] == "청구" and claimed["issued"] == "20260930"

    # 면세 계산서: 세액 칸이 없다
    exempt = variant(TAX_TEXT, 전자세금계산서="전자계산서", **{"          5,979                     결제": "                     결제",
                                                                "59,790     5,979": "59,790", "65,769": "59,790"})
    exempt = tax.parse_invoice(exempt.replace("세액", ""))
    assert exempt["type"] == "계산서" and exempt["vat"] == 0 and exempt["total"] == 59790

    # 표시가 없으면 방식은 사용자에게 묻는다
    assert tax.parse_invoice(variant(TAX_TEXT, **{"[영수]": "영수"}))["receipt"] == ""

    # 상호를 못 읽어도 금액은 읽고, 이름은 비워 둔다
    assert tax.parse_invoice(variant(TAX_TEXT, 성명="대표"))["supplier"] is None

    for broken, reason in ((variant(TAX_TEXT, **{"65,769": "65,770"}), "합계 불일치"),
                           (variant(TAX_TEXT, **{"승인번호": "번호"}), "승인번호 없음"),
                           (variant(TAX_TEXT, **{"20260930-4100": "20260929-4100"}), "승인일·작성일 불일치"),
                           (variant(TAX_TEXT, **{"59,790          5,979": "-59,790          -5,979", "65,769": "-65,769"}), "음수"),
                           (variant(TAX_TEXT, **{"수정사유                 비고": "수정사유 환입 비고"}), "수정세금계산서"),
                           ("영수증\n합계 1,000", "다른 문서")):
        try:
            tax.parse_invoice(broken)
        except ExpenseError:
            continue
        raise AssertionError(reason + "을 통과시켰습니다")
    print("PDF 읽기: 세금계산서·계산서·영수/청구·서식 차이·수정/음수/불일치 차단 통과")


ACCOUNT = {"companyCode": "1000", "userCode": "E1", "userName": "테스트", "deptCode": "D1", "deptName": "부서"}
OWN = {"bankCd": "040", "bankNm": "국민", "baNb": "***@!1!@own", "decryptBaNb": "11112222333344",
       "depositorDc": "(주)엠와이소셜컴퍼니"}
VENDOR = {"bankCd": "088", "bankNm": "신한", "baNb": "***@!1!@vendor", "decryptBaNb": "99998888",
          "depositorDc": "테스트클라우드코리아(유)"}


def document(link, state, title, **row):
    base = {"issDt": "20260430", "sumAm": 426.0, "trNm": "테스트클라우드 코리아 유한회사", "rmkDc": "서버 사용료 ",
            "cashCd": "0350", "pjtCd": "P1", **OWN}
    return {"document": {"linkKey": link, "docNo": "MYSC-" + link, "approState": state},
            "head": {"linkKey": link, "approState": state, "docTitle": title, "isuDt": "20260430"},
            "detail": [{**base, **row}]}


def context(files, **overrides):
    result = {"today": "20261012", "account": ACCOUNT, "division": {"divCd": "1000", "divNm": "주식회사 엠와이소셜컴퍼니"},
              "documents": [document("A", "1", "기타지급수수료_서버 사용료")],
              "form": {"formId": 1210, "formDTp": "APB1020_00001", "formNm": "지출결의서(세금계산서)"},
              "attrCodes": {"세금계산서": "1", "계산서": "2"}, "gisu": {"gisu": 16, "frDt": "20260101"},
              "dateRules": {"syscfg": [{"ctrCd": "51", "fgTy": "2"}, {"ctrCd": "60", "fgTy": "0"}]},
              "cashTypes": [{"typeCd": "0350", "typeNm": "기타지급수수료", "deductFg": "1", "pjtCheckYn": "Y",
                             "accountCheckYn": "Y", "trcdCheckYn": "N", "deptCheckYn": "N", "carYn": "N",
                             "budgetCheckYn": "N"},
                            {"typeCd": "0999", "typeNm": "소프트웨어", "deductFg": "5", "pjtCheckYn": "Y",
                             "accountCheckYn": "Y"}],
              "projects": [{"pjtCd": "P1", "pjtNm": "테스트 경비", "frDt": "20260101", "toDt": "20261231"}],
              "duplicates": {}, "defaultProject": "P1",
              "cardMatches": {}}
    result.update(overrides)
    return result


def invoices(directory, count=1, **changes):
    result = []
    for index in range(count):
        path = Path(directory) / f"invoice{index}.pdf"
        path.write_bytes(b"%PDF-1.4 test " + str(index).encode())
        invoice = tax.parse_invoice(TAX_TEXT.replace("00000001", f"0000000{index + 1}"))
        result.append({**invoice, **changes, "file": str(path)})
    return result


def check_plan():
    with tempfile.TemporaryDirectory() as directory:
        card = Path(directory) / "card.pdf"
        card.write_bytes(b"%PDF-1.4 card")
        items = invoices(directory)
        approval = items[0]["approval"]
        matched = {approval: [{"date": "20261002", "merchant": "테스트", "amount": 65769}]}

        # 인식용 기본: 이전 승인 문서의 제목·적요·용도·프로젝트, 회사 계좌, 출금계좌 비움, 증빙일=회계일
        plan = tax.build_plan(items, context(directory, cardMatches=matched), {"attach": [str(card)]})
        row = plan["rows"][0]
        assert plan["mode"] == "recognition" and plan["issues"] == [] and plan["warnings"] == []
        assert plan["head"]["isuDt"] == row["issDt"] == "20260930" and row["payDt"] == "20261012"
        assert (row["cashCd"], row["attrCd"], row["pjtNm"], row["rmkDc"]) == ("0350", "1", "테스트 경비", "서버 사용료")
        assert (row["supAm"], row["vatAm"], row["sumAm"]) == (59790, 5979, 65769)
        assert row["baNb"] == OWN["baNb"] and plan["accountDigits"] == OWN["decryptBaNb"]
        assert plan["body"]["withdraw"] == [] and "10월 2일 결제" in plan["body"]["notes"][0]
        assert "구글" not in json.dumps(plan["body"], ensure_ascii=False)
        assert [Path(item["path"]).name for item in plan["attachments"]] == ["invoice0.pdf", "card.pdf"]

        # 카드 결제를 못 찾거나 여러 건이면 묻는다. 결제일을 주면 해소된다. 이체 결제도 지원
        assert any("못 찾음" in issue for issue in tax.build_plan(items, context(directory), {})["issues"])
        twice = {approval: matched[approval] * 2}
        assert any("여러 건" in issue for issue in tax.build_plan(items, context(directory, cardMatches=twice), {})["issues"])
        transfer = tax.build_plan(items, context(directory), {"paidDates": ["2026-10-05"], "paidBy": "transfer"})
        assert transfer["issues"] == [] and "이체" in transfer["body"]["notes"][0] and "10월 5일" in transfer["body"]["notes"][0]

        # 중복: 진행·승인 문서는 차단, 반려 문서는 참고만
        duplicate = {approval: [{"docNo": "X", "linkKey": "X", "title": "t", "state": "5"}]}
        assert any("1210 문서가 있습니다" in issue for issue in tax.build_plan(items, context(directory, cardMatches=matched, duplicates=duplicate), {})["issues"])
        duplicate[approval][0]["state"] = "4"
        rejected = tax.build_plan(items, context(directory, cardMatches=matched, duplicates=duplicate), {})
        assert rejected["issues"] == [] and any("1210 문서가 있습니다" in warning for warning in rejected["warnings"])

        # 처음 보는 거래처: 제목·적요를 추측하지 않는다. 회사 계좌(인식용)는 다른 거래처 문서에서 가져와도 된다
        other = [{**items[0], "supplier": "새거래처 주식회사"}]
        fresh = tax.build_plan(other, context(directory, cardMatches=matched), {})
        assert any("제목" in issue for issue in fresh["issues"]) and any("적요" in issue for issue in fresh["issues"])
        assert fresh["rows"][0]["baNb"] == OWN["baNb"]
        named = tax.build_plan(other, context(directory, cardMatches=matched), {"title": "기타지급수수료_새 서비스", "memo": ["새 서비스 사용료"]})
        assert named["issues"] == [] and any("새로 정해야" in warning for warning in named["warnings"])

        # 이력이 전혀 없으면 계좌를 만들어내지 않는다
        empty = tax.build_plan(items, context(directory, documents=[], cardMatches=matched), {"title": "t", "memo": ["m"]})
        assert any("수취 은행·계좌" in issue for issue in empty["issues"]) and empty["rows"][0]["baNb"] == ""

        # 지급요청([청구]): 출금계좌 필수, 같은 거래처 계좌만, 회사 계좌는 쓰지 않음
        claimed = invoices(directory, receipt="청구")
        payment = tax.build_plan(claimed, context(directory), {})
        assert payment["mode"] == "payment" and any("출금계좌" in issue for issue in payment["issues"])
        assert any("수취 은행·계좌" in issue for issue in payment["issues"])
        paid_before = context(directory, documents=[document("A", "1", "기타지급수수료_서버 사용료"),
                                                    document("B", "1", "기타지급수수료_서버 사용료", **VENDOR)])
        payment = tax.build_plan(claimed, paid_before, {"withdraw": ["MYSC 운영비 계좌"]})
        assert payment["issues"] == [] and payment["rows"][0]["baNb"] == VENDOR["baNb"]
        assert payment["body"]["withdraw"] == ["MYSC 운영비 계좌"] and "지급을 요청" in payment["body"]["notes"][0]

        # 영수·청구가 섞이거나 표시가 없으면 방식을 묻는다
        unknown = invoices(directory, receipt="")
        assert any("--mode" in issue for issue in tax.build_plan(unknown, context(directory), {})["issues"])

        # 여러 장: 행마다 금액·결제일, 승인번호 모두 비고에, 제목은 직접
        many = invoices(directory, count=2)
        both = {item["approval"]: matched[approval] for item in many}
        multi = tax.build_plan(many, context(directory, cardMatches=both), {"title": "기타지급수수료_9월 서버"})
        assert multi["issues"] == [] and len(multi["rows"]) == 2
        assert all(item["approval"] in multi["body"]["notes"][-1] for item in many)
        try:
            tax.build_plan(many + many[:1], context(directory), {})
        except ExpenseError:
            pass
        else:
            raise AssertionError("같은 세금계산서 중복을 통과시켰습니다")

        # 받는 회사가 다르거나, 회계일이 다른 해이면 차단. 같은 해의 다른 날이면 참고
        stranger = [{**items[0], "buyer": "다른회사"}]
        assert any("공급받는자" in issue for issue in tax.build_plan(stranger, context(directory, cardMatches=matched), {})["issues"])
        assert any("연도" in issue for issue in tax.build_plan(items, context(directory, cardMatches=matched), {"accountingDate": "2027-01-04"})["issues"])
        later = tax.build_plan(items, context(directory, cardMatches=matched), {"accountingDate": "2026-10-31"})
        assert later["issues"] == [] and any("반려" in warning for warning in later["warnings"])

        # 계산서는 계산서 증빙구분, 세액을 합치는 용도는 막는다
        exempt = invoices(directory, type="계산서", vat=0, supply=65769)
        assert tax.build_plan(exempt, context(directory, cardMatches=matched), {})["rows"][0]["attrCd"] == "2"
        assert any("세액" in issue for issue in tax.build_plan(items, context(directory, cardMatches=matched), {"purpose": "0999"})["issues"])
    print("검토안: 인식용·이체 결제·지급요청·처음 보는 거래처·여러 장·중복·회사/연도/용도 검사 통과")


TEMPLATE = ('<p>지출내역</p><table mapping_key="table2"><tr><td>순번</td></tr><tr><td>프로젝트</td></tr><tr><td>은행</td></tr>'
            '<tr><td><input mapping_key="detailSeq" value="순번"><input mapping_key="rmkDc" value="내용"></td></tr>'
            '<tr><td><input mapping_key="trNm" value="거래처"><input mapping_key="sumAm" value="합계"></td></tr>'
            '<tr><td><input mapping_key="bankNm" value="은행"></td></tr></table>'
            '<input mapping_key="totSumAm" value="총">'
            '<table><tr><td><p>MYSC 출금계좌</p></td><td><p><span>개별계좌 사업은 계좌번호 끝자리 기재해주세요</span></p>'
            '<p><span>ex. 국민 온드림 236243</span></p></td></tr>'
            '<tr><td><p>비고</p></td><td><p><span>경영기획팀 전달사항, 첨부파일 비밀번호 등</span></p></td></tr></table>')


def initial(rows):
    binding = {"ITEMS": {"totSumAm": "131,538", "moveLink": {"type": "hyperlink", "href": "/#/A/x"}},
               "TABLE": {"table2": {"group": [{"items": {"detailSeq": str(index), "rmkDc": row["rmkDc"], "trNm": row["trNm"],
                                                          "sumAm": f"{row['sumAm']:,}", "bankNm": "국민"}}
                                              for index, row in enumerate(rows, 1)]}}}
    return {"mainhtml": TEMPLATE, "html": "<h1>_DF10_</h1>_DF01__DF02__DF03__DF04_<div>_DF11_</div>",
            "resultMap": {"outProcessForm": {"outBindData": json.dumps(binding)}}}


def check_render():
    with tempfile.TemporaryDirectory() as directory:
        many = invoices(directory, count=2)
        plan = tax.build_plan(many, context(directory, cardMatches={i["approval"]: [{"date": "20261002"}] for i in many}),
                              {"title": "제목 <확인>", "memo": ["첫 줄 & 사용료", "둘째"]})
        contents = tax.render(initial(plan["rows"]), {"deptName": "부서", "empName": "테스트"}, plan, "2026-10-12")
        assert contents.count('mapping_key="rmkDc"') == 2 and "첫 줄 &amp; 사용료" in contents and "제목 &lt;확인&gt;" in contents
        assert "개별계좌 사업은" not in contents and "경영기획팀 전달사항" not in contents
        assert all(app.html.escape(line) in contents for line in plan["body"]["notes"])
        assert "<input" not in contents and "_DF" not in contents
        withdrawn = copy.deepcopy(plan)
        withdrawn["body"]["withdraw"] = ["MYSC 운영비 계좌"]
        assert "MYSC 운영비 계좌</span>" in tax.render(initial(plan["rows"]), {"deptName": "부서", "empName": "테스트"}, withdrawn, "2026-10-12")
        for broken in (TEMPLATE.replace("경영기획팀 전달사항", "바뀐 안내"), TEMPLATE.replace('<tr><td>은행</td></tr>', '')):
            changed = initial(plan["rows"])
            changed["mainhtml"] = broken
            try:
                tax.render(changed, {"deptName": "부서", "empName": "테스트"}, plan, "2026-10-12")
            except ExpenseError:
                continue
            raise AssertionError("바뀐 양식을 통과시켰습니다")
    print("본문: 여러 행 반복·출금계좌/비고 칸 채우기·HTML 이스케이프·양식 변경 차단 통과")


class FakeClient:
    """쓰기 호출을 기록하고, 저장된 결과를 1210 재조회 형태로 돌려준다."""
    def __init__(self, plan, lose=None):
        self.plan, self.lose, self.writes, self.saved = plan, lose, [], False
        self.erp = {**ACCOUNT}
        self.uc = {"compSeq": "1", "empSeq": "2", "groupSeq": "g", "deptName": "부서", "empName": "테스트",
                   "deptSeq": "3", "bizSeq": "4", "compName": "회사"}
        self.last_response = None

    def post(self, path, params, *, pdf=None, binary=False):
        if path in ("/ecm/ecm001A01", API + "0ap01002", API + "0ap01001", "/eap/eap110A06"):
            self.writes.append(path)
            if self.lose == path:
                raise ExpenseError("응답 유실")
        if path == "/ecm/ecm001A01":
            return {"list": [{"fileId": "F" + Path(pdf).stem}]}
        if path == API + "0ap01002":
            return {"linkKey": "L1", "approKey": params["approKey"]}
        if path == API + "0ap01001":
            return None
        if path == API + "0ap00020":
            return [{"approKey": self.journal_appro, "docId": "77" if self.saved else None, "empCd": "E1"}]
        if path == "/eap/eap110A06":
            self.saved = True
            return {"result": "77"}
        if path == "/ecm/ecm001A04":
            index = int(params["fileSn"][0])
            return {"list": [{"fileId": "F" + Path(self.plan["attachments"][index]["path"]).stem}]}
        if path == "/ecm/ecm001A03":
            index = int(params["fileSn"][0])
            return Path(self.plan["attachments"][index]["path"]).read_bytes()
        raise AssertionError("예상하지 못한 호출 " + path)

    def document(self, link_key):
        rows = [{**row, "lnSq": str(index), "attrNm": invoice["type"], "decryptBaNb": self.plan["accountDigits"]}
                for index, (row, invoice) in enumerate(zip(self.plan["rows"], self.plan["invoices"]), 1)]
        return {"head": {**self.plan["head"], "approState": "5"}, "detail": rows}


def check_resume():
    with tempfile.TemporaryDirectory() as directory:
        items = invoices(directory)
        plan = tax.build_plan(items, context(directory, cardMatches={items[0]["approval"]: [{"date": "20261002"}]}), {})
        notes = "".join(app.html.escape(line) for line in plan["body"]["notes"])

        def eap_init(client, form_id, appro_key="", doc_id=0):
            assert str(form_id) == "1210"
            if not appro_key:
                return {"resultMap": {"appdocinfo": [], "docnumtype": [{"cd_val": "1"}]}}
            if not doc_id:
                return initial(plan["rows"])
            return {"resultMap": {"appdocinfo": [{"doc_sts": "10", "approkey": appro_key}],
                                  "appdoccontent": [{"doc_contents": notes}],
                                  "fileAttachInfo": [{"fileKey": index, "fileSeq": index} for index in range(len(plan["attachments"]))]}}

        originals = (tax.validate, app.eap_init, app.eap_draft_payload)
        tax.validate = lambda client, plan: None
        app.eap_init = eap_init
        app.eap_draft_payload = lambda *args, **kwargs: {"form": kwargs["form_id"]}
        try:
            # 정상 저장: 쓰기 순서와 재실행 시 무쓰기
            path = Path(directory) / "journal.json"
            journal = {"status": "prepared", "approKey": "ERP_TEST", "plan": plan}
            write_private(path, journal)
            client = FakeClient(plan)
            client.journal_appro = "ERP_TEST"
            tax.continue_draft(client, journal, path)
            assert client.writes == ["/ecm/ecm001A01", API + "0ap01002", API + "0ap01001", "/eap/eap110A06"]
            assert journal["status"] == "verified" and journal["docId"] == 77 and read_private(path)["status"] == "verified"
            before = list(client.writes)
            tax.continue_draft(client, journal, path)
            assert client.writes == before

            # 결과가 불명확한 단계는 재전송하지 않는다
            for state in tax.UNCLEAR:
                stuck = {"status": state, "approKey": "ERP_TEST", "plan": plan, "linkKey": "L1"}
                client = FakeClient(plan)
                try:
                    tax.continue_draft(client, stuck, Path(directory) / f"{state}.json")
                except ExpenseError:
                    pass
                else:
                    raise AssertionError(state + " 단계에서 재전송했습니다")
                assert client.writes == []

            # 전자결재 저장 응답이 유실돼도 서버 상태로 확인하고 다시 쓰지 않는다
            lost = {"status": "prepared", "approKey": "ERP_TEST", "plan": plan}
            client = FakeClient(plan, lose="/eap/eap110A06")
            client.journal_appro = "ERP_TEST"
            client.saved = False
            original_post = client.post

            def post(path, params, **kwargs):
                if path == "/eap/eap110A06":
                    client.saved = True
                return original_post(path, params, **kwargs)
            client.post = post
            lost_path = Path(directory) / "lost.json"
            write_private(lost_path, lost)
            tax.continue_draft(client, lost, lost_path)
            assert lost["status"] == "verified" and client.writes.count("/eap/eap110A06") == 1

            # 다른 사용자의 기록은 처리하지 않는다
            client = FakeClient(plan)
            client.erp["userCode"] = "E2"
            try:
                tax.continue_draft(client, {"status": "prepared", "approKey": "x", "plan": plan}, path)
            except ExpenseError:
                pass
            else:
                raise AssertionError("다른 사용자 기록을 처리했습니다")
        finally:
            tax.validate, app.eap_init, app.eap_draft_payload = originals
    print("저장: 단계 순서·재실행 무쓰기·불명확 단계 재전송 차단·응답 유실 복구·사용자 확인 통과")


def check_layout_compat():
    """양식 1211 렌더링 호출부가 기본 layout으로 그대로 동작하는지 (회귀)."""
    try:
        app.render_native({"resultMap": {"outProcessForm": {"outBindData": json.dumps({"ITEMS": {}, "TABLE": {"table2": {"group": []}}})}},
                           "mainhtml": TEMPLATE, "html": "_DF11_"}, {"deptName": "", "empName": ""}, "t", "d")
    except ExpenseError as error:
        assert "행 서식" in str(error)
    else:
        raise AssertionError("1211 기본 layout이 1210 서식을 통과시켰습니다")
    print("호환: 1211 기본 서식 검사 유지 통과")


if __name__ == "__main__":
    check_parse()
    check_plan()
    check_render()
    check_resume()
    check_layout_compat()
