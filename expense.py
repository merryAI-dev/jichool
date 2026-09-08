#!/usr/bin/env python3
"""MYSC 지출결의 CLI. Python 표준 라이브러리만 사용한다."""

import argparse
import base64
import calendar
import copy
import datetime as dt
from decimal import Decimal, InvalidOperation
import getpass
import fcntl
import hashlib
import hmac
import html
import json
import os
import re
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse

from statement import StatementError, match_candidates, merchant_key, read_statement


ORIGIN = "https://gw.mysc.co.kr"
API = "/personal/APB1020New/"
CONFIG = Path.home() / ".config" / "mysc-expense"
SESSION = CONFIG / "session.json"


class ExpenseError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ExpenseError("요청이 다른 주소로 전환됐습니다. 인증을 확인하세요.")


def read_private(path):
    path = Path(path)
    with path.open() as handle:
        mode = os.fstat(handle.fileno())
        if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
            raise ExpenseError(f"파일 접근권한을 600으로 설정하세요: {path}")
        return json.load(handle)


def write_private(path, data, *, replace=False):
    write_private_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", replace=replace)


def write_private_text(path, raw, *, replace=False):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not replace:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def session_from_user_info(info):
    if isinstance(info, str):
        info = json.loads(info)
    if not isinstance(info, dict):
        raise ExpenseError("그룹웨어 사용자 세션 JSON이 필요합니다.")
    if "resultData" in info:
        info = info["resultData"]
    if isinstance(info, dict) and "sessionInfo" in info:
        info = info["sessionInfo"]
    if isinstance(info, str):
        info = json.loads(info)
    if not isinstance(info, dict):
        raise ExpenseError("로그인 응답에 세션 정보가 없습니다.")
    erp = info.get("erpUserInfo", {})
    uc = info.get("ucUserInfo", {})
    result = {
        "token": info.get("auth_a_token"),
        "signKey": info.get("hash_key"),
        "erp": {key: erp.get(key, "") for key in (
            "companyCode", "userCode", "userName", "deptCode", "deptName",
            "bizareaCode", "bizareaName", "fDate", "nGisu", "language")},
        "uc": {key: uc.get(key, "") for key in (
            "groupSeq", "compSeq", "compName", "bizSeq", "empSeq", "empName",
            "deptSeq", "deptName", "langCode", "eaType")},
    }
    if not all(isinstance(result[k], str) and result[k] for k in ("token", "signKey")):
        raise ExpenseError("세션에 인증 토큰 또는 서명키가 없습니다. 정상 로그인 후 다시 가져오세요.")
    if not erp.get("companyCode") or not erp.get("userCode"):
        raise ExpenseError("세션에 회사 또는 사원 정보가 없습니다.")
    return result


def signed_headers(token, key, path, timestamp, transaction):
    message = token + transaction + str(timestamp) + path
    signature = base64.b64encode(hmac.new(key.encode(), message.encode(), hashlib.sha256).digest()).decode()
    return {"Authorization": "Bearer " + token, "wehago-sign": signature,
            "timestamp": str(timestamp), "transaction-id": transaction,
            "Access-Domain": ORIGIN, "Content-Type": "application/json"}


class Client:
    def __init__(self, session=None):
        self.session = session if session is not None else read_private(SESSION)
        self.erp = self.session["erp"]
        self.uc = self.session["uc"]
        self.opener = urllib.request.build_opener(NoRedirect())

    def post(self, path, params, *, pdf=None, binary=False):
        self.last_response = None
        if not path.startswith(("/personal/", "/system/", "/eap/", "/ecm/")) or "?" in path or ".." in path:
            raise ExpenseError("지원하지 않는 그룹웨어 요청 경로입니다.")
        payload = {**params, "coCd": self.erp["companyCode"], "vPCoCd": self.erp["companyCode"]}
        headers = signed_headers(self.session["token"], self.session["signKey"],
                                 path, int(time.time()), secrets.token_hex(16))
        if pdf:
            if path != "/ecm/ecm001A01" or params != {"moduleGbn": "EAP"}:
                raise ExpenseError("전자결재 증빙 PDF 업로드만 지원합니다.")
            file = Path(pdf)
            raw = file.read_bytes()
            if not raw.startswith(b"%PDF-") or any(char in file.name for char in '\r\n"'):
                raise ExpenseError("PDF 내용 또는 파일명을 확인하세요.")
            boundary = "mysc-expense-" + secrets.token_hex(16)
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="moduleGbn"\r\n\r\nEAP\r\n'
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file[]"; filename="{file.name}"\r\n'
                    'Content-Type: application/pdf\r\n\r\n').encode() + raw + f'\r\n--{boundary}--\r\n'.encode()
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        elif path in ("/ecm/ecm001A03", "/ecm/ecm001A04"):
            # ECM 조회는 네이티브 ajaxEbp의 기본 form 인코딩을 사용한다.
            body = urllib.parse.urlencode({key: ",".join(map(str, value)) if isinstance(value, list) else value
                                           for key, value in params.items()}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(payload).encode()
        if binary and path != "/ecm/ecm001A03":
            raise ExpenseError("전자결재에 연결한 증빙 다운로드만 지원합니다.")
        request = urllib.request.Request(ORIGIN + path, body, headers, method="POST")
        # 쓰기 응답이 유실될 수 있으므로 모든 HTTP 호출은 자동 재시도하지 않는다.
        try:
            with self.opener.open(request, timeout=30) as response:
                if binary:
                    return response.read()
                result = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 601):
                raise ExpenseError(f"인증 만료 또는 권한 부족(HTTP {error.code}). expense.py auth로 다시 인증하세요.") from None
            raise ExpenseError(f"그룹웨어 HTTP {error.code}: {path}. 자동 재시도하지 않았습니다.") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise ExpenseError(f"그룹웨어 응답을 확인할 수 없습니다: {path}. 자동 재시도하지 않았습니다.") from None
        self.last_response = result
        if not isinstance(result, dict) or str(result.get("resultCode")) != "0":
            code = result.get("resultCode", "unknown") if isinstance(result, dict) else "unknown"
            raise ExpenseError(f"그룹웨어 요청 거절: {path}, resultCode={code}")
        return result.get("resultData")

    def flags(self, module, control):
        return self.post("/system/common/flagsEx", {
            "moduleCd": module, "ctrlCd": control, "useYn": "1", "allCdYn": "1"})

    def codepicker(self, help_type, **params):
        return self.post("/system/codepickers/CP0001A0001", {
            "helpTy": help_type, "langkind": self.erp.get("language", "KOR"), **params})

    def cards(self, start, end, card_name):
        params = {"issDtFrom": start, "issDtTo": end, "regNb": "", "trNm": "",
                  "amFrom": "", "amTo": "", "sunginNb": "", "tabFg": "1",
                  "baNbList": [], "isShowCardPrivacy": "", "isShowAccountPrivacy": ""}
        cards = self.post(API + "0ap00008", params)
        cards = [card for card in cards if card.get("cardNm") == card_name]
        if len(cards) != 1:
            raise ExpenseError("이름이 정확히 일치하는 카드를 한 개 선택할 수 없습니다.")
        params["baNbList"] = [{"baNb": row["cardCd"], "cardFg": row["cardFg"]} for row in cards]
        indexes = self.post(API + "0ap00029", params)
        rows = []
        for start_index in range(0, len(indexes), 300):
            keys = [{key: row[key] for key in ("issDt", "issSq", "coCd")}
                    for row in indexes[start_index:start_index + 300]]
            rows.extend(self.post(API + "0ap00030", {**params, "cardKeyList": keys,
                                                     "usingCardList": [], "linkKey": ""}))
        card_types = {row["cardCd"]: row["cardFg"] for row in cards}
        for row in rows:
            if row.get("cardCd") not in card_types or not start <= row["issDt"] <= end:
                raise ExpenseError("선택한 카드 또는 기간 밖의 거래가 반환됐습니다.")
            row["cardFg"] = card_types[row["cardCd"]]
            row["cardNm"] = card_name
        return [row for row in rows if row.get("appYn") == "N"
                and won(row["sunginAm"]) != won(row.get("appSumAm"))]

    def receipt(self, transaction):
        return self.post(API + "0ap00011", {
            "issDt": transaction["issDt"], "issSq": transaction["issSq"],
            "sunginNb": transaction["sunginNb"], "popCoCd": self.erp["companyCode"],
            "isShowCardPrivacy": ""})

    def document(self, link_key):
        heads = self.post(API + "0ap00002", {"linkKey": link_key})
        if len(heads) != 1 or str(heads[0].get("empCd")) != str(self.erp["userCode"]):
            raise ExpenseError("본인 지출결의 문서만 처리할 수 있습니다.")
        rows = self.post(API + "0ap00003", {"linkKey": link_key,
                                            "isShowCardPrivacy": "", "isShowAccountPrivacy": ""})
        return {"head": heads[0], "detail": rows}

    def history(self, start, end, form_id):
        documents = self.post(API + "0ap00001", {
            "appDateFrom": start, "appDateTo": end, "docTitle": "", "fromAm": "",
            "toAm": "", "docNo": "", "docStatus": "1", "dateFg": "1"})
        history = []
        for doc in documents:
            if str(doc["formId"]) != str(form_id):
                continue
            item = self.document(doc["linkKey"])
            if str(item["head"].get("approState")) == "1":
                history.append({"document": doc, **item})
        return history


def date_value(value):
    value = str(value).replace("-", "")
    if not re.fullmatch(r"\d{8}", value):
        raise ExpenseError("날짜는 YYYY-MM-DD 또는 YYYYMMDD로 입력하세요.")
    dt.datetime.strptime(value, "%Y%m%d")
    return value


def won(value):
    try:
        number = Decimal(str(value if value not in (None, "") else 0))
        if not number.is_finite() or number != number.to_integral_value():
            raise ExpenseError("원화 금액이 정수가 아닙니다. 원본 거래를 확인하세요.")
        return int(number)
    except InvalidOperation:
        raise ExpenseError("잘못된 원화 금액입니다.") from None


def identity(transaction):
    fields = (transaction.get("coCd"), transaction.get("issDt"), transaction.get("issSq"),
              transaction.get("sunginNb"))
    if any(value in (None, "", 0, "0") for value in fields):
        raise ExpenseError("원본 카드 거래의 증빙 식별자가 없습니다.")
    return ":".join(map(str, fields))


def verify_receipt(transaction, receipts):
    identity(transaction)
    if len(receipts) != 1:
        raise ExpenseError("원본 카드 거래의 영수증을 한 건으로 확인할 수 없습니다.")
    receipt = receipts[0]
    receipt_date = receipt.get("issDt") or re.sub(r"\D", "", receipt.get("formatedIssDtTime") or "")[:8]
    if (str(receipt.get("sunginNb")) != str(transaction["sunginNb"])
            or receipt_date != transaction["issDt"]
            or won(receipt["sunginAm"]) != won(transaction["sunginAm"])
            or receipt.get("chainName") != transaction.get("chainName")):
        raise ExpenseError("카드 거래와 연결된 영수증의 식별자 또는 승인금액이 일치하지 않습니다.")
    # 상세조회 DTO는 coCd·issSq를 null로 반환한다. 반환된 경우에는 추가 대조한다.
    for field in ("coCd", "issSq"):
        if receipt.get(field) is not None and str(receipt[field]) != str(transaction[field]):
            raise ExpenseError("영수증 상세조회 결과의 회사 또는 거래순번이 다릅니다.")


