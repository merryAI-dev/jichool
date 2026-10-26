# /// script
# requires-python = ">=3.10"
# dependencies = ["pypdfium2>=4.30", "pillow>=10"]
# ///
"""실행: python3 test_amaranth.py  (네트워크 없음, 가상 응답)"""
import io
import json
from pathlib import Path
import socket
import threading

import amaranth as am
from expense import ExpenseError


SESSION = {"token": "TEST-TOKEN", "signKey": "TEST-KEY",
           "erp": {"companyCode": "1000", "userCode": "TEST-EMP", "userName": "테스트", "deptName": "테스트팀"},
           "uc": {"groupSeq": "G", "compSeq": "C", "empSeq": "E", "langCode": "kr"}}


class Reply:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return io.BytesIO(self.body)

    def __exit__(self, *args):
        return False


class Opener:
    """요청을 기록하고 경로별 가상 응답을 돌려준다."""

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def open(self, request, timeout=None):
        path = request.full_url.replace(am.ORIGIN, "")
        self.calls.append((path, json.loads(request.data), dict(request.header_items())))
        return Reply(self.replies[path])


def client(replies):
    opener = Opener(replies)
    return am.ReadClient(SESSION, opener), opener


def check_mask():
    text = "주민 900101-1234567, 계좌 053-21-0123-818 / 11012345678901, 날짜 2025-10-28, 금액 300,000"
    masked = am.mask_text(text)
    assert "1234567" not in masked and "0123" not in masked and "12345678901" not in masked
    assert "2025-10-28" in masked and "300,000" in masked
    nested = am.mask({"rsrgNo": "9001011234567", "baNb": "11012345678901", "rows": [{"note": "9001011234567"}]})
    assert nested["rsrgNo"] == "######-#######" and "45678" not in json.dumps(nested)
    print("마스킹: 주민번호·계좌번호만 가리고 날짜·금액 유지 통과")


def check_search_and_view():
    contents = ("<table><tr><td>1</td><td>사업소득지급수수료</td><td>A_워크숍_연사비_홍길동</td></tr>"
                "<tr><td>국민</td><td>110-123-456789</td><td>홍길동</td></tr></table>")
    api, opener = client({
        "/eap/eap110A46": {"resultCode": 0, "resultData": {"totalCount": 1, "list": [
            {"DOC_ID": 7, "DOC_NO": "MYSC-2025-0001", "DOC_TITLE": "연사비", "FORM_ID": 1210, "FORM_NM": "지출결의서",
             "USER_NM": "동료", "REP_DT": "2025-10-28 15:25:41"}]}},
        "/eap/eap111A23": {"resultCode": 0, "resultData": {"doc_id": 7, "doc_no": "MYSC-2025-0001",
                                                           "doc_contents": contents, "inter_contents": ""}}})
    found = am.run(api, "document_search", {"doc_no": "MYSC-2025-0001", "date_from": "2025-01-01", "date_to": "2025-12-31"})
    assert found["documents"][0]["docId"] == 7
    path, sent, headers = opener.calls[0]
    assert sent["sDocNo"] == "MYSC-2025-0001" and sent["sfrDt"] == "2025-01-01" and "coCd" not in sent
    assert headers.get("Authorization") == "Bearer TEST-TOKEN" and headers.get("Wehago-sign")
    view = am.run(api, "document_view", {"doc_id": 7})
    assert opener.calls[1][1] == {"docId": 7, "docAuth": 0, "approkey": ""}
    assert view["body"][0][1] == "사업소득지급수수료" and view["body"][1][1] == "110-123-456789"
    assert "456789" not in json.dumps(am.mask(view))
    print("문서: 문서번호 검색·본문 표 변환·조회 파라미터 통과")


def check_guards():
    api, opener = client({})
    for name, args, word in (("document_view", {}, "필수"), ("document_search", {"docno": "x"}, "알 수 없는 인자"),
                             ("delete_everything", {}, "알 수 없는 작업")):
        try:
            am.run(api, name, args)
        except ExpenseError as error:
            assert word in str(error), error
        else:
            raise AssertionError(name)
    for path in ("/personal/APB1020New/0ap01001", "/eap/eap110A06", "/eap/eap111A05"):
        try:
            api.post(path, {})
        except ExpenseError:
            pass
        else:
            raise AssertionError("쓰기·읽음처리 경로가 열려 있습니다: " + path)
    assert not opener.calls
    print("보호: 필수·미지 인자, 카탈로그 밖 경로(저장·읽음처리) 차단 통과")


def check_rejection():
    api, _ = client({"/eap/eap110A84": {"resultCode": 2208, "resultMsg": "거절"}})
    try:
        am.run(api, "document_boxes")
    except ExpenseError as error:
        assert "거절" in str(error)
    else:
        raise AssertionError("거절 응답을 성공으로 처리했습니다")
    print("응답: resultCode 오류를 성공으로 넘기지 않음 통과")


def check_tools():
    for style, key in (("anthropic", "input_schema"), ("openai", "function")):
        tools = am.tool_schemas(style)
        assert len(tools) == len(am.OPS)
        names = [t["name"] if style == "anthropic" else t["function"]["name"] for t in tools]
        assert names == list(am.OPS) and all(key in t for t in tools)
    view = next(t for t in am.tool_schemas("anthropic") if t["name"] == "document_view")
    assert view["input_schema"]["required"] == ["doc_id"]
    here = Path(__file__).parent / "tools"
    for style in ("anthropic", "openai"):
        assert json.loads((here / f"{style}-tools.json").read_text()) == am.tool_schemas(style), \
            f"tools/{style}-tools.json이 카탈로그와 다릅니다. amaranth.py tools로 다시 생성하세요."
    print("도구: Claude·GPT 정의가 같은 카탈로그에서 생성·배포 파일 일치 통과")


def check_websocket():
    """CDP 로그인에 쓰는 최소 웹소켓 프레임: 마스킹·긴 길이·분할 수신."""
    left, right = socket.socketpair()
    payload = json.dumps({"id": 1, "result": {"result": {"value": "x" * 70000}}})
    def serve():
        data = payload.encode()
        right.sendall(bytes([0x01, 127]) + len(data[:40000]).to_bytes(8, "big") + data[:40000])
        right.sendall(bytes([0x80, 127]) + len(data[40000:]).to_bytes(8, "big") + data[40000:])
    threading.Thread(target=serve).start()
    assert am.ws_read(left) == payload
    frame = am.ws_frame("abc")
    assert frame[0] == 0x81 and frame[1] == 0x83
    key, body = frame[2:6], frame[6:]
    assert bytes(b ^ key[i % 4] for i, b in enumerate(body)) == b"abc"
    for url in ("ws://10.0.0.5:9222/devtools/page/x", "wss://127.0.0.1:9222/x"):
        try:
            am.cdp_evaluate(url, "1")
        except ExpenseError:
            pass
        else:
            raise AssertionError("원격 디버그 주소를 허용했습니다")
    left.close(), right.close()
    print("로그인: 웹소켓 프레임·분할 수신·로컬 주소 제한 통과")


if __name__ == "__main__":
    check_mask()
    check_search_and_view()
    check_guards()
    check_rejection()
    check_tools()
    check_websocket()
