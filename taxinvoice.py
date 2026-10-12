#!/usr/bin/env python3
"""MYSC 세금계산서·계산서 지출결의(양식 1210) CLI. Python 표준 라이브러리만 사용한다.

- 인식용(recognition): [영수] 증빙. 법인카드·이체로 이미 결제했고 회계 인식만 필요한 건.
- 지급요청(payment): [청구] 증빙. 회사가 거래처 계좌로 지급해야 하는 건.
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import html
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import urllib.parse

import expense as ex
from expense import API, CONFIG, Client, ExpenseError, date_value, read_private, won, write_private


FORM_ID = "1210"
DEFAULT_CASH = "0350"  # 1210 양식의 기타지급수수료. 1211(법인카드)은 같은 계정이 0310이다.
PAYMETHODS = {"세금계산서": "03", "계산서": "04"}  # 0ap00041 지급수단 → 증빙구분 코드
LAYOUT = (3, 3)        # 1210 반복 표: 머리 3행, 거래당 3행
DRAFTS = CONFIG / "tax-drafts"
KST = dt.timezone(dt.timedelta(hours=9))
WITHDRAW_PLACEHOLDER = "개별계좌 사업은 계좌번호 끝자리 기재해주세요"
NOTE_PLACEHOLDER = "경영기획팀 전달사항, 첨부파일 비밀번호 등"
UNCLEAR = ("pdf_upload_attempted", "link_attempted", "erp_attempted")
MODES = {"영수": "recognition", "청구": "payment"}


def compact(name):
    return re.sub(r"\s|\(주\)|㈜|\(유\)|주식회사|유한회사|유한책임회사|[.,·]", "", str(name or "")).casefold()


def same_party(left, right):
    left, right = compact(left), compact(right)
    return bool(left and right) and (left == right or left in right or right in left)


def amount(text):
    return won(str(text).replace(",", ""))


def today_kst():
    return dt.datetime.now(KST).strftime("%Y%m%d")


def party_names(text):
    """공급자·공급받는자 상호. 서식이 다르면 (None, None)을 돌려 사용자에게 묻게 한다."""
    lines = text.splitlines()
    name = next((line for line in lines if line.count("상호") == 2), "")
    first = [part.strip() for part in re.findall(r"상호(.*?)(?:성명|$)", name)]
    corp = next((line for line in lines if line.count("(법인명)") == 2), "")
    second = [part.strip() for part in re.findall(r"\(법인명\)(.*?)(?:\(대표자\)|$)", corp)]
    if len(first) != 2:
        return None, None
    second = second if len(second) == 2 else ["", ""]
    return tuple(" ".join(filter(None, (a, b))) or None for a, b in zip(first, second))


def parse_invoice(text):
    """홈택스 표준 서식의 전자(세금)계산서 텍스트. 읽지 못한 값은 추정하지 않고 오류로 알린다."""
    def need(pattern, label):
        found = re.search(pattern, text)
        if not found:
            raise ExpenseError(f"PDF에서 {label}을(를) 찾을 수 없습니다. 이 서식은 아직 자동으로 못 읽어요.")
        return found
    title = re.sub(r"\s", "", text[:2000])
    if "세금계산서" in title:
        kind = "세금계산서"
    elif "계산서" in title:
        kind = "계산서"
    else:
        raise ExpenseError("전자세금계산서·전자계산서 PDF가 아닙니다.")
    if "수정사유" in text and re.search(r"수정사유\s*[:：]?\s*\S*(환입|계약의해제|착오|공급가액변동|내국신용장)", text):
        raise ExpenseError("수정(세금)계산서는 아직 자동으로 처리하지 않습니다. 원 거래와 함께 재경팀과 확인하세요.")
    approval = need(r"승인\s*번호\s*[:：]?\s*(\d{8})-?(\d{8})-?(\d{8})", "승인번호")
    approval = "-".join(approval.groups())
    registrations = re.findall(r"등록\s*번호\s*[:：]?\s*(\d{3}-?\d{2}-?\d{5})", text)
    if len(registrations) < 2:
        raise ExpenseError("PDF에서 공급자·공급받는자 등록번호를 찾을 수 없습니다.")
    date = r"(\d{4})\s*[-.년]\s*(\d{1,2})\s*[-.월]\s*(\d{1,2})일?"
    money = r"(-?[\d,]+)"
    if kind == "세금계산서":
        line = need(date + r"\s+" + money + r"\s+" + money, "작성일자·공급가액·세액")
        supply, vat = amount(line[4]), amount(line[5])
    else:
        line = need(date + r"\s+" + money, "작성일자·공급가액")
        supply, vat = amount(line[4]), 0
    issued = date_value(f"{line[1]}{int(line[2]):02d}{int(line[3]):02d}")
    rest = text[need(r"합\s*계\s*금\s*액", "합계금액").end():]
    total = re.search(r"(?<![\d-])(-?\d{1,3}(?:,\d{3})+|-?\d+)(?![\d-])", rest)
    if not total:
        raise ExpenseError("PDF에서 합계금액을 찾을 수 없습니다.")
    total = amount(total[1])
    if min(supply, vat, total) < 0:
        raise ExpenseError("음수 금액(수정·환입) 증빙은 아직 자동으로 처리하지 않습니다.")
    if total != supply + vat:
        raise ExpenseError(f"공급가액 {supply:,} + 세액 {vat:,} ≠ 합계 {total:,}. PDF를 확인하세요.")
    if approval[:8] != issued:
        raise ExpenseError("승인번호 날짜와 작성일자가 다릅니다. PDF를 확인하세요.")
    marks = set(re.findall(r"\[\s*(영수|청구)\s*\]", text))
    supplier, buyer = party_names(text)
    return {"type": kind, "approval": approval, "issued": issued, "supply": supply, "vat": vat, "total": total,
            "supplierRegNb": registrations[0].replace("-", ""), "buyerRegNb": registrations[1].replace("-", ""),
            "supplier": supplier, "buyer": buyer, "receipt": marks.pop() if len(marks) == 1 else ""}


def read_invoice(path):
    path = Path(path).expanduser().resolve()
    raw = path.read_bytes()
    if not raw.startswith(b"%PDF-"):
        raise ExpenseError(f"PDF 파일이 아닙니다: {path.name}")
    try:
        text = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True,
                              check=True).stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        raise ExpenseError("PDF를 읽으려면 pdftotext가 필요합니다. (brew install poppler)") from None
    if not text.strip():
        raise ExpenseError(f"글자를 읽을 수 없는 PDF입니다(스캔본 등): {path.name}")
    try:
        parsed = parse_invoice(text)
    except ExpenseError as error:
        raise ExpenseError(f"{path.name}: {error}") from None
    return {**parsed, "file": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def form_record(client, form_id):
    forms = client.post("/eap/eap096A45", {
        "header": {"pId": "", "groupSeq": client.uc["groupSeq"], "empSeq": client.uc["empSeq"]},
        "body": {"companyInfo": {"compSeq": client.uc["compSeq"]}, "formDTp": "",
                 "searchFormDTp": "APB1020", "langCode": client.uc["langCode"]}})
    forms = [form for form in forms if str(form["formId"]) == str(form_id) and form["useYn"] in ("1", "Y")]
    if len(forms) != 1:
        raise ExpenseError("사용 가능한 세금계산서 지출결의 양식(1210)을 확인할 수 없습니다.")
    return forms[0]


def tax_documents(client, since, until):
    """본인 1210 문서(삭제 제외)와 행. 중복 확인과 이전 입력값 제안에 쓴다."""
    documents = client.post(API + "0ap00001", {
        "appDateFrom": since, "appDateTo": until, "docTitle": "", "fromAm": "", "toAm": "",
        "docNo": "", "docStatus": "", "dateFg": "1"})
    return [{"document": doc, **client.document(doc["linkKey"])} for doc in documents
            if str(doc.get("formId")) == FORM_ID and str(doc.get("approState")) != "-3"]


def find_duplicates(documents, invoice):
    """승인번호를 ERP가 저장하지 않으므로 같은 증빙일·합계·거래처로 판단한다."""
    found = []
    for item in documents:
        for row in item["detail"]:
            if row.get("issDt") != invoice["issued"] or won(row.get("sumAm")) != invoice["total"]:
                continue
            if invoice.get("supplier") and row.get("trNm") and not same_party(row["trNm"], invoice["supplier"]):
                continue
            found.append({"docNo": item["document"].get("docNo") or "", "linkKey": item["document"]["linkKey"],
                          "title": item["head"].get("docTitle", ""), "state": str(item["head"].get("approState"))})
    return found


def approved_rows(documents):
    rows = []
    for item in documents:
        if str(item["head"].get("approState")) == "1":
            rows.extend({"head": item["head"], "row": row} for row in item["detail"])
    return sorted(rows, key=lambda entry: (entry["head"].get("isuDt") or "", entry["head"]["linkKey"]))


def vendor_precedent(documents, supplier):
    """같은 거래처로 승인된 가장 최근 행. 제목·적요·용도·프로젝트 제안에만 쓴다."""
    matches = [entry for entry in approved_rows(documents) if same_party(entry["row"].get("trNm"), supplier)]
    return matches[-1] if matches else None


def account_precedent(documents, mode, supplier, company):
    """수취 계좌는 직접 입력하지 않고 승인된 이전 행의 계좌를 그대로 쓴다.
    인식용은 회사 자신의 계좌, 지급요청은 같은 거래처의 계좌만 허용한다."""
    for entry in reversed(approved_rows(documents)):
        row = entry["row"]
        if not (row.get("bankCd") and row.get("baNb") and row.get("depositorDc")):
            continue
        own = same_party(row["depositorDc"], company)
        if mode == "recognition" and own:
            return entry
        if mode == "payment" and not own and same_party(row.get("trNm"), supplier):
            return entry
    return None


def card_matches(client, card_name, invoice, today):
    """작성일부터 31일 안의 미반영 카드 거래 중 합계가 같은 것."""
    start = invoice["issued"]
    end = min((dt.datetime.strptime(start, "%Y%m%d") + dt.timedelta(days=31)).strftime("%Y%m%d"), today)
    if end < start:
        return []
    return [{"date": row["issDt"], "merchant": row.get("chainName", ""), "amount": won(row["sunginAm"])}
            for row in client.cards(start, end, card_name) if won(row["sunginAm"]) == invoice["total"]]


def korean_date(value):
    return f"{int(value[4:6])}월 {int(value[6:])}일"


def default_notes(mode, items):
    numbers = ", ".join(item["invoice"]["approval"] for item in items)
    kinds = "·".join(sorted({item["invoice"]["type"] for item in items}))
    if mode == "payment":
        return [f"ㅇ {kinds} 청구 건으로, 거래처 계좌로 지급을 요청드립니다.", f"ㅇ 승인번호: {numbers}"]
    paid = sorted({item["paidDate"] for item in items if item.get("paidDate")})
    via = {item.get("paidBy") for item in items}
    method = "법인카드" if via == {"card"} else "기 지급(이체 등)" if via == {"transfer"} else "법인카드·이체"
    when = ", ".join(korean_date(day) for day in paid) or "결제일 확인 필요"
    return [f"ㅇ 해당 건은 {kinds}가 발행되었으나, '청구'가 아닌 '영수' 개념이고 이미 {method}로 지출완료된 건입니다. "
            f"({when} 결제, 결제 내역 본문 첨부)",
            "ㅇ 재경팀과의 상의를 통해, 회계장부상 인식 및 기록을 위한 목적으로 지출결의를 추가로 상신드립니다.",
            f"ㅇ 승인번호: {numbers}"]


def build_plan(invoices, context, options):
    """조회 결과와 사용자 선택으로 검토안을 만든다. 네트워크를 쓰지 않는다."""
    account, company = context["account"], context["division"]["divNm"]
    issues, warnings, items = [], [], []
    if len({invoice["approval"] for invoice in invoices}) != len(invoices):
        raise ExpenseError("같은 세금계산서를 두 번 넣었습니다.")
    receipts = {invoice["receipt"] for invoice in invoices}
    mode = options.get("mode") or (MODES.get(receipts.pop()) if len(receipts) == 1 else None)
    if mode not in ("recognition", "payment"):
        issues.append("[영수]/[청구] 표시를 하나로 확인할 수 없습니다. --mode recognition(이미 결제) 또는 payment(지급요청)를 지정하세요.")
        mode = mode or "recognition"
    for invoice in invoices:
        if invoice["receipt"] and MODES[invoice["receipt"]] != mode:
            warnings.append(f"{invoice['approval']}: PDF는 [{invoice['receipt']}]인데 {mode}로 처리합니다.")
    accounting = date_value(options.get("accountingDate") or max(invoice["issued"] for invoice in invoices))
    pay = date_value(options.get("payDate") or context["today"])
    vendors = {compact(options.get("vendor") or invoice.get("supplier")) for invoice in invoices}
    if mode == "payment" and len(vendors) != 1:
        issues.append("지급요청은 거래처 한 곳의 증빙만 한 문서로 올립니다. 거래처별로 나눠 주세요.")
    paid_dates = options.get("paidDates") or []
    if paid_dates and len(paid_dates) != len(invoices):
        raise ExpenseError("--paid-date는 PDF 순서대로 PDF 개수만큼 지정하세요.")
    for index, invoice in enumerate(invoices):
        label = f"{index + 1}행({invoice['approval']})"
        supplier = options.get("vendor") or invoice.get("supplier")
        if not supplier:
            issues.append(f"{label}: 공급자 상호를 PDF에서 읽지 못했습니다. --vendor로 지정하세요.")
        if not same_party(invoice.get("buyer"), company):
            issues.append(f"{label}: 공급받는자({invoice.get('buyer') or '확인 불가'})가 회사명({company})과 다릅니다.")
        for item in context["duplicates"].get(invoice["approval"], []):
            message = f"{label}: 같은 증빙일·합계·거래처의 1210 문서가 있습니다 — {item['docNo'] or '번호 없음'} (상태 {item['state']}) {item['title']}"
            (warnings if item["state"] == "4" else issues).append(message)
        if accounting[:4] != invoice["issued"][:4]:
            issues.append(f"{label}: 회계일과 증빙일의 연도가 다릅니다(그룹웨어 규칙).")
        elif accounting != invoice["issued"]:
            warnings.append(f"{label}: 회계일({accounting})이 작성일자({invoice['issued']})와 다릅니다. 증빙일≠회계일로 반려된 적이 있으니 재경팀 확인을 권합니다.")
        matches, paid, paid_by = context["cardMatches"].get(invoice["approval"], []), "", None
        if mode == "recognition":
            if paid_dates:
                paid, paid_by = date_value(paid_dates[index]), options.get("paidBy") or "card"
            elif len(matches) == 1:
                paid, paid_by = matches[0]["date"], "card"
            else:
                issues.append(f"{label}: 합계 {invoice['total']:,}원 카드 결제를 {'여러 건 찾음' if matches else '못 찾음'}. "
                              "--paid-date로 결제일을 지정하세요 (이체로 냈으면 --paid-by transfer).")
        precedent = vendor_precedent(context["documents"], supplier)
        items.append({"invoice": invoice, "supplier": supplier, "precedent": precedent,
                      "cardMatches": matches, "paidDate": paid, "paidBy": paid_by})
    first = items[0]["precedent"]
    title = (options.get("title") or (first["head"]["docTitle"] if first and len(invoices) == 1 else "")).strip()
    if not title:
        issues.append("문서 제목 확인 필요 (--title). 예: 기타지급수수료_서버 사용료")
    banking = account_precedent(context["documents"], mode, items[0]["supplier"], company)
    if not banking:
        issues.append("수취 은행·계좌를 가져올 승인된 이전 1210 문서가 없습니다. "
                      + ("회사 계좌로 받은 인식용" if mode == "recognition" else "같은 거래처 계좌로 지급한")
                      + " 문서가 한 번은 그룹웨어에서 직접 승인돼야 합니다.")
    bank = banking["row"] if banking else {}
    withdraw = options.get("withdraw") or []
    if mode == "payment" and not withdraw:
        issues.append("지급요청은 MYSC 출금계좌를 적어야 합니다 (--withdraw). 예: MYSC 운영비 계좌, 개별계좌 사업은 끝자리")
    if mode == "recognition" and withdraw:
        warnings.append("인식용은 출금계좌를 비워두라는 재경팀 의견이 있습니다.")
    head = {"coCd": account["companyCode"], "empCd": account["userCode"], "empNm": account["userName"],
            "deptCd": account["deptCode"], "deptNm": account["deptName"],
            "divCd": context["division"]["divCd"], "divNm": company,
            "gisu": context["gisu"]["gisu"], "isuDt": accounting, "docTitle": title,
            "docugroupYn": "Y", "onlyCashFg": "1", "fileGroup": "0", "fileDc": "",
            "pjtCd": "", "pjtNm": "", "consDocTitle": "", "exdocuYn": "N"}
    snapshot = {"dateRules": context["dateRules"], "accountingDate": accounting,
                "cashTypes": context["cashTypes"], "projects": context["projects"]}
    rows = []
    for index, item in enumerate(items):
        invoice, previous = item["invoice"], (item["precedent"] or {}).get("row", {})
        attr = context["attrCodes"].get(invoice["type"])
        if not attr:
            issues.append(f"{index + 1}행: {invoice['type']} 증빙구분 코드를 그룹웨어에서 확인할 수 없습니다.")
        memos = options.get("memo") or []
        if memos and len(memos) not in (1, len(items)):
            raise ExpenseError("--memo는 한 번(모든 행 공통) 또는 PDF 개수만큼 지정하세요.")
        memo = memos[index if len(memos) > 1 else 0] if memos else previous.get("rmkDc", "")
        row = {"coCd": account["companyCode"], "cardDt": "", "cardSq": 0, "sunginNb": "", "vatGroupDc": "",
               "cardCd": "", "cardNm": "", "cardTrCd": "", "cardFg": "",
               "issDt": invoice["issued"], "sumAm": invoice["total"], "vatAm": invoice["vat"], "supAm": invoice["supply"],
               "trCd": "", "trNm": item["supplier"] or "", "regNb": "",
               "empCd": account["userCode"], "empNm": account["userName"],
               "deptCd": account["deptCode"], "deptNm": account["deptName"],
               "bankCd": bank.get("bankCd") or "", "bankNm": bank.get("bankNm") or "",
               "baNb": bank.get("baNb") or "", "depositorDc": bank.get("depositorDc") or "",
               "realDepositorDc": "", "realDepositorFg": "",
               "cashCd": options.get("purpose") or previous.get("cashCd") or DEFAULT_CASH, "cashNm": "",
               "rmkDc": (memo or "").strip(), "pjtCd": options.get("project") or previous.get("pjtCd") or context.get("defaultProject", ""),
               "pjtNm": "", "carCd": "", "carNb": "", "issNo": "", "issSq": 0, "cashDt": "", "cashSq": None, "taxTy": "",
               "payDt": pay, "fileGroup": "0", "fileDc": "", "fileInfoList": [], "budgetSts": "0",
               "formId": int(FORM_ID), "linkKey": "", "attrCd": attr or ""}
        issues.extend(f"{index + 1}행: {message}" for message in ex.row_issues(row, snapshot))
        if row["vatAm"] != invoice["vat"]:
            issues.append(f"{index + 1}행: 이 용도는 세액을 공급가액에 합칩니다. 세금계산서 세액과 맞는 용도인지 확인하세요.")
        if not item["precedent"]:
            warnings.append(f"{index + 1}행: '{item['supplier']}'로 승인된 이전 문서가 없어 적요·용도·프로젝트를 새로 정해야 합니다.")
        rows.append(row)
    notes = options.get("notes") or default_notes(mode, items)
    files = [invoice["file"] for invoice in invoices] + list(options.get("attach") or [])
    attachments = []
    for path in files:
        path = Path(path).expanduser().resolve()
        raw = path.read_bytes()
        if not raw.startswith(b"%PDF-"):
            raise ExpenseError(f"PDF만 첨부할 수 있습니다: {path.name}")
        attachments.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    if len({item["sha256"] for item in attachments}) != len(attachments):
        raise ExpenseError("같은 파일을 두 번 첨부했습니다.")
    if mode == "recognition" and not options.get("attach"):
        warnings.append("결제 내역(카드 이용내역 등) PDF가 없습니다. 재경팀은 결제 증빙을 본문 첨부로 요청했습니다 (--attach).")
    return {"schema": 1, "kind": "taxinvoice", "mode": mode, "head": head, "rows": rows,
            "invoices": invoices, "matching": [{key: item[key] for key in ("cardMatches", "paidDate", "paidBy")} for item in items],
            "body": {"withdraw": withdraw, "notes": notes}, "attachments": attachments,
            "issues": issues, "warnings": warnings, "accountDigits": bank.get("decryptBaNb") or "",
            "source": {"form": context["form"], "gisu": context["gisu"],
                       "precedents": [(item["precedent"] or {}).get("head", {}).get("linkKey", "") for item in items],
                       "account": banking["head"]["linkKey"] if banking else ""}}


def gather(client, invoices, profile_path):
    today = today_kst()
    first = min(invoice["issued"] for invoice in invoices)
    since = (dt.datetime.strptime(first, "%Y%m%d") - dt.timedelta(days=366)).strftime("%Y%m%d")
    documents = tax_documents(client, since, today)
    divisions = client.post(API + "0ap00017", {"empCd": client.erp["userCode"]})
    if len(divisions) != 1:
        raise ExpenseError("회계단위가 여러 개입니다. 적용할 회계단위 확인이 필요합니다.")
    methods = client.post(API + "0ap00041", {})
    attr_codes = {}
    for kind, method in PAYMETHODS.items():
        codes = {str(item.get("attrCd") or "") for item in methods if item.get("paymethodFg") == method}
        if len(codes) == 1 and "" not in codes:
            attr_codes[kind] = codes.pop()
    profile = {}
    if profile_path and Path(profile_path).expanduser().exists():
        profile = ex.load_profile(Path(profile_path).expanduser(), client.erp)
    matches = {}
    if profile.get("cardName"):
        for invoice in invoices:
            matches[invoice["approval"]] = card_matches(client, profile["cardName"], invoice, today)
    return {"today": today, "account": client.erp, "division": divisions[0], "documents": documents,
            "form": form_record(client, FORM_ID), "attrCodes": attr_codes,
            "gisu": client.post("/system/financialApi/selectGisuData", {"frDt": max(i["issued"] for i in invoices)}),
            "dateRules": client.post("/personal/modulecommon/config/getConfig", {
                "syscfg": [{"moduleCd": "A", "ctrCd": key} for key in ("51", "60")]}),
            "cashTypes": client.post("/personal/APCodePicker/ApAcashcdTypeCode", {
                "helpTy": "ACASHCD_TYPE_CODE", "langkind": "KOR", "multiCode": [int(FORM_ID)]}),
            "projects": client.codepicker("SPJT_CODE_AUTH", empCd=client.erp["userCode"], useYn="ALL"),
            "duplicates": {invoice["approval"]: find_duplicates(documents, invoice) for invoice in invoices},
            "defaultProject": profile.get("defaultProject", ""), "cardMatches": matches}


def options_from(args):
    return {"mode": args.mode, "vendor": args.vendor, "title": args.title, "memo": args.memo,
            "purpose": args.purpose, "project": args.project, "accountingDate": args.accounting_date,
            "payDate": args.pay_date, "paidDates": args.paid_date, "paidBy": args.paid_by,
            "notes": args.note, "withdraw": args.withdraw, "attach": args.attach}


def summary(plan):
    digits = plan.get("accountDigits") or ""
    return {"방식": {"recognition": "인식용(이미 결제)", "payment": "지급요청"}[plan["mode"]],
            "제목": plan["head"]["docTitle"], "회계일": plan["head"]["isuDt"],
            "행": [{"증빙": invoice["type"], "공급자": row["trNm"], "증빙일": row["issDt"], "지급요청일": row["payDt"],
                   "공급가액": row["supAm"], "세액": row["vatAm"], "합계": row["sumAm"],
                   "용도": f"{row['cashCd']} {row.get('cashNm', '')}".strip(), "적요": row["rmkDc"],
                   "프로젝트": row.get("pjtNm") or row["pjtCd"], "승인번호": invoice["approval"],
                   "결제일": match["paidDate"], "카드후보": match["cardMatches"]}
                  for row, invoice, match in zip(plan["rows"], plan["invoices"], plan["matching"])],
            "합계": sum(row["sumAm"] for row in plan["rows"]),
            "수취": f"{plan['rows'][0]['bankNm']} …{digits[-4:]} {plan['rows'][0]['depositorDc']}".strip() if digits else "(없음)",
            "출금계좌": plan["body"]["withdraw"] or "(비움)", "비고": plan["body"]["notes"],
            "첨부": [Path(item["path"]).name for item in plan["attachments"]],
            "확인필요": plan["issues"], "참고": plan["warnings"]}


def read_command(args):
    print(json.dumps([read_invoice(path) for path in args.pdf], ensure_ascii=False, indent=2))


def prepare(args):
    client = Client()
    invoices = [read_invoice(path) for path in args.pdf]
    plan = build_plan(invoices, gather(client, invoices, args.profile), options_from(args))
    write_private(args.out, plan)
    print(json.dumps(summary(plan), ensure_ascii=False, indent=2))


def validate(client, plan):
    """저장 직전 현재 그룹웨어 상태로 다시 확인한다. 쓰기 없음."""
    if plan.get("kind") != "taxinvoice":
        raise ExpenseError("세금계산서 검토안이 아닙니다.")
    head = plan["head"]
    if str(head["coCd"]) != str(client.erp["companyCode"]) or str(head["empCd"]) != str(client.erp["userCode"]):
        raise ExpenseError("검토 파일과 현재 로그인 계정이 다릅니다.")
    if plan["issues"]:
        raise ExpenseError("확인할 항목이 남았습니다:\n" + "\n".join(plan["issues"]))
    for invoice in plan["invoices"]:
        if read_invoice(invoice["file"]) != invoice:
            raise ExpenseError("세금계산서 PDF가 검토 이후 변경됐습니다: " + Path(invoice["file"]).name)
    for item in plan["attachments"]:
        if hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ExpenseError("첨부할 PDF가 검토 이후 변경됐습니다: " + Path(item["path"]).name)
    first = min(invoice["issued"] for invoice in plan["invoices"])
    since = (dt.datetime.strptime(first, "%Y%m%d") - dt.timedelta(days=366)).strftime("%Y%m%d")
    documents = tax_documents(client, since, today_kst())
    blocking = [item for invoice in plan["invoices"] for item in find_duplicates(documents, invoice) if item["state"] != "4"]
    if blocking:
        raise ExpenseError("같은 증빙으로 보이는 1210 문서가 이미 있습니다: "
                           + ", ".join(f"{item['docNo'] or item['linkKey']}(상태 {item['state']})" for item in blocking))
    close_date = client.post(API + "0ap00050", {"divCd": head["divCd"]})
    if close_date and close_date != "00000000" and head["isuDt"] <= str(close_date):
        raise ExpenseError("회계처리일자가 지출결의 마감일 이전입니다.")
    usable = {str(item["typeCd"]) for item in client.post("/personal/APCodePicker/ApAcashcdTypeCode", {
        "helpTy": "ACASHCD_TYPE_CODE", "langkind": "KOR", "multiCode": [int(FORM_ID)]})}
    if any(row["cashCd"] not in usable for row in plan["rows"]):
        raise ExpenseError("1210 양식에서 더 이상 쓸 수 없는 용도가 있습니다.")


def check_command(args):
    plan = read_private(args.plan)
    validate(Client(), plan)
    print(f"저장 전 검증 통과: {len(plan['rows'])}건, 중복 문서 없음, PDF 변경 없음, 마감일·용도 확인")


def fill_cell(body, placeholder, lines):
    """본문 하단 표에서 안내 문구가 든 칸을 사용자 문장으로 바꾼다. 빈 목록이면 칸을 비운다."""
    cells = [cell for cell in re.finditer(r"(<td\b[^>]*>)(.*?)(</td>)", body, re.S | re.I)
             if placeholder in cell[2] and "<td" not in cell[2].lower()]
    if len(cells) != 1:
        raise ExpenseError("전자결재 본문 하단(출금계좌·비고) 서식이 변경됐습니다.")
    cell = cells[0]
    style = ("color:rgb(0, 0, 0);font-family:'맑은 고딕';font-size:10pt;"
             "margin-top:0px;margin-bottom:0px;line-height:1.2;")
    paragraphs = "".join(f'<p style="{style}"><span style="font-family:\'맑은 고딕\';">{html.escape(line)}</span></p>'
                         for line in lines) or f'<p style="{style}"><br></p>'
    return body[:cell.start()] + cell[1] + paragraphs + cell[3] + body[cell.end():]


def render(initial, uc, plan, today):
    def footer(body):
        body = fill_cell(body, WITHDRAW_PLACEHOLDER, plan["body"]["withdraw"])
        return fill_cell(body, NOTE_PLACEHOLDER, plan["body"]["notes"])
    contents = ex.render_native(initial, uc, plan["head"]["docTitle"], today, layout=LAYOUT, body_hook=footer)
    if "callComp=APB1020PCard" in contents:
        raise ExpenseError("세금계산서 본문에 카드 영수증 링크가 있습니다.")
    expected = [html.escape(row["rmkDc"]) for row in plan["rows"]] + [html.escape(line) for line in plan["body"]["notes"]]
    if any(value not in contents for value in expected):
        raise ExpenseError("전자결재 본문에 검토안 값이 반영되지 않았습니다.")
    return contents


def verify_tax(client, journal):
    plan = journal["plan"]
    saved = client.document(journal["linkKey"])
    if str(saved["head"]["approState"]) != "5" or len(saved["detail"]) != len(plan["rows"]):
        raise ExpenseError(f"ERP 상태={saved['head']['approState']}, 행 수={len(saved['detail'])}. "
                           f"임시보관 상태 5와 {len(plan['rows'])}행이 필요합니다.")
    for field in ("isuDt", "docTitle", "empCd", "deptCd", "divCd"):
        if str(saved["head"].get(field) or "") != str(plan["head"].get(field) or ""):
            raise ExpenseError("저장한 문서의 기본 정보가 검토안과 다릅니다: " + field)
    loaded_rows = sorted(saved["detail"], key=lambda row: int(row.get("lnSq") or 0))
    for loaded, row, invoice in zip(loaded_rows, plan["rows"], plan["invoices"]):
        for field in ("sumAm", "supAm", "vatAm", "issDt", "payDt", "cashCd", "rmkDc", "pjtCd", "trNm",
                      "empCd", "deptCd", "attrCd", "bankCd", "depositorDc"):
            left, right = loaded.get(field), row.get(field)
            differs = won(left) != won(right) if field in ("sumAm", "supAm", "vatAm") else str(left or "") != str(right or "")
            if differs:
                raise ExpenseError("저장 후 재조회 값이 다릅니다: " + field)
        if loaded.get("attrNm") and loaded["attrNm"] != invoice["type"]:
            raise ExpenseError(f"저장된 증빙구분({loaded['attrNm']})이 PDF({invoice['type']})와 다릅니다.")
        if plan.get("accountDigits") and str(loaded.get("decryptBaNb") or "") != plan["accountDigits"]:
            raise ExpenseError("저장 후 재조회한 수취 계좌가 이전 문서와 다릅니다.")
    eap = ex.eap_init(client, FORM_ID, journal["approKey"], journal["docId"])
    info = eap["resultMap"]["appdocinfo"]
    if len(info) != 1 or str(info[0]["doc_sts"]) != "10" or info[0]["approkey"] != journal["approKey"]:
        raise ExpenseError("전자결재 임시보관 상태를 확인할 수 없습니다.")
    content = eap["resultMap"]["appdoccontent"]
    if len(content) != 1:
        raise ExpenseError("저장된 전자결재 본문을 확인할 수 없습니다.")
    contents = content[0]["doc_contents"]
    if "%3C" in contents[:30].upper():
        contents = urllib.parse.unquote(contents)
    if any(html.escape(line) not in contents for line in plan["body"]["notes"]):
        raise ExpenseError("저장된 본문에 비고 문장이 없습니다.")
    attached = sorted(eap["resultMap"]["fileAttachInfo"], key=lambda item: item.get("fileSeq", 0))
    if len(attached) != len(plan["attachments"]):
        raise ExpenseError("전자결재 첨부 개수가 검토안과 다릅니다.")
    auth = {"compSeq": client.uc["compSeq"], "empSeq": client.uc["empSeq"], "docId": journal["docId"], "migYn": "0"}
    expected = {item["fileId"]: source for item, source in zip(journal["attachments"], plan["attachments"])}
    verified = []
    for file in attached:
        params = {"moduleGbn": "EAP", "authKeyMap": json.dumps(auth), "fileSn": [file["fileKey"]]}
        metadata = client.post("/ecm/ecm001A04", {**params, "condition": "99"})["list"]
        if len(metadata) != 1 or metadata[0]["fileId"] not in expected:
            raise ExpenseError("저장한 PDF의 파일 식별자가 다릅니다.")
        source = expected.pop(metadata[0]["fileId"])
        downloaded = client.post("/ecm/ecm001A03", {**params, "filename": Path(source["path"]).name,
                                 "groupSeq": client.uc["groupSeq"]}, binary=True)
        if hashlib.sha256(downloaded).hexdigest() != source["sha256"]:
            raise ExpenseError("저장 문서에서 다운로드한 PDF가 원본과 다릅니다: " + Path(source["path"]).name)
        verified.append({"fileKey": file["fileKey"], "sha256": source["sha256"]})
    journal["verifiedPdf"] = verified
    journal["status"] = "verified"


def recover(client, journal):
    """저장 응답 대신 실제 연결 상태를 재조회한다. 쓰기는 재시도하지 않는다."""
    if not journal.get("docId") and journal.get("linkKey"):
        linked = client.post(API + "0ap00020", {"linkKey": journal["linkKey"]})
        matches = [row for row in linked if row.get("approKey") == journal["approKey"] and row.get("docId")]
        if len(matches) == 1 and str(matches[0].get("empCd")) == str(client.erp["userCode"]):
            journal["docId"] = int(matches[0]["docId"])
    if not journal.get("docId"):
        raise ExpenseError("저장 결과를 확인할 수 없습니다. 재전송하지 않았습니다. 그룹웨어에서 확인하세요.")
    verify_tax(client, journal)


def continue_draft(client, journal, path):
    """확인된 단계는 건너뛰고, 결과가 불명확한 쓰기는 재전송하지 않는다."""
    def save(status=None):
        if status:
            journal["status"] = status
        write_private(path, journal, replace=True)
    plan = journal["plan"]
    if str(plan["head"]["coCd"]) != str(client.erp["companyCode"]) or str(plan["head"]["empCd"]) != str(client.erp["userCode"]):
        raise ExpenseError("다른 사용자 작업 기록입니다.")
    if journal.get("docId") or journal["status"] in ("eap_attempted", "eap_saved", "verified"):
        try:
            recover(client, journal)
        finally:
            save()
        return
    if journal["status"] in UNCLEAR:
        raise ExpenseError(f"{journal['status']}: 쓰기 결과가 불명확합니다. 재전송하지 않았습니다. 그룹웨어에서 확인하세요.")
    form = plan["source"]["form"]
    if journal["status"] == "prepared":
        validate(client, plan)
        initial = ex.eap_init(client, FORM_ID)
        if initial["resultMap"].get("appdocinfo") or len(initial["resultMap"]["docnumtype"]) != 1:
            raise ExpenseError("신규 전자결재 양식 초기화를 확인할 수 없습니다.")
        journal["attachments"] = []
        save("pdf_upload_attempted")
        for item in plan["attachments"]:
            uploaded = client.post("/ecm/ecm001A01", {"moduleGbn": "EAP"}, pdf=item["path"])
            if len(uploaded["list"]) != 1 or not uploaded["list"][0].get("fileId"):
                raise ExpenseError("PDF 업로드 결과를 확인할 수 없습니다. 재전송하지 않습니다.")
            journal["attachments"].append({
                "fileId": uploaded["list"][0]["fileId"], "fileName": Path(item["path"]).stem, "fileExtsn": "pdf",
                "fileSize": item["bytes"], "noConvertFileSize": item["bytes"], "moduleGbn": "EAP",
                "authKeyMap": {"compSeq": client.uc["compSeq"], "empSeq": client.uc["empSeq"], "docId": 0, "migYn": "0"}})
            save()
        save("pdf_uploaded")
    if journal["status"] == "pdf_uploaded":
        save("link_attempted")
        link = client.post(API + "0ap01002", {"approKey": journal["approKey"], "formDTp": form["formDTp"],
                           "formId": str(form["formId"]), "formNm": form["formNm"], "linkKey": "", "docTitle": plan["head"]["docTitle"],
                           "contents": "", "contentsApi": "/personal/APB1020/GetInterlockFormContents",
                           "statusApi": "/personal/APB1020/SetInterlockSync", "docAttachment": [], "appdocRefList": [],
                           "appLineId": "", "dummy1": "", "link": "", "menuCode": "APB1020", "fileList": "[]"})
        journal.update(linkKey=link["linkKey"], approKey=link["approKey"])
        save("link_saved")
    head = {**plan["head"], "linkKey": journal["linkKey"], "approKey": journal["approKey"], "frDt": plan["source"]["gisu"]["frDt"]}
    detail = [{**row, "linkKey": journal["linkKey"], "lnSq": index, "divCd": head["divCd"]}
              for index, row in enumerate(plan["rows"], 1)]
    if journal["status"] == "link_saved":
        linked = client.post(API + "0ap00020", {"linkKey": journal["linkKey"]})
        if any(item["approKey"] != journal["approKey"] for item in linked):
            raise ExpenseError("채번된 문서 연결키가 중복됩니다.")
        if any(item.get("docId") for item in linked):
            try:
                recover(client, journal)
            finally:
                save()
            return
        save("erp_attempted")
        client.post(API + "0ap01001", {"consList": [], "head": head, "detail": detail, "budgetList": None})
        save("erp_saved")
    initial = ex.eap_init(client, FORM_ID, journal["approKey"])
    contents = render(initial, client.uc, plan, dt.datetime.now(KST).strftime("%Y-%m-%d"))
    payload = ex.eap_draft_payload(initial, client, plan, journal["approKey"], contents,
                                   journal.get("attachments", []), form_id=FORM_ID)
    save("eap_attempted")
    try:
        result = client.post("/eap/eap110A06", payload)
        journal.update(status="eap_saved", docId=int(result["result"]))
    except ExpenseError as error:
        journal["saveResponseError"] = str(error)
    finally:
        journal["saveResponse"] = client.last_response
        save()
    try:
        recover(client, journal)
    finally:
        save()


def journal_key(plan):
    return hashlib.sha256(("tax:" + "|".join(sorted(i["approval"] for i in plan["invoices"]))).encode()).hexdigest()


def draft(args):
    client = Client()
    plan = read_private(args.plan)
    if plan.get("kind") != "taxinvoice":
        raise ExpenseError("세금계산서 검토안이 아닙니다.")
    DRAFTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = DRAFTS / (journal_key(plan) + ".json")
    approvals = {invoice["approval"] for invoice in plan["invoices"]}
    with (DRAFTS / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            journal = read_private(path)
            if journal["plan"] != plan:
                raise ExpenseError("이 세금계산서는 이미 저장을 시도했습니다. status --verify로 기존 문서를 먼저 확인하세요.")
        else:
            for other in DRAFTS.glob("*.json"):
                previous = read_private(other)
                if approvals & {invoice["approval"] for invoice in previous["plan"]["invoices"]}:
                    raise ExpenseError("다른 저장 시도와 세금계산서가 겹칩니다. status --verify로 확인하세요.")
            journal = {"status": "prepared", "approKey": "ERP_" + "_".join(secrets.token_hex(2) for _ in range(5)),
                       "plan": plan}
            write_private(path, journal)
        continue_draft(client, journal, path)
        print(f"임시보관 및 첨부 검증 완료: 문서 {journal['docId']} / {plan['head']['docTitle']}")


def status_command(args):
    client = Client()
    results = []
    for path in sorted(DRAFTS.glob("*.json")) if DRAFTS.exists() else []:
        journal = read_private(path)
        head = journal["plan"]["head"]
        if str(head["coCd"]) != str(client.erp["companyCode"]) or str(head["empCd"]) != str(client.erp["userCode"]):
            continue
        error = None
        if args.verify:
            try:
                recover(client, journal)
                write_private(path, journal, replace=True)
            except ExpenseError as exc:
                error = str(exc)
        results.append({"journal": str(path), "status": journal["status"], "docId": journal.get("docId"),
                        "title": head["docTitle"], "approvals": [i["approval"] for i in journal["plan"]["invoices"]],
                        "total": sum(row["sumAm"] for row in journal["plan"]["rows"]),
                        "serverChecked": bool(args.verify and not error), "error": error})
    print(json.dumps(results, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("read", help="PDF의 증빙 종류·승인번호·작성일자·금액·상호만 읽기 (네트워크 없음)")
    command.add_argument("pdf", nargs="+")
    command.set_defaults(run=read_command)
    command = commands.add_parser("prepare", help="PDF와 이전 1210 문서·카드 거래로 검토안 생성 (쓰기 없음)")
    command.add_argument("--pdf", action="append", required=True, help="(세금)계산서 PDF. 여러 장이면 반복")
    command.add_argument("--mode", choices=("recognition", "payment"), help="기본은 PDF의 [영수]/[청구]")
    command.add_argument("--profile", default=str(CONFIG / "state" / "me.profile.json"))
    command.add_argument("--vendor", help="거래처 이름. 기본은 PDF의 공급자 상호")
    command.add_argument("--title", help="문서 제목. 기본은 같은 거래처 이전 승인 문서 제목(한 장일 때)")
    command.add_argument("--memo", action="append", help="적요. PDF 순서대로 반복. 기본은 같은 거래처 이전 적요")
    command.add_argument("--purpose", help="용도 코드(1210 양식 기준). 기본은 이전 문서 또는 0350 기타지급수수료")
    command.add_argument("--project", help="프로젝트 코드")
    command.add_argument("--accounting-date", help="회계일. 기본은 작성일자(증빙일과 같게)")
    command.add_argument("--pay-date", help="지급요청일. 기본은 오늘")
    command.add_argument("--paid-date", action="append", help="인식용: 실제 결제일. PDF 순서대로 반복")
    command.add_argument("--paid-by", choices=("card", "transfer"), help="인식용: --paid-date의 결제 수단")
    command.add_argument("--note", action="append", help="비고 한 줄. 지정하면 기본 비고를 모두 대체")
    command.add_argument("--withdraw", action="append", help="MYSC 출금계좌 칸 한 줄. 인식용은 비워둔다")
    command.add_argument("--attach", action="append", help="함께 본문 첨부할 PDF (카드 이용내역 등)")
    command.add_argument("--out", required=True)
    command.set_defaults(run=prepare)
    command = commands.add_parser("check", help="현재 그룹웨어 데이터로 저장 전 검증 (쓰기 없음)")
    command.add_argument("plan")
    command.set_defaults(run=check_command)
    command = commands.add_parser("draft", help="1210 지출결의를 임시보관하고 ERP·본문·첨부를 재조회")
    command.add_argument("plan")
    command.set_defaults(run=draft)
    command = commands.add_parser("status", help="세금계산서 작업 기록과 저장 상태")
    command.add_argument("--verify", action="store_true")
    command.set_defaults(run=status_command)
    args = parser.parse_args()
    try:
        args.run(args)
    except (ExpenseError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        if isinstance(error, ExpenseError):
            print(f"오류: {error}", file=sys.stderr)
        else:
            print(f"오류: 입력 또는 로컬 파일을 확인하세요 ({type(error).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