def collect(client, start, end, form_id, card_name, accounting_date=""):
    start, end = date_value(start), date_value(end)
    if start > end:
        raise ExpenseError("시작일이 종료일보다 늦습니다.")
    forms = client.post("/eap/eap096A45", {
        "header": {"pId": "", "groupSeq": client.uc["groupSeq"], "empSeq": client.uc["empSeq"]},
        "body": {"companyInfo": {"compSeq": client.uc["compSeq"]}, "formDTp": "",
                 "searchFormDTp": "APB1020", "langCode": client.uc["langCode"]}})
    forms = [form for form in forms if str(form["formId"]) == str(form_id) and form["useYn"] in ("1", "Y")]
    if len(forms) != 1:
        raise ExpenseError("사용 가능한 지출결의 양식을 확인할 수 없습니다.")
    accounting_date = date_value(accounting_date) if accounting_date else ""
    cards = client.cards(start, end, card_name)
    receipts = [client.receipt(card) for card in cards]
    for card, receipt in zip(cards, receipts):
        verify_receipt(card, receipt)
    since = (dt.datetime.strptime(start, "%Y%m%d").date() - dt.timedelta(days=183)).strftime("%Y%m%d")
    divisions = client.post(API + "0ap00017", {"empCd": client.erp["userCode"]})
    if len(divisions) != 1:
        raise ExpenseError("회계단위가 여러 개입니다. 적용할 회계단위 확인이 필요합니다.")
    return {"period": {"from": start, "to": end}, "account": client.erp,
            "form": forms[0], "division": divisions[0], "cardName": card_name,
            "gisu": client.post("/system/financialApi/selectGisuData", {"frDt": accounting_date or end}),
            "accountingDate": accounting_date, "cards": cards, "receipts": receipts,
            "dateRules": client.post("/personal/modulecommon/config/getConfig", {
                "syscfg": [{"moduleCd": "A", "ctrCd": key} for key in ("51", "60", "68", "Z3")]}),
            "history": client.history(since, end, form_id),
            "cashTypes": client.post("/personal/APCodePicker/ApAcashcdTypeCode", {
                "helpTy": "ACASHCD_TYPE_CODE", "langkind": "KOR", "multiCode": [int(form_id)]}),
            "projects": client.codepicker("SPJT_CODE_AUTH", empCd=client.erp["userCode"], useYn="ALL"),
            "payMethods": client.post(API + "0ap00041", {}),
            "holidays": client.post(API + "0ap00013", {
                "searchYears": "|".join(str(year) for year in range(int(start[:4]), int(end[:4]) + 2))})}


def history_candidates(transaction, history, user_code, query=""):
    candidates = []
    for document in history:
        if (str(document["head"].get("approState")) != "1"
                or str(document["head"].get("empCd")) != str(user_code)):
            continue
        for row in document["detail"]:
            if (str(row.get("coCd")) != str(transaction["coCd"])
                    or not row.get("cardCd") or row["cardCd"] != transaction.get("cardCd")):
                continue
            if query:
                if query.casefold() not in (str(row.get("trNm", "")) + " " + str(row.get("rmkDc", ""))).casefold():
                    continue
            elif (not row.get("regNb") or row["regNb"] != transaction.get("chainRegnb")
                    or str(row.get("trNm", "")).strip().casefold() != str(transaction.get("chainName", "")).strip().casefold()):
                continue
            candidates.append({"row": row, "linkKey": document["document"]["linkKey"],
                               "date": document["document"].get("isuDt", "")})
    return candidates


def payment_attribute(transaction, methods):
    payment = "02" if str(transaction.get("liqWs")) == "2" else "01"
    codes = {str(item.get("attrCd") or "") for item in methods if item.get("paymethodFg") == payment}
    if len(codes) != 1 or "" in codes:
        raise ExpenseError("현재 그룹웨어의 지급수단 코드를 한 개로 확인할 수 없습니다.")
    return codes.pop()


def make_plan(snapshot):
    account = snapshot["account"]
    head = {"coCd": account["companyCode"], "empCd": account["userCode"], "empNm": account["userName"],
            "deptCd": account["deptCode"], "deptNm": account["deptName"],
            "divCd": snapshot["division"]["divCd"], "divNm": snapshot["division"]["divNm"],
            "gisu": snapshot["gisu"]["gisu"], "isuDt": snapshot.get("accountingDate", snapshot["period"]["to"]),
            "docTitle": "법인카드 지출결의 " + snapshot["period"]["from"] + "~" + snapshot["period"]["to"],
            "docugroupYn": "Y", "onlyCashFg": "1", "fileGroup": "0", "fileDc": "",
            "pjtCd": "", "pjtNm": "", "consDocTitle": "", "exdocuYn": "N"}
    rows, seen = [], set()
    if len(snapshot["cards"]) != len(snapshot["receipts"]):
        raise ExpenseError("카드 거래와 영수증 개수가 다릅니다.")
    for transaction, receipt in zip(snapshot["cards"], snapshot["receipts"]):
        key = identity(transaction)
        if key in seen or str(transaction["coCd"]) != str(account["companyCode"]):
            raise ExpenseError("중복 거래 또는 다른 회사의 거래가 포함됐습니다.")
        seen.add(key)
        verify_receipt(transaction, receipt)
        total = won(transaction["sunginAm"]) - won(transaction.get("appSumAm"))
        vat = won(transaction.get("vatAm")) - won(transaction.get("appVatAm"))
        row = {"coCd": account["companyCode"], "cardDt": transaction["issDt"],
               "cardSq": won(transaction["issSq"]), "sunginNb": transaction["sunginNb"],
               "vatGroupDc": transaction["sunginNb"], "cardCd": transaction.get("cardCd", ""),
               "cardNm": transaction.get("cardNm", ""),
               "cardTrCd": transaction.get("cardTrCd", ""), "cardFg": transaction.get("cardFg", ""),
               "issDt": transaction["issDt"], "sumAm": total, "vatAm": vat, "supAm": total - vat,
               "orgCardSumAm": won(transaction["sunginAm"]), "orgCardVatAm": won(transaction.get("vatAm")),
               "orgCardSupAm": won(transaction["sunginAm"]) - won(transaction.get("vatAm")),
               "trCd": transaction.get("trCd", ""), "trNm": transaction.get("chainName", ""),
               "regNb": transaction.get("chainRegnb", ""), "empCd": account["userCode"],
               "empNm": account["userName"], "deptCd": account["deptCode"], "deptNm": account["deptName"],
               "bankCd": transaction.get("sbankCd") or "", "bankNm": transaction.get("sbankNm") or "",
               "baNb": transaction.get("baNb") or "", "depositorDc": transaction.get("depositor") or "",
               "realDepositorDc": "", "realDepositorFg": "", "cashCd": "", "cashNm": "",
               "rmkDc": "", "pjtCd": "", "pjtNm": "", "carCd": "", "carNb": "",
               "issNo": "", "issSq": 0, "cashDt": "", "cashSq": None, "taxTy": "",
               "payDt": transaction.get("payDt") or "", "fileGroup": "0", "fileDc": "", "fileInfoList": [],
               "budgetSts": "0", "formId": snapshot["form"]["formId"], "linkKey": ""}
        row["attrCd"] = payment_attribute(transaction, snapshot["payMethods"])
        # ponytail: 같은 카드·사업자번호·가맹점의 일치 이력만 제안. 퍼지 매칭은 오분류 근거가 쌓이면 추가.
        candidates = history_candidates(transaction, snapshot["history"], account["userCode"])
        proposed = {}
        for field in ("rmkDc", "cashCd", "pjtCd", "carCd"):
            values = {str(item["row"].get(field) or "") for item in candidates}
            if len(values) == 1 and next(iter(values)):
                row[field] = next(iter(values))
                proposed[field] = {"value": row[field], "sourceDocuments": sorted({item["linkKey"] for item in candidates})}
        item = {"identity": key, "expense": row, "proposed": proposed,
                "candidateCount": len(candidates), "receiptVerified": True}
        item["issues"] = row_issues(row, snapshot)
        rows.append(item)
    return {"schema": 1, "head": head, "rows": rows, "source": snapshot}


def row_issues(row, snapshot):
    issues = []
    for field, label in (("cashCd", "용도"), ("rmkDc", "적요"), ("empCd", "사원"),
                         ("issDt", "증빙일자"), ("payDt", "지급요청일")):
        if not row.get(field):
            issues.append(label + " 확인 필요")
    if len(row.get("rmkDc", "")) > 80:
        issues.append("적요는 80자 이하")
    for field in ("issDt", "payDt"):
        if row.get(field):
            date_value(row[field])
    rules = {item["ctrCd"]: item for item in snapshot.get("dateRules", {}).get("syscfg", [])}
    accounting_date = snapshot.get("accountingDate", "")
    period_rule = rules.get("51", {}).get("fgTy")
    length = {"1": 6, "2": 4}.get(period_rule)
    if length and accounting_date and row.get("issDt") and accounting_date[:length] != row["issDt"][:length]:
        issues.append("회계처리일·증빙일자가 그룹웨어 허용 기간과 다름")
    if rules.get("60", {}).get("fgTy") == "1" and row.get("payDt") and row["payDt"] < max(accounting_date, row.get("issDt", "")):
        issues.append("지급요청일이 회계처리일 또는 증빙일자보다 빠름")
    cash = next((item for item in snapshot["cashTypes"] if str(item["typeCd"]) == str(row["cashCd"])), None)
    if row["cashCd"] and cash is None:
        issues.append("현재 양식에서 사용할 수 없는 용도")
    if cash:
        row["cashNm"] = cash["typeNm"]
        for flag, field, label in (("deptCheckYn", "deptCd", "사용부서"), ("pjtCheckYn", "pjtCd", "프로젝트"),
                                   ("trcdCheckYn", "trCd", "거래처"), ("carYn", "carCd", "업무용승용차")):
            row[flag] = cash.get(flag, "N")
            if cash.get(flag) == "Y" and not row.get(field):
                issues.append(label + " 필수")
        if cash.get("accountCheckYn") == "Y" and any(not row.get(field) for field in ("bankCd", "baNb", "depositorDc")):
            issues.append("은행·계좌·예금주 확인 필요")
        if cash.get("budgetCheckYn") == "Y":
            issues.append("예산통제 항목의 예산정보 확인 필요")
        if str(cash.get("deductFg")) == "5":
            row["supAm"], row["vatAm"] = row["sumAm"], 0
        row["deductFg"] = cash.get("deductFg", "")
    if row.get("pjtCd"):
        project = next((item for item in snapshot["projects"] if str(item["pjtCd"]) == str(row["pjtCd"])), None)
        if project is None:
            issues.append("접근 가능한 프로젝트 목록에 없음")
        else:
            row["pjtNm"] = project["pjtNm"]
            start, end = project.get("frDt") or "", project.get("toDt") or ""
            if (start and start != "00000000" and row["issDt"] < start) or (end and end != "00000000" and row["issDt"] > end):
                issues.append("증빙일자가 프로젝트 기간 밖임")
    if row["sumAm"] != row["supAm"] + row["vatAm"]:
        issues.append("공급가액·세액 합계 불일치")
    return issues


