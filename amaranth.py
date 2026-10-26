#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""MYSC 그룹웨어(Amaranth 10) 조회 API 계층. Python 표준 라이브러리만 사용한다.

Claude 스킬과 GPT 함수 호출이 같은 카탈로그(OPS)를 쓴다.
- 이 모듈은 조회만 한다. 쓰기(임시보관 등)는 expense.py·taxinvoice.py의 작업 기록 흐름으로만 한다.
- 주민등록번호·계좌번호는 기본으로 가려서 내보낸다. 원문은 --raw-out 파일(0600)로만 저장한다.
- 로그인은 디버그 포트를 연 별도 Chrome 프로필에서 sessionStorage.userInfo를 읽어 온다(login).

API 근거: REA로 전자결재 모듈(/modules/eap)·개인 모듈(/modules/personal) 번들과 실제 화면 호출을 분석했다.
"""

import argparse
import base64
import datetime as dt
import html
import json
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from expense import (API, CONFIG, ORIGIN, SESSION, WINDOWS, ExpenseError, browser_path, read_private,
                     session_from_user_info, signed_headers, write_private)


CDP_DEFAULT = "http://127.0.0.1:9222"
BROWSER_PROFILE = CONFIG / "browser-profile"
RRN = re.compile(r"(?<!\d)\d{6}\s*-?\s*[1-8]\d{6}(?!\d)")
ACCOUNT = re.compile(r"(?<![\d-])(?:\d{2,6}-){2,3}\d{2,8}(?![\d-])|(?<!\d)\d{10,16}(?!\d)")


def mask_text(text):
    def account(match):
        # 날짜(2025-10-28)·전화번호처럼 짧은 숫자열은 두고, 숫자 10자리 이상만 계좌로 본다.
        if sum(char.isdigit() for char in match[0]) < 10:
            return match[0]
        return match[0][:3] + "*" * max(len(match[0]) - 6, 1) + match[0][-3:]
    return ACCOUNT.sub(account, RRN.sub("######-#######", str(text)))


def mask(value):
    """응답 전체에서 주민번호·계좌번호 형태를 가린다. 키 이름도 함께 본다."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = key.lower()
            if any(word in lowered for word in ("rsrgno", "identityno", "pplnb", "resno", "jumin")):
                out[key] = "######-#######" if item else item
            elif any(word in lowered for word in ("banb", "acntno", "acctno", "decryptbanb")) and item:
                digits = str(item)
                out[key] = digits[:3] + "*" * max(len(digits) - 6, 1) + digits[-3:]
            else:
                out[key] = mask(item)
        return out
    if isinstance(value, list):
        return [mask(item) for item in value]
    if isinstance(value, str):
        return mask_text(value)
    return value


def html_tables(contents):
    """전자결재 본문 HTML을 표의 행·칸 텍스트로 바꾼다."""
    contents = contents or ""
    if "%3C" in contents[:40].upper():
        contents = urllib.parse.unquote(contents)
    rows = []
    for row in re.findall(r"<tr\b.*?</tr>", contents, re.S | re.I):
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()
                 for cell in re.findall(r"<t[dh]\b.*?</t[dh]>", row, re.S | re.I)]
        if any(cells):
            rows.append(cells)
    return rows


# ---------------------------------------------------------------- 조회 작업 카탈로그

