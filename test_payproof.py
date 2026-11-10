# /// script
# requires-python = ">=3.10"
# dependencies = ["pypdfium2>=4.30", "pillow>=10", "xlrd>=2"]
# ///
"""실행: uv run test_payproof.py  (네트워크·실제 자료 없음)"""
from pathlib import Path
import tempfile

from PIL import Image

from expense import ExpenseError
import payproof as proof


def row(day, seq, amount, name="테스트클라우드코리아", used="N"):
    return {"coCd": "1000", "issDt": day, "issSq": seq, "cardFg": "1", "sunginAm": float(amount),
            "chainName": name, "appYn": used}


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def cards(self, start, end, card, include_applied=False):
        self.calls.append((start, end, card, include_applied))
        return self.rows


def check_find():
    client = FakeClient([row("20261102", 18, 182372), row("20261013", 51, 100000, used="Y"),
                         row("20261028", 4, 500000), row("20261015", 2, 12000, "다른가게")])
    result = proof.find_payments(client, "테스트 카드", "테스트 클라우드", "20261031", 282372, 31)
    assert client.calls == [("20260930", "20261201", "테스트 카드", True)]
    assert [item["date"] for item in result["candidates"]] == ["20261013", "20261028", "20261102"]
    assert len(result["matches"]) == 1 and [item["amount"] for item in result["matches"][0]] == [100000, 182372]
    assert result["matches"][0][0]["usedInExpense"] is True
    assert "issSq=51" in result["matches"][0][0]["receiptUrl"]
    print("찾기: 나뉜 결제 묶음·이미 쓴 거래 포함 통과")


def check_limit():
    try:
        proof.payment_sets([row("20261001", index, 1) for index in range(proof.MAX_MATCHES + 1)], 1)
    except ExpenseError as error:
        assert "좁혀" in str(error)
    else:
        raise AssertionError("후보가 많은데 조합을 계산했습니다")
    print("찾기: 후보 상한 통과")


def check_mark():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "전표.png"
        Image.new("RGB", (proof.WIDTH, proof.HEIGHT), "white").save(path)
        page = proof.mark_image(path)
        left, top = proof.CROP[:2]
        assert page.size == (proof.CROP[2] - left, proof.CROP[3] - top)
        assert page.getpixel((600 - left, 428 - top)) == proof.GRAY
        assert page.getpixel((proof.HIGHLIGHTS[0][0] + 1 - left, 100 - top)) == proof.RED
        assert page.getpixel((400 - left, 300 - top)) == (255, 255, 255)
        bad = Path(folder) / "다른크기.png"
        Image.new("RGB", (800, 600), "white").save(bad)
        try:
            proof.mark_image(bad)
        except ExpenseError as error:
            assert "다시 찍어" in str(error)
        else:
            raise AssertionError("다른 크기의 캡처를 처리했습니다")
    print("강조: 가림·빨간 테두리·크기 검사 통과")


if __name__ == "__main__":
    check_find()
    check_limit()
    check_mark()