def load_profile(path, account):
    profile = read_private(path)
    owner = {key: str(account[key]) for key in ("companyCode", "userCode")}
    if profile.get("schema") != 1 or profile.get("owner") != owner:
        raise ExpenseError("현재 로그인 사용자와 프로필 소유자가 다릅니다.")
    if not profile.get("cardName") or profile.get("dates") != {"accounting": "month-end", "payment": "today"}:
        raise ExpenseError("프로필의 카드와 날짜 규칙을 확인하세요.")
    seen = set()
    for rule in profile.get("rules", []):
        key = merchant_key(rule.get("merchant", ""))
        if not key or key in seen or not rule.get("description") or not rule.get("purpose"):
            raise ExpenseError("적요 규칙의 가맹점·적요·용도를 확인하세요. 같은 가맹점 규칙은 하나만 등록합니다.")
        seen.add(key)
    for alias in profile.get("merchantAliases", []):
        if not alias.get("statement") or not alias.get("card"):
            raise ExpenseError("명세서 가맹점 별칭의 두 이름을 모두 지정하세요.")
    return profile


def profile_command(args):
    client = Client()
    profile = {"schema": 1, "owner": {key: str(client.erp[key]) for key in ("companyCode", "userCode")},
               "cardName": args.card, "defaultProject": args.project or "",
               "dates": {"accounting": "month-end", "payment": "today"}, "rules": [], "merchantAliases": []}
    write_private(args.out, profile)
    print("개인 프로필 생성: " + args.out + " (영구 적요 규칙은 아직 없음)")


def rule_command(args):
    profile = load_profile(args.profile, Client().erp)
    profile["rules"] = [r for r in profile["rules"] if merchant_key(r["merchant"]) != merchant_key(args.merchant)]
    profile["rules"].append({"merchant": args.merchant, "description": args.description,
                             "purpose": args.purpose, "project": args.project or "", "source": "사용자 명시 등록"})
    if args.statement_merchant:
        profile["merchantAliases"] = [a for a in profile["merchantAliases"]
                                       if merchant_key(a["statement"]) != merchant_key(args.statement_merchant)]
        profile["merchantAliases"].append({"statement": args.statement_merchant, "card": args.merchant})
    write_private(args.out, profile)
    print("영구 규칙을 새 프로필에 저장했습니다: " + args.out)


def record_adjustment(plan, item, amount, vat, evidence, page, reason):
    if not evidence or not reason or not page or page < 1:
        raise ExpenseError("금액 수정에는 근거 PDF·페이지·사유가 필요합니다.")
    evidence = Path(evidence).resolve(strict=True)
    raw = evidence.read_bytes()
    row = item["expense"]
    if not raw.startswith(b"%PDF-"):
        raise ExpenseError("금액 수정 근거는 원본 PDF를 지정하세요.")
    if amount * row["orgCardSumAm"] <= 0 or abs(vat) > abs(amount) or (vat and vat * amount < 0):
        raise ExpenseError("취소 여부 또는 공급가액·부가세 부호가 원본과 맞지 않습니다.")
    source = next(card for card in plan["source"]["cards"] if identity(card) == item["identity"])
    item["amountAdjustment"] = {"originalRemaining": won(source["sunginAm"]) - won(source.get("appSumAm")),
                                "amount": amount, "vat": vat, "reason": reason, "evidencePath": str(evidence),
                                "sha256": hashlib.sha256(raw).hexdigest(), "page": page}
    row.update(sumAm=amount, vatAm=vat, supAm=amount - vat)


def bind_statement(plan, item, charge, reason):
    row = item["expense"]
    if row["cardDt"] != charge["date"] or item.get("excluded"):
        raise ExpenseError("증빙일자가 다르거나 제외한 거래입니다.")
    if row["sumAm"] != charge["amount"]:
        if charge["currency"] == "KRW" or row["vatAm"] or item.get("alreadySaved"):
            raise ExpenseError("청구금액이 다릅니다. 근거를 확인하고 금액·부가세를 직접 수정하세요.")
        record_adjustment(plan, item, charge["amount"], 0, plan["statement"]["file"], charge["page"],
                          "명세서의 해외 원화 청구금액 적용")
    item["statementMatch"] = {"id": charge["id"], "page": charge["page"], "reason": reason}


def reconcile_statement(plan, profile):
    charges = plan["statement"]["charges"]
    bound = {item["statementMatch"]["id"] for item in plan["rows"] if item.get("statementMatch")}
    while True:
        available = [item for item in plan["rows"] if not item.get("statementMatch") and not item.get("excluded")]
        proposals = []
        for charge in charges:
            if charge["id"] in bound:
                continue
            candidates, reason = match_candidates(charge, available, profile.get("merchantAliases", []))
            if reason != "가맹점 확인 필요":
                proposals.append((charge, candidates, reason))
        unique = [(charge, candidates[0], reason) for charge, candidates, reason in proposals
                  if len(candidates) == 1 and sum(candidates[0] in other for _, other, _ in proposals) == 1]
        if not unique:
            break
        for charge, item, reason in unique:
            bind_statement(plan, item, charge, reason)
            bound.add(charge["id"])
    for item in plan["rows"]:
        item["issues"] = row_issues(item["expense"], plan["source"])


def statement_questions(plan):
    statement = plan.get("statement", {})
    if "charges" not in statement:
        return []
    questions, used = [], set()
    charges = {row["id"]: row for row in statement["charges"]}
    if len(charges) != statement["count"] or sum(c["amount"] for c in charges.values()) != statement["total"]:
        raise ExpenseError("명세서의 거래 목록·총 건수·금액이 다릅니다.")
    for index, item in enumerate(plan["rows"], 1):
        if item.get("excluded"):
            continue
        link = item.get("statementMatch")
        if not link:
            questions.append({"kind": "unmatched_card", "row": index, "message": "명세서 연결 또는 이번 결의 제외 사유 확인"})
            continue
        charge = charges.get(link["id"])
        if charge is None or link["id"] in used:
            raise ExpenseError("명세서 거래가 중복 연결됐거나 존재하지 않습니다.")
        used.add(link["id"])
        if charge["date"] != item["expense"]["cardDt"] or charge["amount"] != item["expense"]["sumAm"]:
            questions.append({"kind": "amount_mismatch", "row": index, "message": "명세서와 증빙일·금액 불일치"})
    for charge in statement["charges"]:
        if charge["id"] not in used:
            available = [item for item in plan["rows"] if not item.get("statementMatch")]
            candidates, reason = match_candidates(charge, available, plan.get("profile", {}).get("merchantAliases", []))
            questions.append({"kind": "unmatched_statement", "charge": charge["id"], "merchant": charge["merchant"],
                              "amount": charge["amount"], "message": reason,
                              "candidateRows": [plan["rows"].index(row) + 1 for row in candidates]})
    return questions


def inspect_plan(plan):
    included = [row for row in plan["rows"] if not row.get("excluded")]
    questions = statement_questions(plan)
    questions += [{"kind": "field", "row": i, "message": message} for i, row in enumerate(plan["rows"], 1)
                  if not row.get("excluded") and not row.get("alreadySaved") for message in row["issues"]]
    questions += [{"kind": "confirmation", "message": message} for message in plan.get("unresolved", [])]
    labels = {"rmkDc": "적요", "cashCd": "용도", "pjtCd": "프로젝트", "trCd": "거래처 코드"}
    for index, item in enumerate(plan["rows"], 1):
        if item.get("excluded") or item.get("alreadySaved"):
            continue
        for field, options in item.get("historyOptions", {}).items():
            if field in item.get("confirmedFields", []) or item.get("proposed", {}).get(field, {}).get("source") == "개인 프로필의 명시적 영구 규칙":
                continue
            if field == "trCd" and not options:
                continue  # 거래처 코드가 필수인 경우에는 row_issues가 질문한다.
            count = sum(option["count"] for option in options)
            if len(options) != 1 or count < 2 or (field == "trCd" and item["expense"].get(field) != options[0]["value"]):
                questions.append({"kind": "history", "row": index, "field": field,
                                  "message": labels[field] + ("의 과거 선택이 서로 다릅니다" if len(options) > 1 else "의 승인 근거가 부족합니다"),
                                  "current": item["expense"].get(field, ""), "options": options})
        description = item["expense"].get("rmkDc", "")
        if (item.get("historyOptions") and re.search(r"야근|출장|회의|접대|회식", description)
                and "rmkDc" not in item.get("confirmedFields", [])
                and item.get("proposed", {}).get("rmkDc", {}).get("source") != "개인 프로필의 명시적 영구 규칙"):
            questions.append({"kind": "purpose_review", "row": index, "field": "rmkDc",
                              "message": "과거 적요가 이번 사용 목적에도 맞는지 검토하세요. 근거가 부족하면 사용자에게 질문하세요.",
                              "current": description})
    return {"count": len(included), "total": sum(row["expense"]["sumAm"] for row in included),
            "statementCount": plan.get("statement", {}).get("count"), "statementTotal": plan.get("statement", {}).get("total"),
            "alreadySaved": sum(bool(row.get("alreadySaved")) for row in included), "questions": questions}


def context_command(args):
    plan = read_private(args.plan)
    if not 1 <= args.row <= len(plan["rows"]):
        raise ExpenseError("존재하지 않는 행입니다.")
    item = plan["rows"][args.row - 1]
    source = plan["source"]
    transaction, receipt = next((card, receipt[0]) for card, receipt in zip(source["cards"], source["receipts"])
                                if identity(card) == item["identity"])
    candidates = history_candidates(transaction, source["history"], source["account"]["userCode"], args.query or "")
    history = []
    for candidate in sorted(candidates, key=lambda c: c["date"], reverse=True)[:12]:
        row = candidate["row"]
        history.append({"document": candidate["linkKey"], "accountingDate": candidate["date"],
                        **{key: row.get(key) for key in ("issDt", "trNm", "sumAm", "rmkDc", "cashCd", "cashNm", "pjtCd", "pjtNm", "trCd")}})
    print(json.dumps({"row": args.row, "merchant": transaction["chainName"],
                      "approvalAt": receipt.get("formatedIssDtTime") or transaction["issDt"],
                      "originalAmount": won(transaction["sunginAm"]), "proposedAmount": item["expense"]["sumAm"],
                      "description": item["expense"].get("rmkDc", ""), "historyMatchCount": len(candidates),
                      "approvedHistory": history,
                      "reviewPolicy": "승인 시각·가맹점·과거 적요는 참고 근거입니다. 이번 야근·출장·회의 목적을 단정하지 말고 의심되면 사용자에게 질문하세요."},
                     ensure_ascii=False, indent=2))


