# /// script
# requires-python = ">=3.10"
# dependencies = ["pypdfium2>=4.30", "pillow>=10", "pillow-heif>=0.18", "xlrd>=2"]
# ///
"""실행: python3 test_expertfee.py  (네트워크 없음, 가상 서류·가상 그룹웨어 값)"""
import json
from pathlib import Path
import tempfile

import expertfee as ef
from expense import ExpenseError


def check_withholding():
    # 실제 결의 금액으로 맞춘다: 5019(기타 600,000), 중서원 심사비(기타 300,000), 4940(사업 300,000), 규제간담회(기타 3,500,000)
    cases = [("other", 600000, 52800, 547200), ("other", 300000, 26400, 273600), ("business", 300000, 9900, 290100),
             ("other", 3500000, 308000, 3192000), ("other", 125000, 0, 125000), ("other", 125010, 11000, 114010),
             ("business", 33000, 0, 33000), ("business", 34000, 1120, 32880)]
    for kind, gross, tax, net in cases:
        result = ef.withholding(kind, gross)
        assert (result["tax"], result["net"]) == (tax, net), (kind, gross, result)
    assert "과세최저한" in ef.withholding("other", 125000)["note"]
    assert "소액부징수" in ef.withholding("business", 33000)["note"]
    print("세액: 기타 8.8%·사업 3.3%·과세최저한·소액부징수·10원 절사 (실제 결의 금액 일치) 통과")


def check_confirmation():
    text = """비용지급확인서
                             □ 사업소득(원천징수공제율 3.3%)
                             ◼️ 기타소득(원천징수공제율 8.8%)
    성명   소속            비용          주민등록번호        은행명/계좌번호
           테스트회사                    900101 1234567    하나은행 877 910050 57207
    홍길동  대표  / 600,000 원"""
    parsed = ef.parse_confirmation(text)
    assert parsed["incomeType"] == "other" and 600000 in parsed["amounts"]
    assert "87791005057207" in parsed["digits"] and "900101" in parsed["digits"]
    assert ef.parse_confirmation("") is None
    assert ef.parse_confirmation("■ 사업소득 ■ 기타소득")["incomeType"] is None
    print("확인서: 체크된 소득유형·금액·계좌 숫자 읽기, 둘 다 체크면 미정 통과")


TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffc0000b080002000301011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffda0008010100003f00d2cf20ffd9")


def check_jpeg_pdf(folder):
    source = folder / "id.jpg"
    source.write_bytes(TINY_JPEG)
    pdf = ef.jpeg_to_pdf(source, folder / "id.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-1.4") and b"/Width 3 /Height 2" in pdf and TINY_JPEG in pdf and pdf.rstrip().endswith(b"%%EOF")
    try:
        ef.jpeg_to_pdf(folder / "id.pdf", folder / "x.pdf")
    except ExpenseError:
        pass
    else:
        raise AssertionError("PDF를 JPEG로 받아들였습니다")
    print("첨부: 신분증 사진(JPEG)을 한 장짜리 PDF로 감싸기 통과")


def spec_for(folder, **overrides):
    (folder / "bank.pdf").write_bytes(b"%PDF-1.4 bankbook")
    (folder / "id.jpg").write_bytes(TINY_JPEG)
    (folder / "confirm.pdf").write_bytes(b"%PDF-1.4 confirm")
    payee = {"name": "홍길동", "incomeType": "other", "gross": 600000, "bank": "하나", "account": "877-910050-57207",
             "accountSource": "bankbook", "holder": "홍길동", "rrnFront": "900101",
             "documents": {"confirmation": str(folder / "confirm.pdf"), "idCard": str(folder / "id.jpg"), "bankbook": str(folder / "bank.pdf")}}
    spec = {"program": "2026테스트사업", "purpose": "1:1 자문비", "memoTemplate": "{program}_{purpose}({name})",
            "project": "PP0001", "accountingDate": "20261020", "funding": "operating", "payees": [payee],
            "memoConfirmed": True, "projectConfirmed": True, "titleConfirmed": True}
    spec.update(overrides)
    return spec