def _date(value, default):
    value = str(value or default).replace(".", "-")
    if re.fullmatch(r"\d{8}", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ExpenseError(f"날짜 형식이 아닙니다: {value}")
    return value


def _compact(value, default):
    return _date(value, default).replace("-", "")


def _today():
    return dt.date.today().isoformat()


def _year_ago():
    return (dt.date.today() - dt.timedelta(days=365)).isoformat()


def view_document(result, args):
    data = result or {}
    return {"docId": data.get("doc_id"), "docNo": data.get("doc_no"), "title": data.get("doc_title"),
            "formId": data.get("form_id"), "formName": data.get("form_nm"), "drafter": data.get("userNm"),
            "dept": data.get("deptNm"), "reportedAt": data.get("rep_dt"),
            "erpTable": html_tables(data.get("inter_contents")), "body": html_tables(data.get("doc_contents"))}


def erp_document(client, args):
    link_key = args["linkKey"]
    heads = client.post(API + "0ap00002", {"linkKey": link_key})
    rows = client.post(API + "0ap00003", {"linkKey": link_key, "isShowCardPrivacy": "", "isShowAccountPrivacy": ""})
    return {"head": heads[0] if len(heads) == 1 else heads, "detail": rows}


OPS = {
    "document_boxes": {
        "description": "전자결재 문서함 목록(상신·기결·수신참조·이전문서 등)과 menuId. document_search의 box에 쓴다.",
        "path": "/eap/eap110A84", "params": {}, "build": lambda a, c: {},
        "shape": lambda r, a: (r or {}).get("boxList", [])},
    "document_search": {
        "description": "열람 권한이 있는 전자결재 문서를 문서번호·제목·본문·기안자·문서함·기간으로 찾는다. "
                       "다른 사람이 기안한 문서도 결재·참조·공개 범위 안이면 나온다. 결과의 docId로 document_view를 호출한다.",
        "path": "/eap/eap110A46",
        "params": {"doc_no": {"type": "string", "description": "문서번호 (예: MYSC-2025-4940)"},
                   "title": {"type": "string", "description": "제목에 포함된 말"},
                   "content": {"type": "string", "description": "본문에 포함된 말"},
                   "drafter": {"type": "string", "description": "기안자 이름"},
                   "box": {"type": "string", "description": "document_boxes의 menuId. 비우면 전체"},
                   "date_from": {"type": "string", "description": "기안일 시작 YYYY-MM-DD (기본 1년 전)"},
                   "date_to": {"type": "string", "description": "기안일 끝 YYYY-MM-DD (기본 오늘)"},
                   "page": {"type": "integer", "description": "페이지 (기본 1)"},
                   "page_size": {"type": "integer", "description": "페이지 크기 (기본 20, 최대 100)"}},
        "build": lambda a, c: {"page": str(a.get("page") or 1), "pageSize": str(min(int(a.get("page_size") or 20), 100)),
                               "sfrDt": _date(a.get("date_from"), _year_ago()), "stoDt": _date(a.get("date_to"), _today()),
                               "sDocTp": "", "sTitle": a.get("title") or "", "sContents": a.get("content") or "",
                               "sUserNm": a.get("drafter") or "", "sDocNo": a.get("doc_no") or "",
                               "menuId": a.get("box") or "", "docId": None},
        "shape": lambda r, a: {"total": (r or {}).get("totalCount", 0),
                               "documents": [{"docId": d.get("DOC_ID"), "docNo": d.get("DOC_NO"), "title": d.get("DOC_TITLE"),
                                              "formId": d.get("FORM_ID"), "formName": d.get("FORM_NM"),
                                              "drafter": d.get("USER_NM"), "reportedAt": d.get("REP_DT")}
                                             for d in (r or {}).get("list", [])]}},
    "document_view": {
        "description": "전자결재 문서 한 건의 본문을 읽는다(조회만, 읽음 처리 호출 없음). "
                       "erpTable은 지출결의 행(계정·적요·증빙·프로젝트·거래처·금액), body는 수취 계좌·출금계좌·비고 등이다.",
        "path": "/eap/eap111A23",
        "params": {"doc_id": {"type": "integer", "description": "document_search 결과의 docId"}},
        "required": ["doc_id"],
        "build": lambda a, c: {"docId": int(a["doc_id"]), "docAuth": 0, "approkey": ""},
        "shape": view_document},
    "expense_list": {
        "description": "내가 기안한 지출결의(ERP 연동) 목록. formId 1210 세금계산서·외부인건비, 1211 법인카드, 1212 자금이체 등.",
        "path": API + "0ap00001",
        "params": {"date_from": {"type": "string", "description": "YYYY-MM-DD (기본 1년 전)"},
                   "date_to": {"type": "string", "description": "YYYY-MM-DD (기본 오늘)"},
                   "form_id": {"type": "string", "description": "양식 번호로 거르기 (예: 1210)"}},
        "build": lambda a, c: {"appDateFrom": _compact(a.get("date_from"), _year_ago()),
                               "appDateTo": _compact(a.get("date_to"), _today()), "docTitle": "", "fromAm": "",
                               "toAm": "", "docNo": "", "docStatus": "", "dateFg": "1"},
        "shape": lambda r, a: [{"formId": d.get("formId"), "state": d.get("approState"), "title": d.get("docTitle"),
                                "linkKey": d.get("linkKey"), "accountingDate": d.get("isuDt")}
                               for d in r or [] if not a.get("form_id") or str(d.get("formId")) == str(a["form_id"])]},
    "expense_document": {
        "description": "내가 기안한 지출결의 한 건의 ERP 머리·행 원본(용도·프로젝트·거래처·금액·계좌 코드). expense_list의 linkKey를 쓴다.",
        "params": {"linkKey": {"type": "string", "description": "expense_list의 linkKey"}},
        "required": ["linkKey"], "custom": erp_document},
    "purpose_codes": {
        "description": "양식별 용도(계정) 코드. deductFg 5는 원천징수 대상(0290 사업소득·0300 기타소득지급수수료).",
        "path": "/personal/APCodePicker/ApAcashcdTypeCode",
        "params": {"form_id": {"type": "integer", "description": "양식 번호 (기본 1210)"}},
        "build": lambda a, c: {"helpTy": "ACASHCD_TYPE_CODE", "langkind": "KOR", "multiCode": [int(a.get("form_id") or 1210)]},
        "shape": lambda r, a: [{k: d.get(k) for k in ("typeCd", "typeNm", "deductFg", "taxFg", "pjtCheckYn", "rmkDc")} for d in r or []]},
    "payment_methods": {
        "description": "지급수단(paymethodFg)별 증빙구분 코드(attrCd).",
        "path": API + "0ap00041", "params": {}, "build": lambda a, c: {},
        "shape": lambda r, a: r},
    "expense_forms": {
        "description": "사용 가능한 지출결의 양식 목록(formId·이름·사용 여부).",
        "path": "/eap/eap096A45", "params": {},
        "build": lambda a, c: {"header": {"pId": "", "groupSeq": c.uc["groupSeq"], "empSeq": c.uc["empSeq"]},
                               "body": {"companyInfo": {"compSeq": c.uc["compSeq"]}, "formDTp": "",
                                        "searchFormDTp": "APB1020", "langCode": c.uc["langCode"]}},
        "shape": lambda r, a: [{"formId": f.get("formId"), "name": f.get("formNm"), "use": f.get("useYn")} for f in r or []]},
}
READ_PATHS = frozenset(op["path"] for op in OPS.values() if "path" in op) | {API + "0ap00002", API + "0ap00003"}


class ReadClient:
    """카탈로그의 조회 경로만 서명해서 호출한다. 자동 재시도하지 않는다."""

    def __init__(self, session=None, opener=None):
        self.session = session if session is not None else read_private(SESSION)
        self.erp, self.uc = self.session["erp"], self.session["uc"]
        self.opener = opener or urllib.request.build_opener()

    def post(self, path, params):
        if path not in READ_PATHS:
            raise ExpenseError(f"조회 카탈로그에 없는 경로입니다: {path}")
        payload = dict(params)
        if path.startswith("/personal/"):
            payload.update(coCd=self.erp["companyCode"], vPCoCd=self.erp["companyCode"])
        headers = signed_headers(self.session["token"], self.session["signKey"], path,
                                 int(time.time()), secrets.token_hex(16))
        request = urllib.request.Request(ORIGIN + path, json.dumps(payload).encode(), headers, method="POST")
        try:
            with self.opener.open(request, timeout=30) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 601):
                raise ExpenseError(f"인증 만료(HTTP {error.code}). amaranth.py login으로 다시 연결하세요.") from None
            raise ExpenseError(f"그룹웨어 HTTP {error.code}: {path}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise ExpenseError(f"그룹웨어 응답을 확인할 수 없습니다: {path}") from None
        if not isinstance(result, dict) or str(result.get("resultCode")) != "0":
            message = result.get("resultMsg") if isinstance(result, dict) else ""
            raise ExpenseError(f"그룹웨어 요청 거절: {path} ({message})")
        return result.get("resultData")


def run(client, name, args=None):
    if name not in OPS:
        raise ExpenseError(f"알 수 없는 작업: {name}. 사용 가능: {', '.join(OPS)}")
    op, args = OPS[name], dict(args or {})
    unknown = set(args) - set(op["params"])
    if unknown:
        raise ExpenseError(f"{name}: 알 수 없는 인자 {', '.join(sorted(unknown))}")
    missing = [key for key in op.get("required", []) if args.get(key) in (None, "")]
    if missing:
        raise ExpenseError(f"{name}: 필수 인자 {', '.join(missing)}")
    if "custom" in op:
        return op["custom"](client, args)
    return op["shape"](client.post(op["path"], op["build"](args, client)), args)


def tool_schemas(style):
    """카탈로그를 Claude(tool use) 또는 OpenAI(function calling) 정의로 내보낸다."""
    tools = []
    for name, op in OPS.items():
        schema = {"type": "object", "properties": op["params"], "required": op.get("required", []),
                  "additionalProperties": False}
        if style == "openai":
            tools.append({"type": "function", "function": {"name": name, "description": op["description"],
                                                           "parameters": schema}})
        else:
            tools.append({"name": name, "description": op["description"], "input_schema": schema})
    return tools


# ---------------------------------------------------------------- 로그인: 디버그 Chrome에서 세션 읽기

def ws_frame(payload, opcode=1):
    data = payload.encode() if isinstance(payload, str) else payload
    key = secrets.token_bytes(4)
    head = bytes([0x80 | opcode])
    if len(data) < 126:
        head += bytes([0x80 | len(data)])
    elif len(data) < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", len(data))
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", len(data))
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(data))