def prepare_plan(snapshot, statement, profile, today, completed=()):
    plan = make_plan(copy.deepcopy(snapshot))
    plan.update(statement=statement, profile=profile, dateChoice={"accountingDate": snapshot["period"]["to"],
                                                                "payDate": date_value(today), "source": "개인 프로필"})
    plan["head"]["isuDt"] = snapshot["period"]["to"]
    plan["head"]["docTitle"] = f"미지급직원_{snapshot['account']['userName']}_{int(snapshot['period']['from'][4:6])}월"
    saved = {item["identity"]: item for item in completed}
    for item in plan["rows"]:
        if item["identity"] in saved:
            item.update(copy.deepcopy(saved[item["identity"]]))
            continue
        row = item["expense"]
        transaction = next(card for card in snapshot["cards"] if identity(card) == item["identity"])
        candidates = history_candidates(transaction, snapshot["history"], snapshot["account"]["userCode"])
        item["historyOptions"] = {}
        for field in ("rmkDc", "cashCd", "pjtCd", "trCd"):
            values = sorted({str(c["row"].get(field) or "") for c in candidates} - {""})
            item["historyOptions"][field] = [
                {"value": value, "count": len({c["linkKey"] for c in candidates if str(c["row"].get(field) or "") == value}),
                 "documents": sorted({c["linkKey"] for c in candidates if str(c["row"].get(field) or "") == value})}
                for value in values]
            names = ({r["typeCd"]: r["typeNm"] for r in snapshot["cashTypes"]} if field == "cashCd" else
                     {r["pjtCd"]: r["pjtNm"] for r in snapshot["projects"]} if field == "pjtCd" else {})
            for option in item["historyOptions"][field]:
                option["label"] = names.get(option["value"], option["value"])
        if not row.get("trCd") and len(item["historyOptions"]["trCd"]) == 1:
            # 거래처 코드는 제안만 표시한다. 현재 유효성을 검증하지 않은 코드를 자동 입력하지 않는다.
            item["historyOptions"]["trCd"][0]["needsCurrentValidation"] = True
        row["payDt"] = date_value(today)
        if profile.get("defaultProject") and not row["pjtCd"]:
            row["pjtCd"] = profile["defaultProject"]
        for rule in profile.get("rules", []):
            if merchant_key(rule["merchant"]) == merchant_key(row["trNm"]):
                for key, value in (("rmkDc", rule["description"]), ("cashCd", rule["purpose"]), ("pjtCd", rule.get("project"))):
                    if value:
                        row[key] = value
                        item["proposed"][key] = {"value": value, "source": "개인 프로필의 명시적 영구 규칙"}
    reconcile_statement(plan, profile)
    return plan


def prepare(args):
    client = Client()
    profile = load_profile(args.profile, client.erp)
    statement = read_statement(args.pdf, args.month)
    year, month = map(int, args.month.split("-"))
    start, end = f"{year:04d}{month:02d}01", f"{year:04d}{month:02d}{calendar.monthrange(year, month)[1]}"
    snapshot = collect(client, start, end, 1211, profile["cardName"], end)
    completed, changed, previous_choices = [], [], {}
    for path, journal in own_journals(client):
        original = journal["plan"]["source"]
        if original["period"] != snapshot["period"] or original.get("cardName") != profile["cardName"]:
            continue
        try:
            recover_draft(client, journal)
        except ExpenseError as error:
            changed.append(f"기존 문서 {journal.get('docId', '미확정')} 상태 확인 필요: {error}")
            continue
        write_private(path, journal, replace=True)
        previous = journal.get("validationPlan", {})
        if previous.get("statement", {}).get("sha256") == statement["sha256"]:
            previous_choices.update({row["identity"]: row for row in previous["rows"]})
        for card, receipt, item in zip(original["cards"], original["receipts"], journal["plan"]["rows"]):
            key = item["identity"]
            positions = [i for i, row in enumerate(snapshot["cards"]) if identity(row) == key]
            if positions:
                index = positions[0]
                snapshot["cards"][index], snapshot["receipts"][index] = card, receipt
            else:
                snapshot["cards"].append(card)
                snapshot["receipts"].append(receipt)
            item = copy.deepcopy(item)
            if journal["plan"].get("statement", {}).get("sha256") != statement["sha256"]:
                item.pop("statementMatch", None)
            completed.append({**item, "alreadySaved": {"docId": journal["docId"], "journal": str(path)}})
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y%m%d")
    previous_choices.update({row["identity"]: row for row in completed})
    plan = prepare_plan(snapshot, statement, profile, today, list(previous_choices.values()))
    plan["unresolved"] = changed
    write_private(args.out, plan)
    review_path = str(Path(args.out).with_suffix(".review.html"))
    write_private_text(review_path, review_html(plan))
    print(json.dumps({**inspect_plan(plan), "plan": args.out, "review": review_path}, ensure_ascii=False, indent=2))


def match_command(args):
    plan = read_private(args.plan)
    if not args.reason.strip() or not 1 <= args.row <= len(plan["rows"]):
        raise ExpenseError("원본 행 번호와 연결 확인 사유를 지정하세요.")
    charge = next((row for row in plan["statement"]["charges"] if row["id"] == args.charge), None)
    if not charge or any(row.get("statementMatch", {}).get("id") == args.charge for row in plan["rows"]):
        raise ExpenseError("명세서 거래가 없거나 이미 연결됐습니다.")
    item = plan["rows"][args.row - 1]
    if item.get("statementMatch"):
        raise ExpenseError("이미 명세서에 연결된 카드 거래입니다.")
    bind_statement(plan, item, charge, "사용자 확인: " + args.reason)
    reconcile_statement(plan, plan.get("profile", {}))
    write_private(args.out, plan)
    print(json.dumps(inspect_plan(plan), ensure_ascii=False, indent=2))


def show_plan(plan):
    print(plan["head"]["docTitle"])
    print(f"카드: {plan['source'].get('cardName', '미지정')} / 회계처리일: {plan['head']['isuDt'] or '확인 필요'}")
    for message in plan.get("unresolved", []):
        print("전체 확인: " + message)
    for index, item in enumerate(plan["rows"], 1):
        row = item["expense"]
        if item.get("excluded"):
            print(f"{index:>2}. 이번 결의 제외 | {row['trNm']} | {row['sumAm']:,}원 | {item['excluded']}")
            continue
        print(f"{index:>2}. {row['issDt']} | {row['trNm']} | {row['sumAm']:,}원 | {row['rmkDc'] or '적요 확인 필요'} | {row['pjtNm'] or '프로젝트 미지정'}")
        if item["issues"]:
            print("    확인: " + "; ".join(item["issues"]))
    included = [item for item in plan["rows"] if not item.get("excluded")]
    print(f"{len(included)}건 / 합계 {sum(item['expense']['sumAm'] for item in included):,}원 / 영수증 대조 {sum(bool(item['receiptVerified']) for item in included)}건 / 저장 실행 전")
    for question in inspect_plan(plan)["questions"]:
        if question["kind"] != "field":
            print("확인 필요: " + json.dumps(question, ensure_ascii=False))


def preview(args):
    plan = make_plan(collect(Client(), args.start, args.end, args.form, args.card, args.accounting_date))
    write_private(args.out, plan)
    show_plan(plan)
    print(f"검토 파일: {args.out}")


def edit(args):
    plan = read_private(args.plan)
    if args.row < 1 or args.row > len(plan["rows"]):
        raise ExpenseError("존재하지 않는 행입니다.")
    names = {"description": "rmkDc", "purpose": "cashCd", "project": "pjtCd", "vendor": "trCd",
             "employee": "empCd", "department": "deptCd", "bank": "bankCd",
             "account": "baNb", "holder": "depositorDc", "vehicle": "carCd",
             "receipt-date": "issDt", "pay-date": "payDt", "amount": "sumAm", "vat": "vatAm"}
    item = plan["rows"][args.row - 1]
    row = item["expense"]
    changes = dict(value.split("=", 1) for value in args.set if "=" in value)
    if "amount" in changes or "vat" in changes:
        if not args.evidence or not args.reason or not args.page or args.page < 1:
            raise ExpenseError("금액 수정에는 --evidence PDF --page 페이지 --reason 사유가 필요합니다.")
        amount = won(changes.get("amount", row["sumAm"]))
        vat = won(changes.get("vat", row["vatAm"]))
        if row["vatAm"] and "vat" not in changes:
            raise ExpenseError("부가세가 있는 거래의 금액 수정에는 vat도 명시하세요.")
        record_adjustment(plan, item, amount, vat, args.evidence, args.page, args.reason)
    for assignment in args.set:
        name, separator, value = assignment.partition("=")
        if not separator or name not in names:
            raise ExpenseError("수정 가능한 항목: " + ", ".join(names))
        if name in ("receipt-date", "pay-date"):
            value = date_value(value)
        if name in ("amount", "vat"):
            value = won(value)
        field = names[name]
        item["expense"][field] = value
        item["proposed"][field] = {"value": value, "source": "검토 후 명시 입력"}
        item["confirmedFields"] = sorted(set(item.get("confirmedFields", [])) | {field})
        if name in ("bank", "account", "holder"):
            item["expense"]["realDepositorFg"] = ""
            item["expense"]["realDepositorDc"] = ""
        for code, label in (("empCd", "empNm"), ("deptCd", "deptNm"), ("bankCd", "bankNm"), ("pjtCd", "pjtNm"), ("carCd", "carNb")):
            if field == code:
                item["expense"][label] = ""
    row["supAm"] = row["sumAm"] - row["vatAm"]
    item["issues"] = row_issues(item["expense"], plan["source"])
    write_private(args.out, plan)
    show_plan(plan)