def check_plan(folder):
    ef.read_confirmation = lambda path: None   # 테스트 PDF에는 글자 층이 없다
    plan = ef.build_plan(spec_for(folder), folder)
    assert plan["head"]["title"] == "2026테스트사업_1:1 자문비(홍길동)"
    assert [a["name"] for a in plan["attachments"]] == ["1. 비용지급확인서(홍길동)", "2. 신분증 사본(홍길동)", "3. 통장 사본(홍길동)"]
    assert all(Path(a["path"]).name == a["name"] + ".pdf" and Path(a["path"]).read_bytes().startswith(b"%PDF") for a in plan["attachments"])
    assert plan["transfer"]["required"] is False and "운영비" in plan["body"]["notes"][0]
    assert plan["payments"][0]["account"] == "87791005057207" and plan["totals"]["tax"] == 52800

    two = spec_for(folder, funding="project", projectAccount="신한 26 테스트 130549 계좌")
    second = dict(two["payees"][0], name="김철수", holder="김철수", incomeType="business", gross=300000)
    two["payees"].append(second)
    plan = ef.build_plan(two, folder)
    assert plan["head"]["title"] == "2026테스트사업_1:1 자문비(홍길동 외 1인)"
    assert [r["amount"] for r in plan["transfer"]["rows"]] == [52800, 9900] and plan["transfer"]["total"] == 62700
    assert plan["transfer"]["rows"][1]["memo"] == "자금이체_신한 26 테스트 130549 계좌 -> 국민 운영비 계좌_1:1 자문비 원천세 이체(김철수)"
    assert plan["transfer"]["title"].endswith("원천세 이체(홍길동, 김철수)")
    assert [a["name"][:3] for a in plan["attachments"]] == ["1. ", "2. ", "3. ", "4. ", "5. ", "6. "]

    bad = spec_for(folder)
    bad["payees"][0].update(holder="다른사람")
    del bad["payees"][0]["documents"]["idCard"]
    issues = ef.build_plan(bad, folder)["issues"]
    assert any("예금주" in i for i in issues) and any("신분증 사본 첨부 필요" in i for i in issues)
    # 4446처럼 영문 예금주: 확인하면 통과, 거래처·예금주는 통장 이름, 적요는 한글 이름
    english = spec_for(folder)
    english["payees"][0].update(holder="HONG GILDONG", holderConfirmed=True, purpose="발표 심사비")
    plan = ef.build_plan(english, folder)
    assert not plan["issues"] and plan["payments"][0]["memo"] == "2026테스트사업_발표 심사비(홍길동)"
    leaked = spec_for(folder)
    leaked["payees"][0]["rrn"] = "900101-1234567"
    try:
        ef.build_plan(leaked, folder)
    except ExpenseError as error:
        assert "앞 6자리" in str(error)
    else:
        raise AssertionError("주민번호 전체를 받아들였습니다")
    # 계좌는 통장 사본 기준: 출처 없음·자릿수 이상·확인서(손글씨)와 불일치는 막고, 확인하면 통과
    nosource = spec_for(folder)
    nosource["payees"][0].pop("accountSource")
    assert any("통장 사본에서 읽어" in i for i in ef.build_plan(nosource, folder)["issues"])
    short = spec_for(folder)
    short["payees"][0]["account"] = "877-9100"
    assert any("7자리" in i for i in ef.build_plan(short, folder)["issues"])
    ef.read_confirmation = lambda path: {"incomeType": "other", "amounts": [600000], "digits": "8779100505720", "birthFronts": []}
    handwritten = spec_for(folder)
    assert any("통장 사본(877-910050-57207)과 다릅니다" in i for i in ef.build_plan(handwritten, folder)["issues"])
    handwritten["payees"][0]["accountConfirmed"] = True
    confirmed = ef.build_plan(handwritten, folder)
    assert not confirmed["issues"] and any("통장 사본 기준으로 올립니다" in w for w in confirmed["warnings"])
    assert ef.summary(confirmed)["행"][0]["수취(통장 사본 기준)"] == "하나 877-910050-57207 홍길동"
    ef.read_confirmation = lambda path: None
    # 적요·프로젝트는 사람 확인 전에는 저장 불가
    unconfirmed = spec_for(folder, memoConfirmed=False, projectConfirmed=False)
    gates = ef.build_plan(unconfirmed, folder)["issues"]
    assert any("memoConfirmed" in i for i in gates) and any("projectConfirmed" in i for i in gates)
    print("계좌·확인: 통장 사본 기준·손글씨 계좌 불일치·자릿수, 적요·프로젝트 사람 확인 관문 통과")
    print("검토안: 제목·적요·첨부 이름, 운영비 지급(1212 없음)·사업계좌 지급(1212 원천세), 예금주·서류 누락·주민번호 전체 차단 통과")