def ws_read(sock):
    def exact(count):
        buffer = b""
        while len(buffer) < count:
            chunk = sock.recv(count - len(buffer))
            if not chunk:
                raise ExpenseError("브라우저 연결이 끊겼습니다.")
            buffer += chunk
        return buffer
    message = b""
    while True:
        first, second = exact(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", exact(8))[0]
        mask_key = exact(4) if second & 0x80 else None
        data = exact(length)
        if mask_key:
            data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        if first & 0x0F == 8:
            raise ExpenseError("브라우저가 연결을 닫았습니다.")
        if first & 0x0F in (0, 1, 2):
            message += data
            if first & 0x80:
                return message.decode("utf-8")


def cdp_evaluate(ws_url, expression):
    parsed = urllib.parse.urlparse(ws_url)
    if parsed.scheme != "ws" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ExpenseError("로컬 디버그 브라우저(127.0.0.1)만 연결합니다.")
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
    try:
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        sock.sendall((f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += sock.recv(1024)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise ExpenseError("브라우저 디버그 연결을 열 수 없습니다.")
        sock.sendall(ws_frame(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                          "params": {"expression": expression, "returnByValue": True}})))
        while True:
            reply = json.loads(ws_read(sock))
            if reply.get("id") == 1:
                return ((reply.get("result") or {}).get("result") or {}).get("value")
    finally:
        sock.close()


def browser_session(cdp=CDP_DEFAULT):
    parsed = urllib.parse.urlparse(cdp)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ExpenseError("로컬 디버그 브라우저(127.0.0.1)만 연결합니다.")
    try:
        targets = json.load(urllib.request.urlopen(cdp.rstrip("/") + "/json/list", timeout=5))
    except (urllib.error.URLError, OSError):
        raise ExpenseError("디버그 브라우저가 없습니다. amaranth.py browser로 연 창에서 그룹웨어에 로그인하세요.") from None
    pages = [t for t in targets if t.get("type") == "page" and urllib.parse.urlparse(t.get("url", "")).netloc == "gw.mysc.co.kr"]
    for page in pages:
        raw = cdp_evaluate(page["webSocketDebuggerUrl"], "sessionStorage.userInfo || ''")
        if raw:
            return session_from_user_info(raw)
    raise ExpenseError("로그인된 그룹웨어 탭이 없습니다. 디버그 브라우저 창에서 그룹웨어에 로그인하세요.")


def login_command(args):
    session = browser_session(args.cdp)
    ReadClient(session).post("/eap/eap110A84", {})
    write_private(SESSION, session, replace=True)
    print(json.dumps({"connected": True, "employee": session["erp"]["userName"],
                      "department": session["erp"]["deptName"]}, ensure_ascii=False))


def browser_command(args):
    """사용자 Chrome·Edge와 분리된 프로필로 디버그 창을 연다. 로그인은 사용자가 직접 한다."""
    browser = browser_path()
    BROWSER_PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    port = urllib.parse.urlparse(args.cdp).port or 9222
    subprocess.Popen([browser, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                      f"--user-data-dir={BROWSER_PROFILE}", "--no-first-run", "--no-default-browser-check", ORIGIN],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=not WINDOWS)
    print(f"디버그 브라우저를 열었습니다(포트 {port}). 그 창에서 그룹웨어에 로그인한 뒤 login을 실행하세요.")


# ---------------------------------------------------------------- CLI

def call_command(args):
    params = json.loads(args.params) if args.params else {}
    result = run(ReadClient(), args.op, params)
    if args.raw_out:
        write_private(Path(args.raw_out).expanduser(), result, replace=True)
    print(json.dumps(mask(result), ensure_ascii=False, indent=2))


def ops_command(args):
    for name, op in OPS.items():
        print(f"{name}: {op['description']}")


def tools_command(args):
    print(json.dumps(tool_schemas(args.format), ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("browser", help="분리된 프로필의 디버그 Chrome 창 열기 (로그인은 사용자가)")
    command.add_argument("--cdp", default=CDP_DEFAULT)
    command.set_defaults(run=browser_command)
    command = commands.add_parser("login", help="디버그 Chrome의 로그인 세션을 가져와 저장")
    command.add_argument("--cdp", default=CDP_DEFAULT)
    command.set_defaults(run=login_command)
    command = commands.add_parser("ops", help="조회 작업 목록")
    command.set_defaults(run=ops_command)
    command = commands.add_parser("call", help="조회 작업 실행 (출력은 주민번호·계좌번호 마스킹)")
    command.add_argument("op")
    command.add_argument("params", nargs="?", help="JSON 인자. 예: '{\"doc_no\": \"MYSC-2025-4940\"}'")
    command.add_argument("--raw-out", help="마스킹 전 결과를 이 파일(0600)에만 저장")
    command.set_defaults(run=call_command)
    command = commands.add_parser("tools", help="Claude/GPT 도구 정의 내보내기")
    command.add_argument("--format", choices=("anthropic", "openai"), default="anthropic")
    command.set_defaults(run=tools_command)
    args = parser.parse_args()
    try:
        args.run(args)
    except (ExpenseError, OSError, ValueError, KeyError) as error:
        message = str(error) if isinstance(error, ExpenseError) else f"입력 또는 로컬 파일을 확인하세요 ({type(error).__name__})."
        print(f"오류: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