def set_dates(args):
    plan = read_private(args.plan)
    start, end = plan["source"]["period"]["from"], plan["source"]["period"]["to"]
    if args.accounting_date == "month-end":
        if start[:6] != end[:6]:
            raise ExpenseError("여러 사용월에 걸친 거래에는 회계처리일을 직접 지정하세요.")
        accounting_date = end[:6] + str(calendar.monthrange(int(end[:4]), int(end[4:6]))[1])
    else:
        accounting_date = date_value(args.accounting_date)
    pay_date = (dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y%m%d")
                if args.pay_date == "today" else date_value(args.pay_date))
    client = Client()
    plan["source"]["gisu"] = client.post("/system/financialApi/selectGisuData", {"frDt": accounting_date})
    plan["source"]["dateRules"] = client.post("/personal/modulecommon/config/getConfig", {
        "syscfg": [{"moduleCd": "A", "ctrCd": key} for key in ("51", "60", "68", "Z3")]})
    plan["head"].update(isuDt=accounting_date, gisu=plan["source"]["gisu"]["gisu"])
    plan["source"]["accountingDate"] = accounting_date
    plan["dateChoice"] = {"accountingDate": accounting_date, "payDate": pay_date,
                          "source": "사용자 지정: 사용월 말일·검토안 작성일. 저장 재시도 시 날짜를 자동 갱신하지 않음"}
    for item in plan["rows"]:
        item["expense"]["payDt"] = pay_date
        item["issues"] = row_issues(item["expense"], plan["source"])
    write_private(args.out, plan)
    show_plan(plan)


def exclude(args):
    plan = read_private(args.plan)
    if not 1 <= args.row <= len(plan["rows"]) or not args.reason.strip():
        raise ExpenseError("제외할 행 번호와 사유를 지정하세요.")
    plan["rows"][args.row - 1]["excluded"] = args.reason
    write_private(args.out, plan)
    show_plan(plan)