def check_erp_rows(folder):
    plan = ef.build_plan(spec_for(folder), folder)
    context = {"today": "20261020", "account": {"companyCode": "1000", "userCode": "E1", "userName": "테스트", "deptCode": "D1", "deptName": "팀"},
               "division": {"divCd": "1000", "divNm": "테스트회사"}, "form": {"formId": "1210"}, "gisu": {"gisu": 2, "frDt": "20260401"},
               "dateRules": [], "cashTypes": [{"typeCd": "0300"}, {"typeCd": "0290"}],
               "projects": [{"pjtCd": "PP0001", "pjtNm": "A_2026테스트사업"}], "documents": [],
               "banks": [{"bankCd": "810", "bankNm": "KEB하나"}, {"bankCd": "040", "bankNm": "국민"}]}
    ef.ex.row_issues = lambda row, snapshot: []
    plan = ef.erp_plan(plan, context)
    row = plan["rows"][0]
    assert (row["cashCd"], row["attrCd"], row["bankCd"], row["baNb"], row["trNm"], row["pjtCd"]) == ("0300", "9", "810", "87791005057207", "홍길동", "PP0001")
    assert (row["sumAm"], row["supAm"], row["vatAm"], row["issDt"]) == (600000, 600000, 0, "20261020")
    assert plan["head"]["docTitle"] == plan["head"]["title"] and not plan["issues"]
    mixed = ef.build_plan(spec_for(folder), folder)
    mixed["payments"][0].update(holder="HONG GILDONG")
    assert ef.erp_plan(mixed, context)["rows"][0]["trNm"] == "HONG GILDONG"
    assert ef.bank_code(context["banks"], "하나은행")["bankCd"] == "810"
    assert ef.bank_code(context["banks"], "없는은행") is None
    print("ERP 행: 0300·증빙 9(기타)·KEB하나 810·거래처=사람·부가세 0 (실제 임시보관 128780 재조회 값과 같은 모양) 통과")


def check_scan(folder):
    """흩어진 폴더: 사람 폴더 안 / 다운로드에 섞인 파일 / 이름 규칙이 제각각 / 누구 것인지 모르는 사진."""
    root = folder / "scan"
    files = {
        "자문비_10월/김승현/비용지급확인서.pdf": b"%PDF-1.4", "자문비_10월/김승현/신분증.jpg": TINY_JPEG,
        "다운로드/통장사본_김승현.pdf": b"%PDF-1.4", "다운로드/김현준_지급확인서.pdf": b"%PDF-1.4",
        "다운로드/김현준 통장.png": b"png", "다운로드/정재혁(신분증).pdf": b"%PDF-1.4",
        "다운로드/IMG_1234.jpg": TINY_JPEG, "다운로드/이력서_김현준.pdf": b"%PDF-1.4",
        "다운로드/김현준_통장(새계좌).pdf": b"%PDF-1.4", "다운로드/.DS_Store": b"x",
    }
    for name, raw in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(raw)
    ef.read_confirmation = lambda path: {"incomeType": "business", "amounts": [400000], "digits": "", "birthFronts": ["900101"]}
    result = ef.scan_folders([root / "자문비_10월", root / "다운로드"])
    people = {p["name"]: p for p in result["people"]}
    assert set(people) == {"김승현", "김현준", "정재혁"}
    assert set(people["김승현"]["documents"]) == {"confirmation", "idCard", "bankbook"} and not people["김승현"]["missing"]
    assert set(people["김현준"]["documents"]) == {"confirmation", "bankbook", "resume"} and people["김현준"]["missing"] == ["신분증 사본"]
    assert [e["kind"] for e in people["김현준"]["extra"]] == ["bankbook"]          # 통장이 두 개면 고르지 않고 남긴다
    assert people["정재혁"]["missing"] == ["비용지급확인서", "통장 사본"]
    assert [Path(u["path"]).name for u in result["unassigned"]] == ["IMG_1234.jpg"]
    assert result["fileCount"] == 9
    spec = ef.init_spec(result, {"program": "2026핀테크", "purpose": "서류 심사비"})
    first = next(p for p in spec["payees"] if p["name"] == "김승현")
    assert (first["incomeType"], first["gross"], first["rrnFront"], first["account"]) == ("business", 400000, "900101", None)
    assert spec["program"] == "2026핀테크" and spec["project"] is None
    table = ef.scan_table(result)
    assert table[0].startswith("이름 |") and "김현준 | ✓ | ✗ | ✓ | 400,000 | 사업소득" in table
    print("묶기: 흩어진 폴더·제각각 파일명·폴더 이름·중복 통장·주인 모를 사진 분리, spec 뼈대 미리 채우기 통과")