def review_html(plan):
    escape = lambda value: html.escape(str(value if value is not None else ""))
    sections = []
    included = [item for item in plan["rows"] if not item.get("excluded")]
    summary = f"이번 결의 {len(included)}건 · {sum(item['expense']['sumAm'] for item in included):,}원"
    notes = "".join(f"<p>{escape(message)}</p>" for message in plan.get("unresolved", []))
    questions = inspect_plan(plan)["questions"]
    if questions:
        def describe(question):
            message = (f"{question['row']}행: " if question.get("row") else "") + question["message"]
            if question.get("merchant"):
                message += f" · {question['merchant']} {question['amount']:,}원"
            if question.get("options"):
                message += " · 과거 선택: " + ", ".join(f"{option.get('label', option['value'])} (승인 문서 {option['count']}건)" for option in question["options"])
            if question.get("candidateRows"):
                message += " · 연결 후보: " + ", ".join(str(index) + "행" for index in question["candidateRows"])
            return message
        notes += "<aside><strong>확인할 항목</strong><ul>" + "".join(
            "<li>" + escape(describe(question)) + "</li>" for question in questions) + "</ul></aside>"
    for index, (item, receipts) in enumerate(zip(plan["rows"], plan["source"]["receipts"]), 1):
        row, receipt = item["expense"], receipts[0]
        fields = [("승인일시", receipt.get("formatedIssDtTime") or row["cardDt"]),
                  ("카드", row.get("cardNm") or plan["source"].get("cardName")),
                  ("가맹점", receipt.get("chainName")), ("사업자번호", receipt.get("chainRegnb")),
                  ("승인번호", receipt.get("sunginNb")),
                  ("승인금액", f"{won(receipt['sunginAm']):,}원"),
                  ("이번 결의금액", f"{row['sumAm']:,}원"), ("적요 제안", row["rmkDc"] or "확인 필요"),
                  ("프로젝트", row.get("pjtNm") or row.get("pjtCd") or "확인 필요"),
                  ("증빙일자", row["issDt"]), ("지급요청일", row["payDt"] or "확인 필요"),
                  ("사원", row.get("empNm") or row["empCd"]), ("사용부서", row.get("deptNm") or row["deptCd"]),
                  ("은행", row.get("bankNm") or row["bankCd"]),
                  ("계좌", "•••• " + str(row["baNb"])[-4:] if row.get("baNb") else "미입력"),
                  ("예금주", row["depositorDc"] or "미입력"),
                  ("실명조회", "미확인"), ("업무용승용차", row.get("carNb") or row.get("carCd") or "미지정")]
        body = "".join(f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>" for label, value in fields)
        issues = "이번 결의 제외: " + item["excluded"] if item.get("excluded") else " / ".join(item["issues"]) or "필수 입력값 확인됨 · 업무 목적과 프로젝트를 검토하세요"
        adjustment = item.get("amountAdjustment")
        if adjustment:
            fields.extend([("금액 수정 근거", f"{Path(adjustment['evidencePath']).name} {adjustment['page']}쪽 · {adjustment['reason']}"),
                           ("승인금액 대비 차이", f"{row['sumAm'] - adjustment['originalRemaining']:+,}원")])
            body = "".join(f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>" for label, value in fields)
        query = {"MicroModuleCode": "personal", "coCd": row["coCd"], "issDt": row["cardDt"],
                 "issSq": row["cardSq"], "callComp": "APB1020PCard"}
        if row.get("cardFg") not in (None, ""):
            query["cardFg"] = row["cardFg"]
        receipt_url = ORIGIN + "/#/popup?" + urllib.parse.urlencode(query)
        sections.append(f"<section><h2>{index}. {escape(row['trNm'])}</h2><p>{escape(issues)}</p>"
                        f"<p><a href='{escape(receipt_url)}' target='_blank' rel='noopener noreferrer'>그룹웨어 원본 영수증 열기</a></p><table>{body}</table></section>")
    return ("<!doctype html><html lang='ko'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<title>지출결의 검토</title><style>body{max-width:850px;margin:40px auto;padding:0 20px;font:16px/1.6 sans-serif;color:#202b34}"
            "section{border-top:2px solid #202b34;margin:32px 0;padding:16px 0}table{border-collapse:collapse;width:100%}"
            "th,td{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid #ddd}th{width:150px}"
            "aside{padding:16px;background:#f3f4f5}p{color:#604619}</style>"
            f"<h1>{escape(plan['head']['docTitle'])}</h1>"
            f"<p>카드: {escape(plan['source'].get('cardName'))} · 회계처리일: {escape(plan['head']['isuDt'] or '확인 필요')}</p>"
            f"<p><strong>{escape(summary)}</strong></p>{notes}"
            "<aside>그룹웨어에서 조회한 카드 승인정보의 검토용 화면입니다. 원본 영수증 이미지나 제출용 증빙 파일이 아닙니다. "
            "각 거래의 승인일자·승인번호·금액을 대조했습니다. 추가 품목 영수증이 필요한 지출은 해당 원본을 별도로 확인하세요.</aside>"
            + "".join(sections) + "</html>")


def review(args):
    plan = read_private(args.plan)
    write_private_text(args.out, review_html(plan))
    print(f"검토 화면 생성: {args.out}")


def preflight(client, plan):
    if (str(plan["head"]["coCd"]) != str(client.erp["companyCode"])
            or str(plan["head"]["empCd"]) != str(client.erp["userCode"])):
        raise ExpenseError("검토 파일과 현재 로그인 계정이 다릅니다.")
    snapshot = plan["source"]
    if plan.get("statement", {}).get("charges"):
        statement = plan["statement"]
        month = snapshot["period"]["from"][:4] + "-" + snapshot["period"]["from"][4:6]
        original = read_statement(statement["file"], month)
        if any(original[key] != statement[key] for key in ("sha256", "count", "total", "charges")):
            raise ExpenseError("명세서 원본 또는 추출값이 변경됐습니다.")
    verify_completed(client, plan)
    identities = [item["identity"] for item in plan["rows"] if not item.get("excluded")]
    if len(identities) != len(set(identities)):
        raise ExpenseError("같은 카드 거래가 검토안에 중복되어 있습니다.")
    snapshot["accountingDate"] = plan["head"].get("isuDt", "")
    snapshot["dateRules"] = client.post("/personal/modulecommon/config/getConfig", {
        "syscfg": [{"moduleCd": "A", "ctrCd": key} for key in ("51", "60", "68", "Z3")]})
    if not snapshot.get("cardName"):
        raise ExpenseError("카드가 지정되지 않은 검토안입니다. --card를 지정하여 다시 조회하세요.")
    fresh = {identity(row): row for row in client.cards(snapshot["period"]["from"], snapshot["period"]["to"], snapshot["cardName"])}
    snapshot["projects"] = client.codepicker("SPJT_CODE_AUTH", empCd=client.erp["userCode"], useYn="ALL")
    snapshot["cashTypes"] = client.post("/personal/APCodePicker/ApAcashcdTypeCode", {
        "helpTy": "ACASHCD_TYPE_CODE", "langkind": "KOR", "multiCode": [int(snapshot["form"]["formId"])]})
    snapshot["payMethods"] = client.post(API + "0ap00041", {})
    rows, problems = [], list(plan.get("unresolved", []))
    if not plan["head"].get("isuDt"):
        problems.append("회계처리일 확인 필요")
    for index, item in enumerate(plan["rows"], 1):
        if item.get("excluded") or item.get("alreadySaved"):
            continue
        row = item["expense"]
        source = fresh.get(item["identity"])
        if source is None:
            raise ExpenseError(f"{index}행 카드 거래가 이미 반영됐거나 더 이상 조회되지 않습니다.")
        expected = (source["issDt"], str(source["issSq"]), str(source["sunginNb"]), source["cardCd"])
        actual = (row["cardDt"], str(row["cardSq"]), str(row["sunginNb"]), row["cardCd"])
        remaining = won(source["sunginAm"]) - won(source.get("appSumAm"))
        adjustment = item.get("amountAdjustment")
        if adjustment:
            if not any(rule["ctrCd"] == "68" and rule.get("useYn") == "1" for rule in snapshot["dateRules"]["syscfg"]):
                raise ExpenseError("현재 그룹웨어는 증빙 적용 금액 수정을 허용하지 않습니다.")
            if (hashlib.sha256(Path(adjustment["evidencePath"]).read_bytes()).hexdigest() != adjustment["sha256"]
                    or adjustment["amount"] != row["sumAm"] or adjustment["vat"] != row["vatAm"]
                    or adjustment["originalRemaining"] != remaining):
                raise ExpenseError(f"{index}행 금액 수정 근거 또는 원본 잔액이 변경됐습니다.")
        if actual != expected or (not adjustment and row["sumAm"] != remaining):
            raise ExpenseError(f"{index}행 증빙 식별자 또는 미반영 금액이 변경됐습니다. 다시 조회하세요.")
        if row.get("attrCd") != payment_attribute(source, snapshot["payMethods"]):
            raise ExpenseError(f"{index}행 지급수단 코드가 현재 그룹웨어 설정과 다릅니다. 다시 조회하세요.")
        verify_receipt(source, client.receipt(source))
        item["receiptVerified"] = True
        item["issues"] = row_issues(row, snapshot)
        problems.extend(f"{index}행: {message}" for message in item["issues"])
        rows.append(row)
    problems.extend(json.dumps(question, ensure_ascii=False) for question in inspect_plan(plan)["questions"]
                    if question["kind"] not in ("field", "confirmation"))
    if not rows and not any(item.get("alreadySaved") for item in plan["rows"]):
        problems.append("작성할 거래가 없습니다.")
    close_date = client.post(API + "0ap00050", {"divCd": plan["head"]["divCd"]})
    if plan["head"].get("isuDt") and close_date and close_date != "00000000" and date_value(plan["head"]["isuDt"]) <= str(close_date):
        problems.append("회계처리일자가 지출결의 마감일 이전입니다.")
    if problems:
        raise ExpenseError("\n".join(problems))
    if not rows:
        return []
    if any(rule["ctrCd"] == "Z3" and rule.get("useYn") == "1" for rule in snapshot["dateRules"]["syscfg"]):
        balances = client.post(API + "0ap00045", {"cardList": rows})
        by_key = {(row["cardDt"], str(row["cardSq"])): row for row in rows}
        for balance in balances:
            row = by_key.get((balance["cardDt"], str(balance["cardSq"])))
            if row is None:
                raise ExpenseError("카드 잔액 확인 결과에 알 수 없는 거래가 있습니다.")
            remaining = won(balance["balanceAm"])
            if (remaining >= 0 and row["sumAm"] > remaining) or (remaining < 0 and row["sumAm"] < remaining):
                raise ExpenseError("그룹웨어 잔액 통제 한도를 초과했습니다.")
    params = {"cardData": rows, "taxData": [], "cashData": []}
    for endpoint, label in (("0ap00033", "다른 메뉴 반영 여부"), ("0ap00034", "잔액")):
        if client.post(API + endpoint, params):
            raise ExpenseError(label + " 검증을 통과하지 못했습니다.")
    if client.post(API + "0ap00039", {"cardKeyList": [{"cardDt": row["cardDt"], "cardSq": row["cardSq"]} for row in rows]}):
        raise ExpenseError("카드 사용 권한 검증을 통과하지 못했습니다.")
    return rows


def check_plan(args):
    plan = read_private(args.plan)
    rows = preflight(Client(), plan)
    print(f"저장 대상 {len(rows)}건: 원본 영수증·명세서·필수 항목·프로젝트·카드 권한 검증 통과")


def native_binding(initial):
    process = initial["resultMap"].get("outProcessForm") or {}
    raw = process.get("outBindData")
    if not raw:
        raise ExpenseError("전자결재에 연결된 지출결의 본문 데이터가 없습니다.")
    return json.loads(raw)


def render_native(initial, uc, title, today):
    binding = native_binding(initial)
    def fill(template, values):
        def replace(match):
            attributes = {key.lower(): html.unescape(value) for key, _, value in
                          re.findall(r'''([\w-]+)\s*=\s*(["'])(.*?)\2''', match[0], re.S)}
            key = attributes.get("mapping_key")
            if key not in values:
                raise ExpenseError("전자결재 본문에 지원하지 않는 연결 항목이 있습니다.")
            value = values[key]
            style = html.escape(attributes.get("style", ""), quote=True)
            if isinstance(value, dict):
                href = value.get("href", "")
                if value.get("type") != "hyperlink" or not href.startswith("/#/popup?") or "callComp=APB1020PCard" not in href:
                    raise ExpenseError("지원하지 않는 전자결재 증빙 링크입니다.")
                return (f'<a href="{html.escape(href, quote=True)}" target="_blank" '
                        f'data-dze-jstype="win_open">{html.escape(str(value.get("title", "")))}</a>')
            if isinstance(value, (list, dict)):
                raise ExpenseError("지원하지 않는 전자결재 본문 값입니다.")
            return (f'<span style="{style}" mapping_key="{html.escape(key)}" '
                    f'data-prevtype="text">{html.escape(str(value if value is not None else ""))}</span>')
        return re.sub(r"<input\b[^>]*>", replace, template, flags=re.I)
    body = initial["mainhtml"]
    tables = list(re.finditer(r'(<table\b[^>]*mapping_key="table2"[^>]*>)(.*?)(</table>)', body, re.S | re.I))
    if len(tables) != 1 or re.search(r"<table\b", tables[0][2], re.I):
        raise ExpenseError("전자결재 반복 표 서식이 변경됐습니다.")
    table = tables[0]
    template_rows = list(re.finditer(r"<tr\b[^>]*>.*?</tr>", table[2], re.S | re.I))
    if len(template_rows) != 4 or any("mapping_key=" in row[0] for row in template_rows[:2]):
        raise ExpenseError("전자결재 행 서식이 변경됐습니다.")
    # ponytail: 현재 양식 1211의 두 줄짜리 반복 행만 지원. 양식 변경 시 실제 서식부터 재검증.
    row_template = table[2][template_rows[2].start():template_rows[3].end()]
    expanded = "".join(fill(row_template, {**binding["ITEMS"], **group["items"]})
                       for group in binding["TABLE"]["table2"]["group"])
    inner = table[2][:template_rows[2].start()] + expanded + table[2][template_rows[3].end():]
    body = fill(body[:table.start()] + table[1] + inner + table[3] + body[table.end():], binding["ITEMS"])
    values = {"_DF01_": "", "_DF02_": today, "_DF03_": uc["deptName"],
              "_DF04_": uc["empName"], "_DF10_": title}
    result = initial["html"]
    for token, value in values.items():
        result = result.replace(token, html.escape(str(value)))
    result = result.replace("_DF11_", '<div id="divInterJson">' + body + "</div>")
    if re.search(r"_DF\w+_|<input\b", result, re.I):
        raise ExpenseError("전자결재 본문에 미완성 입력값이 있습니다.")
    return result


def verify_native_links(contents, rows):
    links = []
    for _, href in re.findall(r'''\bhref=(["'])(.*?)\1''', contents, re.S | re.I):
        href = html.unescape(href)
        if "callComp=APB1020PCard" not in href:
            continue
        if not href.startswith(("/#/popup?", ORIGIN + "/#/popup?")):
            raise ExpenseError("원본 그룹웨어 밖의 증빙 링크입니다.")
        values = urllib.parse.parse_qs(href.split("?", 1)[1])
        links.append(tuple(values.get(key, [""])[0] for key in ("coCd", "issDt", "issSq")))
    expected = [(str(row["coCd"]), row["cardDt"], str(row["cardSq"])) for row in rows]
    if sorted(links) != sorted(expected):
        raise ExpenseError("전자결재 본문의 영수증 링크가 원본 거래와 일치하지 않습니다.")


def eap_init(client, form_id, appro_key="", doc_id=0):
    return client.post("/eap/eap110A03", {"docID": doc_id, "formID": str(form_id), "approkey": appro_key,
                       "draftTp": "", "reDraft": "", "docType": "", "doc_auth": 0,
                       "pageCode": "UBAP001", "lineType": "", "appLineId": ""})


def eap_draft_payload(initial, client, plan, appro_key, contents, attachments=()):
    data = initial["resultMap"]
    if data.get("appdocinfo") or len(data["docnumtype"]) != 1 or not data["hidAppDocLine"]:
        raise ExpenseError("신규 전자결재 기본 설정을 확인할 수 없습니다.")
    uc, form = client.uc, data["form_info"]
    if str(form["form_id"]) != "1211" or str(data["outProcessInfo"]["contents_tp"]) != "3":
        raise ExpenseError("검증한 법인카드 양식만 임시저장할 수 있습니다.")
    params = {"doc_id": 0, "form_id": int(form["form_id"]), "numbering_id": data["docnumtype"][0]["cd_val"],
              "rep_dt": None, "repdt_mod_yn": "0", "co_id": uc["compSeq"], "dept_id": uc["deptSeq"],
              "biz_id": uc["bizSeq"], "user_id": uc["empSeq"], "co_nm": uc["compName"],
              "dept_nm": uc["deptName"], "user_nm": uc["empName"], "doc_title": plan["head"]["docTitle"],
              "doc_sts": "10", "inservice_time": str(form.get("inservice_life") or ""),
              "doc_level": form.get("doc_level") or "", "emergency_level": "", "doc_security": "0", "use_yn": "1",
              "approkey": appro_key, "contents_tp": "10", "doc_contents": urllib.parse.quote(contents, safe="~()*!.'-"),
              "bindData": json.dumps(data["outProcessForm"]["outBindData"], ensure_ascii=False),
              "interDivId": "divInterJson", "interDocTp": "json", "pTEAG_APPDOC_LINE": data["hidAppDocLine"],
              "pVKD_TKDDITEM": [], "pVCM_ATTACHFILEINFO": list(attachments), "pRefer": data["hidRefer"],
              "pReceive": data["hidReceive"], "pOper": data["hidOper"], "pTEAG_APPDOC_REF": [],
              "pTEAG_TOC_FOLDER": "", "pDraftTp": "", "seal_use_yn": "", "receipient": "", "receipt": "",
              "iframeHtml": "", "re_draft": "", "modifyFileList": [], "delFileSnList": [], "modifyItemList": [],
              "auditorYn": "1" if any(data["propValue"].get("p" + str(line["act_id"]) + "1099") for line in data["hidAppDocLine"]) else "0",
              "isLatestVerContentsFile": True, "formLang": data["selectedFormLang"],
              "aiVerifyHistories": [], "aiVerifyAutoOnSubmit": False, "aiVerifyUseYn": "0"}
    for field in ("AppLineYn", "Receive10", "Receive20", "Receive30", "Receive40", "Title", "Content", "Ref",
                  "Attach", "AddItem", "Inservice", "Doclevel", "Emergency", "Seal", "Eabox"):
        params["modify" + field] = "Y"
    params["modifyDocInfo"] = {
        "docId": 0,
        "appdoc": {field: params[field] for field in ("inservice_time", "doc_level", "doc_security", "emergency_level", "doc_title")},
        "appdocReceiveList": [{"receive_div": division, "org_div": row.get("org_div"), "org_id": row.get("org_id"),
                               "appline_d_seq": row.get("app_line_d_seq")}
                              for field, division in (("hidOper", "40"), ("hidReceive", "30" if data["propValue"].get("p1052") == "1119" else "20"), ("hidRefer", "10")) for row in data[field]],
        "appdocLineList": [{key: row[key] for key in ("doc_line_m_seq", "doc_line_s_seq", "act_id", "co_id", "dept_id", "user_id", "doc_line_gb") if key in row} for row in data["hidAppDocLine"]],
        "appdocColumnList": [], "appdocFileList": [{"fileSeq": index, "fileId": file["fileId"]} for index, file in enumerate(attachments)],
        "appdocFolderList": [], "appdocRefList": []}
    return {"paramItem": params, "pageCode": "UBAP001"}


def verify_saved(client, journal):
    expected = journal["plan"]
    if str(expected["head"]["coCd"]) != str(client.erp["companyCode"]) or str(expected["head"]["empCd"]) != str(client.erp["userCode"]):
        raise ExpenseError("다른 사용자 작업 기록입니다.")
    saved = client.document(journal["linkKey"])
    if str(saved["head"]["approState"]) != "5" or len(saved["detail"]) != len(expected["rows"]):
        raise ExpenseError(f"ERP 상태={saved['head']['approState']}, 행 수={len(saved['detail'])}. 임시보관 상태 5와 원래 행 수가 필요합니다.")
    for field in ("isuDt", "docTitle", "empCd", "deptCd", "divCd"):
        if str(saved["head"].get(field) or "") != str(expected["head"].get(field) or ""):
            raise ExpenseError("저장한 문서의 기본 정보가 검토안과 다릅니다: " + field)
    actual = {(row["cardDt"], str(row["cardSq"])): row for row in saved["detail"]}
    for item in expected["rows"]:
        row = item["expense"]
        loaded = actual.get((row["cardDt"], str(row["cardSq"])))
        if not loaded:
            raise ExpenseError("저장한 거래의 증빙 식별자가 없습니다.")
        for field in ("sumAm", "supAm", "vatAm", "issDt", "payDt", "cashCd", "rmkDc", "pjtCd", "trCd", "empCd", "deptCd", "bankCd", "baNb", "depositorDc", "carCd"):
            left, right = loaded.get(field), row.get(field)
            differs = won(left) != won(right) if field in ("sumAm", "supAm", "vatAm") else str(left or "") != str(right or "")
            if differs:
                raise ExpenseError("저장 후 재조회 값이 다릅니다: " + field)
    for transaction in expected["source"]["cards"]:
        verify_receipt(transaction, client.receipt(transaction))
    eap = eap_init(client, expected["source"]["form"]["formId"], journal["approKey"], journal["docId"])
    info = eap["resultMap"]["appdocinfo"]
    if len(info) != 1 or str(info[0]["doc_sts"]) != "10" or info[0]["approkey"] != journal["approKey"]:
        raise ExpenseError("전자결재 임시보관 상태를 확인할 수 없습니다.")
    content = eap["resultMap"]["appdoccontent"]
    if len(content) != 1:
        raise ExpenseError("저장된 전자결재 본문을 확인할 수 없습니다.")
    contents = content[0]["doc_contents"]
    if "%3C" in contents[:30].upper():
        contents = urllib.parse.unquote(contents)
    verify_native_links(contents, saved["detail"])
    if journal.get("pdf"):
        attached = eap["resultMap"]["fileAttachInfo"]
        if len(attached) != 1 or not attached[0].get("fileKey"):
            raise ExpenseError("전자결재에 명세서 PDF가 연결되지 않았습니다.")
        auth = {"compSeq": client.uc["compSeq"], "empSeq": client.uc["empSeq"],
                "docId": journal["docId"], "migYn": "0"}
        params = {"moduleGbn": "EAP", "authKeyMap": json.dumps(auth), "fileSn": [attached[0]["fileKey"]]}
        metadata = client.post("/ecm/ecm001A04", {**params, "condition": "99"})["list"]
        if len(metadata) != 1 or metadata[0]["fileId"] != journal["attachments"][0]["fileId"]:
            raise ExpenseError("저장한 PDF의 파일 식별자가 다릅니다.")
        downloaded = client.post("/ecm/ecm001A03", {**params, "filename": Path(journal["pdf"]["path"]).name,
                                 "groupSeq": client.uc["groupSeq"]}, binary=True)
        if hashlib.sha256(downloaded).hexdigest() != journal["pdf"]["sha256"]:
            raise ExpenseError("저장 문서에서 다운로드한 PDF가 원본과 다릅니다.")
        journal["verifiedPdf"] = {"fileKey": attached[0]["fileKey"], "sha256": journal["pdf"]["sha256"], "bytes": len(downloaded)}
    journal["verifiedContent"] = contents
    journal["status"] = "verified"


def own_journals(client):
    for path in sorted((CONFIG / "drafts").glob("*.json")):
        journal = read_private(path)
        head = journal["plan"]["head"]
        if str(head["coCd"]) == str(client.erp["companyCode"]) and str(head["empCd"]) == str(client.erp["userCode"]):
            yield path, journal


def verify_completed(client, plan):
    verified = {}
    for item in plan["rows"]:
        saved = item.get("alreadySaved")
        if not saved:
            continue
        path = Path(saved["journal"]).resolve()
        if path.parent != (CONFIG / "drafts").resolve():
            raise ExpenseError("완료 기록이 작업 기록 폴더 밖을 가리킵니다.")
        if path not in verified:
            journal = read_private(path)
            recover_draft(client, journal)
            verified[path] = journal
        journal = verified[path]
        original = next((row for row in journal["plan"]["rows"] if row["identity"] == item["identity"]), None)
        if not original or original["expense"] != item["expense"] or journal["docId"] != saved["docId"]:
            raise ExpenseError("이미 저장된 거래의 입력값 또는 문서 연결이 변경됐습니다.")


def status_command(args):
    client = Client()
    results = []
    for path, journal in own_journals(client):
        error = None
        if args.verify:
            try:
                recover_draft(client, journal)
                write_private(path, journal, replace=True)
            except ExpenseError as exc:
                error = str(exc)
        results.append({"journal": str(path), "status": journal["status"], "docId": journal.get("docId"),
                        "title": journal["plan"]["head"]["docTitle"], "count": len(journal["identities"]),
                        "total": sum(row["expense"]["sumAm"] for row in journal["plan"]["rows"]),
                        "serverChecked": bool(args.verify and not error), "error": error})
    print(json.dumps(results, ensure_ascii=False, indent=2))


def continue_draft(client, journal, journal_path):
    """확인된 완료 단계는 건너뛴다. 결과가 불명확한 쓰기는 재전송하지 않는다."""
    def save(status=None):
        if status:
            journal["status"] = status
        write_private(journal_path, journal, replace=True)
    state = journal["status"]
    head = journal["plan"]["head"]
    if str(head["coCd"]) != str(client.erp["companyCode"]) or str(head["empCd"]) != str(client.erp["userCode"]):
        raise ExpenseError("다른 사용자 작업 기록입니다.")
    if journal.get("linkKey") and not journal.get("docId"):
        linked = client.post(API + "0ap00020", {"linkKey": journal["linkKey"]})
        matches = [row for row in linked if row.get("approKey") == journal["approKey"] and row.get("docId")]
        if len(matches) > 1:
            raise ExpenseError("하나의 연결키에 여러 문서가 있어 확인이 필요합니다.")
        if matches:
            if str(matches[0].get("empCd")) != str(client.erp["userCode"]):
                raise ExpenseError("다른 사용자의 문서 연결입니다.")
            journal["docId"] = int(matches[0]["docId"])
            save()
    if journal.get("docId") or state in ("eap_attempted", "eap_saved", "verified"):
        try:
            recover_draft(client, journal)
        finally:
            save()
        return
    if state not in ("prepared", "pdf_uploaded", "link_saved", "erp_saved"):
        raise ExpenseError(f"{state}: 쓰기 결과가 불명확합니다. 재전송하지 않았습니다. status --verify로 먼저 확인하세요.")
    plan, pdf = journal["plan"], journal.get("pdf")
    if pdf and hashlib.sha256(Path(pdf["path"]).read_bytes()).hexdigest() != pdf["sha256"]:
        raise ExpenseError("첨부할 PDF가 변경됐습니다.")
    if state != "erp_saved":
        validation = journal.get("validationPlan", plan)
        rows = preflight(client, validation)
        if sorted((row["cardDt"], str(row["cardSq"])) for row in rows) != sorted((item["expense"]["cardDt"], str(item["expense"]["cardSq"])) for item in plan["rows"]):
            raise ExpenseError("재개 대상 거래 구성이 변경됐습니다.")
        if any(row.get("bankCd") or row.get("baNb") or row.get("carCd") or str(row["empCd"]) != str(client.erp["userCode"]) or str(row["deptCd"]) != str(client.erp["deptCode"]) for row in rows):
            raise ExpenseError("본인·현재 부서의 계좌·차량 정보가 없는 카드 거래만 지원합니다.")
        by_key = {(row["cardDt"], str(row["cardSq"])): row for row in rows}
        for item in plan["rows"]:
            item["expense"] = copy.deepcopy(by_key[(item["expense"]["cardDt"], str(item["expense"]["cardSq"]))])
        save()
    form = plan["source"]["form"]
    if journal["status"] == "prepared":
        initial = eap_init(client, form["formId"])
        if initial["resultMap"].get("appdocinfo") or len(initial["resultMap"]["docnumtype"]) != 1:
            raise ExpenseError("신규 전자결재 양식 초기화를 확인할 수 없습니다.")
        if pdf:
            save("pdf_upload_attempted")
            try:
                uploaded = client.post("/ecm/ecm001A01", {"moduleGbn": "EAP"}, pdf=pdf["path"])
            finally:
                journal["uploadResponse"] = client.last_response
                save()
            if len(uploaded["list"]) != 1 or not uploaded["list"][0].get("fileId"):
                raise ExpenseError("PDF 업로드 결과를 확인할 수 없습니다. 재전송하지 않습니다.")
            journal["attachments"] = [{"fileId": uploaded["list"][0]["fileId"], "fileName": Path(pdf["path"]).stem,
                                       "fileExtsn": "pdf", "fileSize": pdf["bytes"], "noConvertFileSize": pdf["bytes"],
                                       "moduleGbn": "EAP", "authKeyMap": {"compSeq": client.uc["compSeq"],
                                       "empSeq": client.uc["empSeq"], "docId": 0, "migYn": "0"}}]
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
    detail = [{**item["expense"], "linkKey": journal["linkKey"], "lnSq": index, "divCd": head["divCd"]}
              for index, item in enumerate(plan["rows"], 1)]
    if journal["status"] == "link_saved":
        duplicates = client.post(API + "0ap00020", {"linkKey": journal["linkKey"]})
        if any(item["approKey"] != journal["approKey"] for item in duplicates):
            raise ExpenseError("채번된 문서 연결키가 중복됩니다.")
        if any(item.get("docId") for item in duplicates):
            recover_draft(client, journal)
            save()
            return
        save("erp_attempted")
        client.post(API + "0ap01001", {"consList": [], "head": head, "detail": detail, "budgetList": None})
        save("erp_saved")
    initial = eap_init(client, form["formId"], journal["approKey"])
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    contents = render_native(initial, client.uc, head["docTitle"], today)
    verify_native_links(contents, detail)
    payload = eap_draft_payload(initial, client, plan, journal["approKey"], contents, journal.get("attachments", []))
    journal["eapPayload"] = payload
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
        recover_draft(client, journal)
    finally:
        save()


def resume_command(args):
    client = Client()
    path = Path(args.journal).resolve()
    if path.parent != (CONFIG / "drafts").resolve():
        raise ExpenseError("이 계정의 작업 기록 폴더 안에 있는 JSON을 지정하세요.")
    with (CONFIG / "drafts" / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        journal = read_private(path)
        head = journal["plan"]["head"]
        if str(head["coCd"]) != str(client.erp["companyCode"]) or str(head["empCd"]) != str(client.erp["userCode"]):
            raise ExpenseError("다른 사용자 작업 기록입니다.")
        continue_draft(client, journal, path)
        print(f"임시보관 검증 완료: {journal['docId']}")


def recover_draft(client, journal):
    """저장 응답 대신 실제 연결 상태를 재조회한다. 쓰기는 재시도하지 않는다."""
    if not journal.get("docId") and journal.get("linkKey"):
        linked = client.post(API + "0ap00020", {"linkKey": journal["linkKey"]})
        matches = [row for row in linked if row.get("approKey") == journal["approKey"] and row.get("docId")]
        if len(matches) == 1 and str(matches[0].get("empCd")) == str(client.erp["userCode"]):
            journal["docId"] = int(matches[0]["docId"])
    if not journal.get("docId"):
        raise ExpenseError("저장 결과를 확인할 수 없습니다. 거래를 재전송하지 않았습니다.")
    verify_saved(client, journal)


def draft(args):
    client = Client()
    full_plan = read_private(args.plan)
    if inspect_plan(full_plan)["questions"]:
        raise ExpenseError("확인할 항목이 남았습니다. inspect 명령으로 질문을 확인하고 답변을 반영하세요.")
    plan = copy.deepcopy(full_plan)
    plan["rows"] = [item for item in plan["rows"] if not item.get("excluded") and not item.get("alreadySaved")]
    if not plan["rows"]:
        preflight(client, full_plan)
        print("전체 거래가 이미 임시보관되어 있습니다. 서버 재조회 완료, 새 문서 생성 없음.")
        return
    if any(item["expense"].get("fileInfoList") for item in plan["rows"]):
        raise ExpenseError("행별 추가 첨부파일은 아직 지원하지 않습니다. 문서 PDF 첨부를 사용하세요.")
    identities = [item["identity"] for item in plan["rows"]]
    if len(identities) != len(set(identities)):
        raise ExpenseError("같은 거래가 중복되어 있습니다.")
    source_pairs = [(row, receipt) for row, receipt in zip(plan["source"]["cards"], plan["source"]["receipts"])
                    if identity(row) in identities]
    if sorted(identity(row) for row, _ in source_pairs) != sorted(identities):
        raise ExpenseError("검토안과 원본 거래의 구성이 다릅니다.")
    plan["source"]["cards"] = [row for row, _ in source_pairs]
    plan["source"]["receipts"] = [receipt for _, receipt in source_pairs]
    pdf = None
    pdf_path = args.pdf or (plan["statement"]["file"] if plan.get("statement", {}).get("charges") else None)
    if pdf_path:
        path = Path(pdf_path).resolve()
        raw = path.read_bytes()
        if not raw.startswith(b"%PDF-"):
            raise ExpenseError("증빙 PDF를 확인하세요.")
        pdf = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    if any(item.get("amountAdjustment") and (not pdf or item["amountAdjustment"]["sha256"] != pdf["sha256"]) for item in plan["rows"]):
        raise ExpenseError("금액 수정 근거와 동일한 PDF를 첨부해야 합니다.")
    key = hashlib.sha256("|".join(sorted(identities)).encode()).hexdigest()
    directory = CONFIG / "drafts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    journal_path = directory / (key + ".json")
    # ponytail: 사용자 로컬 작업 기록에 전역 잠금. 다른 기기와의 공유는 서버 예약 기능 도입 시 추가.
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if journal_path.exists():
            journal = read_private(journal_path)
            if plan["head"] != journal["plan"]["head"] or [item["expense"] for item in plan["rows"]] != [item["expense"] for item in journal["plan"]["rows"]] or pdf != journal.get("pdf"):
                raise ExpenseError("이미 저장을 시도한 거래의 내용이 변경됐습니다. 기존 문서를 먼저 확인하세요.")
        else:
            for existing, previous in own_journals(client):
                if set(identities).intersection(previous["identities"]):
                    raise ExpenseError("기존 저장 시도와 겹칩니다. status로 기존 기록을 확인한 뒤 resume하세요.")
            appro_key = "ERP_" + "_".join(secrets.token_hex(2) for _ in range(5))
            journal = {"status": "prepared", "identities": identities, "approKey": appro_key, "plan": plan,
                       "validationPlan": full_plan}
            if pdf:
                journal["pdf"] = pdf
            write_private(journal_path, journal)
        continue_draft(client, journal, journal_path)
        print(f"임시보관 및 증빙 검증 완료: {journal['docId']} / {plan['head']['docTitle']}")


def authenticate(args):
    if args.status:
        if not SESSION.exists():
            raise ExpenseError("저장된 세션이 없습니다. README의 정상 로그인 안내 후 auth --clipboard를 실행하세요.")
        client = Client()
        client.flags("S0", "01")
        print(json.dumps({"authenticated": True, "employee": client.erp["userName"], "department": client.erp["deptName"]}, ensure_ascii=False))
        return
    if args.clipboard:
        if sys.platform != "darwin":
            raise ExpenseError("클립보드 인증은 macOS에서만 지원합니다. --stdin을 사용하세요.")
        raw = subprocess.run(["pbpaste"], capture_output=True, check=True).stdout.decode()
    elif args.stdin:
        raw = sys.stdin.read()
    else:
        raw = getpass.getpass("그룹웨어 사용자 세션 JSON (화면에 표시되지 않음): ")
    session = session_from_user_info(json.loads(raw))
    Client(session).flags("S0", "01")
    write_private(SESSION, session, replace=True)
    if args.clipboard:
        current = subprocess.run(["pbpaste"], capture_output=True, check=True).stdout.decode()
        if current == raw:
            subprocess.run(["pbcopy"], input=b"", check=True)
    print("인증 확인 완료. 세션을 사용자 전용 파일에 저장했습니다.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="정상 로그인 세션을 사용자 전용 파일에 저장")
    source = auth.add_mutually_exclusive_group()
    source.add_argument("--clipboard", action="store_true")
    source.add_argument("--stdin", action="store_true")
    source.add_argument("--status", action="store_true", help="토큰을 출력하지 않고 현재 인증 상태만 확인")
    auth.set_defaults(run=authenticate)
    command = commands.add_parser("profile", help="본인용 카드·기본 프로젝트·날짜 프로필 생성")
    command.add_argument("--card", required=True)
    command.add_argument("--project")
    command.add_argument("--out", required=True)
    command.set_defaults(run=profile_command)
    command = commands.add_parser("rule", help="명시적으로 확인한 영구 적요 규칙을 새 프로필에 저장")
    command.add_argument("profile")
    command.add_argument("--merchant", required=True)
    command.add_argument("--description", required=True)
    command.add_argument("--purpose", required=True)
    command.add_argument("--project")
    command.add_argument("--statement-merchant", help="명세서에 표시되는 같은 가맹점의 이름")
    command.add_argument("--out", required=True)
    command.set_defaults(run=rule_command)
    command = commands.add_parser("prepare", help="월·현대카드 PDF·개인 프로필로 검토안 및 질문 생성")
    command.add_argument("--month", required=True)
    command.add_argument("--pdf", required=True)
    command.add_argument("--profile", required=True)
    command.add_argument("--out", required=True)
    command.set_defaults(run=prepare)
    command = commands.add_parser("inspect", help="검토안의 건수·합계·확인 질문을 JSON으로 출력")
    command.add_argument("plan")
    command.set_defaults(run=lambda args: print(json.dumps(inspect_plan(read_private(args.plan)), ensure_ascii=False, indent=2)))
    command = commands.add_parser("context", help="모델 검토용 승인 시각·금액·과거 승인 적요와 프로젝트 조회")
    command.add_argument("plan")
    command.add_argument("--row", type=int, required=True)
    command.add_argument("--query", help="같은 카드의 과거 가맹점·적요 검색. 예: 택시, 야근, 출장")
    command.set_defaults(run=context_command)
    command = commands.add_parser("match", help="모호한 명세서 거래와 카드 원본의 연결을 이번 건에 한해 확인")
    command.add_argument("plan")
    command.add_argument("--charge", required=True)
    command.add_argument("--row", type=int, required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--out", required=True)
    command.set_defaults(run=match_command)
    command = commands.add_parser("status", help="본인 작업 기록·문서 ID·저장 상태를 JSON으로 조회")
    command.add_argument("--verify", action="store_true", help="서버 문서·원본 영수증·첨부 PDF까지 재조회")
    command.set_defaults(run=status_command)
    command = commands.add_parser("resume", help="저장 기록의 확인된 단계부터 이어가기")
    command.add_argument("journal")
    command.set_defaults(run=resume_command)
    command = commands.add_parser("preview", help="카드 원본·연결 영수증·승인 이력으로 검토안 생성")
    command.add_argument("--from", dest="start", required=True)
    command.add_argument("--to", dest="end", required=True)
    command.add_argument("--form", default="1211")
    command.add_argument("--card", required=True, help="그룹웨어 카드의 정확한 이름")
    command.add_argument("--accounting-date", default="", help="회계처리일. 미지정 시 검토만 가능")
    command.add_argument("--out", required=True)
    command.set_defaults(run=preview)
    command = commands.add_parser("edit", help="원본 증빙 연결을 유지하면서 입력값 수정")
    command.add_argument("plan")
    command.add_argument("--row", type=int, required=True)
    command.add_argument("--set", action="append", required=True)
    command.add_argument("--evidence", help="금액 수정 근거 PDF")
    command.add_argument("--page", type=int, help="근거 PDF 페이지")
    command.add_argument("--reason", help="금액 수정 사유")
    command.add_argument("--out", required=True)
    command.set_defaults(run=edit)
    command = commands.add_parser("dates", help="회계처리일·지급요청일 지정. today는 실행 시 한 번 확정")
    command.add_argument("plan")
    command.add_argument("--accounting-date", required=True, help="YYYY-MM-DD 또는 month-end")
    command.add_argument("--pay-date", required=True, help="YYYY-MM-DD 또는 today")
    command.add_argument("--out", required=True)
    command.set_defaults(run=set_dates)
    command = commands.add_parser("exclude", help="원본·영수증은 보존하고 이번 결의 대상에서 제외")
    command.add_argument("plan")
    command.add_argument("--row", type=int, required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--out", required=True)
    command.set_defaults(run=exclude)
    command = commands.add_parser("review", help="카드 승인정보와 입력값의 로컬 검토 화면 생성")
    command.add_argument("plan")
    command.add_argument("--out", required=True)
    command.set_defaults(run=review)
    command = commands.add_parser("check", help="현재 그룹웨어 데이터로 저장 전 검증")
    command.add_argument("plan")
    command.set_defaults(run=check_plan)
    command = commands.add_parser("draft", help="카드 지출을 임시보관하고 ERP·전자결재·영수증·PDF 재조회")
    command.add_argument("plan")
    command.add_argument("--pdf", help="문서에 첨부할 금액 수정 근거 PDF")
    command.set_defaults(run=draft)
    args = parser.parse_args()
    try:
        args.run(args)
    except (ExpenseError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        if isinstance(error, (ExpenseError, StatementError)):
            print(f"오류: {error}", file=sys.stderr)
        else:
            print(f"오류: 입력 또는 로컬 파일을 확인하세요 ({type(error).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