def check_real_world(folder):
    """2026-10-20 실전 테스트(문서 128783)에서 나온 경우: 합본 서류·영문 파일명·법인·확인서 없음·행별 프로젝트·사람별 적요."""
    root = folder / "real"
    root.mkdir()
    for name in ("Minjung Kal_CV_Business Manager.pdf", "신분증, 통장사본_갈민정.pdf", "마이오렌지_견적서_MYSC.pdf",
                 "마이오렌지_통장사본_하나은행.pdf", "마이오렌지_사업자등록증.pdf"):
        (root / name).write_bytes(b"%PDF-1.4")
    ef.read_confirmation = lambda path: None
    result = ef.scan_folders([root], ["갈민정", "마이오렌지"], {"Minjung Kal": "갈민정"})
    people = {p["name"]: p for p in result["people"]}
    assert not result["unassigned"]
    assert set(people["갈민정"]["documents"]) == {"idCard", "bankbook", "resume"}
    assert people["갈민정"]["documents"]["idCard"] == people["갈민정"]["documents"]["bankbook"]
    assert set(people["마이오렌지"]["documents"]) == {"bankbook", "quote", "businessReg"}

    spec = spec_for(folder)
    person = spec["payees"][0]
    person["documents"]["bankbook"] = person["documents"]["idCard"]          # 신분증+통장 합본
    person.update(memo="26경기사경_10월 오피스아워(홍길동)", project="PP0002")
    plan = ef.build_plan(spec, folder)
    assert [a["name"] for a in plan["attachments"]] == ["1. 비용지급확인서(홍길동)", "2. 신분증, 통장 사본(홍길동)"]
    assert plan["payments"][0]["memo"] == "26경기사경_10월 오피스아워(홍길동)" and plan["payments"][0]["project"] == "PP0002"

    company = spec_for(folder)
    company["payees"][0].update(name="마이오렌지", holder="마이오렌지 주식회사", holderConfirmed=True, incomeType="business")
    del company["payees"][0]["documents"]["confirmation"]
    issues = ef.build_plan(company, folder)["issues"]
    assert any("원천징수 대상이 아닙니다" in i for i in issues) and any("비용지급확인서 첨부 필요" in i for i in issues)
    company["payees"][0].update(corporateOverrideForTest=True, documentsWaived=["confirmation"])
    allowed = ef.build_plan(company, folder)
    assert not allowed["issues"] and any("테스트 전용" in w for w in allowed["warnings"])
    print("실전: 합본 서류·영문 파일명(alias)·견적서/사업자등록증·법인 차단(테스트 예외)·확인서 생략 확인·행별 프로젝트·사람별 적요 통과")


def check_titles(folder):
    """제목은 실제 사례를 그대로 재현해야 한다: 5019(한 명=적요), 4446(공통 용도·외 N인), 1509(총N인), 4112(1212)."""
    one = {"program": "2026인베스트경기", "purpose": "1:1 자문비", "memoTemplate": "{program}_{purpose}({name})"}
    assert ef.title_for(one, [{"name": "안동옥"}]) == "2026인베스트경기_1:1 자문비(안동옥)"
    fintech = {"program": "2026핀테크", "purpose": "해외 데모데이(Singapore) 심사비", "memoTemplate": "{program}_{purpose}({name})",
               "projectAccount": "국민 2026핀테크 236243"}
    people = [{"name": n, "purpose": f"해외 데모데이(Singapore) {k} 심사비"} for n, k in
              (("김승현", "서류"), ("김현준", "서류"), ("정재혁", "서류"), ("김소중", "발표"), ("홍준기", "발표"))]
    assert ef.title_for(fintech, people) == "2026핀테크_해외 데모데이(Singapore) 심사비(김승현 외 4인)"
    assert ef.memo_for(fintech, people[3]) == "2026핀테크_해외 데모데이(Singapore) 발표 심사비(김소중)"
    assert ef.transfer_title(fintech, [p["name"] for p in people], "해외 데모데이(Singapore) 심사비") == (
        "자금이체_국민 2026핀테크 236243 -> 국민 운영비 계좌_해외 데모데이(Singapore) 심사비 원천세 이체(김승현, 김현준, 정재혁, 김소중, 홍준기)")
    old = {"program": "25경기 사경 창도패", "purpose": "대면심사 심사비", "titleStyle": "total"}
    assert ef.title_for(old, [{"name": n} for n in "가나다라"]) == "25경기 사경 창도패_대면심사 심사비_(총4인)"
    assert ef.common_purpose(["오피스아워", "연사비"]) is None
    # 제목 확인 관문·사업이 섞인 문서 차단
    unconfirmed = spec_for(folder, titleConfirmed=False)
    assert any("titleConfirmed" in i and "제목 '" in i for i in ef.build_plan(unconfirmed, folder)["issues"])
    mixed = spec_for(folder)
    other = dict(mixed["payees"][0], name="김철수", holder="김철수", project="PP0002")
    mixed["payees"].append(other)
    assert any("사업별로 문서를 나누세요" in i for i in ef.build_plan(mixed, folder)["issues"])
    print("제목: 5019·4446·1509·4112 실제 제목 재현, 공통 용도, 제목 확인 관문, 사업 섞임 차단 통과")


def check_transfer(folder):
    """1212 원천세 이체(실제 4112·5649 규칙, 테스트 임시보관 128786 재조회 값): 사람마다 한 행, 0360 보통예금,
    증빙 기타, 수취 = 국민 운영비 계좌 (주)엠와이소셜컴퍼니, 첨부 = 0. 원천세 계산 + N-1. 비용지급확인서."""
    spec = spec_for(folder, funding="project", projectAccount="국민 2026핀테크 236243", transferConfirmed=False)
    second = dict(spec["payees"][0], name="김철수", holder="김철수", incomeType="business", gross=400000)
    third = dict(spec["payees"][0], name="박영희", holder="박영희", gross=120000)        # 과세최저한 → 원천세 0원, 1212 행 없음
    spec["payees"] += [second, third]
    plan = ef.build_plan(spec, folder)
    sub = plan["transfer"]["plan"]
    assert any("transferConfirmed" in i for i in sub["issues"])
    spec["transferConfirmed"] = True
    sub = ef.build_plan(spec, folder)["transfer"]["plan"]
    assert not sub["issues"] and [p["name"] for p in sub["payments"]] == ["홍길동", "김철수"]
    assert [p["amount"] for p in sub["payments"]] == [52800, 13200]
    assert [a["name"] for a in sub["attachments"]] == ["0. 원천세 계산", "1-1. 비용지급확인서(홍길동)", "2-1. 비용지급확인서(김철수)"]
    assert Path(sub["attachments"][0]["path"]).read_bytes().startswith(b"%PDF-")
    assert sub["head"]["title"] == "자금이체_국민 2026핀테크 236243 -> 국민 운영비 계좌_1:1 자문비 원천세 이체(홍길동, 김철수)"
    assert sub["body"]["withdraw"] == ["국민 2026핀테크 236243"]
    context = {"today": "20261020", "account": {"companyCode": "1000", "userCode": "E1", "userName": "테스트", "deptCode": "D1", "deptName": "팀"},
               "division": {"divCd": "1000", "divNm": "테스트회사"}, "transferForm": {"formId": "1212"}, "gisu": {"gisu": 2},
               "dateRules": [], "transferCashTypes": [{"typeCd": "0360"}], "projects": [{"pjtCd": "PP0001", "pjtNm": "A_테스트"}]}
    ef.ex.row_issues = lambda row, snapshot: []
    sub = ef.erp_transfer(sub, context)
    row = sub["rows"][0]
    assert (row["cashCd"], row["attrCd"], row["trNm"], row["bankCd"], row["baNb"], row["depositorDc"], row["formId"]) == (
        "0360", "9", "", "040", "80173704008641", "(주)엠와이소셜컴퍼니", 1212)
    assert (row["sumAm"], row["vatAm"]) == (52800, 0) and sub["head"]["docTitle"] == sub["head"]["title"]
    operating = ef.build_plan(spec_for(folder), folder)
    assert operating["transfer"]["plan"] is None and operating["transfer"]["required"] is False
    print("1212: 확인 관문·원천세 있는 사람만·첨부(계산표+확인서)·제목·0360/기타/운영비 계좌 행·운영비 지급이면 없음 통과")


if __name__ == "__main__":
    check_withholding()
    check_confirmation()
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        check_jpeg_pdf(folder)
        check_plan(folder)
        check_erp_rows(folder)
        check_scan(folder)
        check_real_world(folder)
        check_titles(folder)
        check_transfer(folder)
