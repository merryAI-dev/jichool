"""현대카드 인쇄 PDF의 텍스트 표를 읽는다. 원본 파일은 변경하지 않는다."""
import datetime as dt
import hashlib
from pathlib import Path
import re
import statistics
import subprocess
import unicodedata
import xml.etree.ElementTree as ET


class StatementError(ValueError):
    pass


def merchant_key(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"주식회사|\(주\)", "", value)
    return re.sub(r"[^a-z0-9가-힣]", "", value)


def same_merchant(left, right, aliases=()):
    left, right = merchant_key(left), merchant_key(right)
    if not left or not right:
        return False
    if left == right or (min(len(left), len(right)) >= 4 and (left in right or right in left)):
        return True
    return any(left == merchant_key(pair["statement"]) and right == merchant_key(pair["card"])
               for pair in aliases)


def read_statement(path, month):
    path = Path(path).resolve(strict=True)
    raw = path.read_bytes()
    if not raw.startswith(b"%PDF-") or not re.fullmatch(r"\d{4}-\d{2}", month):
        raise StatementError("PDF 파일과 YYYY-MM 형식의 사용월을 지정하세요.")
    dt.datetime.strptime(month, "%Y-%m")
    try:
        output = subprocess.run(["pdftotext", "-bbox", str(path), "-"], capture_output=True, check=True, timeout=30)
        root = ET.fromstring(output.stdout)
    except FileNotFoundError:
        raise StatementError("pdftotext가 필요합니다. macOS: brew install poppler") from None
    except (subprocess.SubprocessError, ET.ParseError):
        raise StatementError("PDF 텍스트를 읽지 못했습니다. 암호·손상·스캔 여부를 확인하세요.") from None
    ns = {"x": "http://www.w3.org/1999/xhtml"}
    charges, summaries = [], []
    for page_no, page in enumerate(root.findall(".//x:page", ns), 1):
        words = [{"text": unicodedata.normalize("NFKC", word.text or ""),
                  **{key: float(word.get(key)) for key in ("xMin", "xMax", "yMin", "yMax")}}
                 for word in page.findall("x:word", ns)]
        headers = {}
        for name in ("이용일", "가맹점", "할부개월", "청구회차", "청구금액"):
            found = [w for w in words if w["text"] == name]
            if len(found) != 1:
                raise StatementError(f"{page_no}쪽: 현대카드 인쇄 표의 '{name}' 열을 식별할 수 없습니다.")
            headers[name] = found[0]
        centers = {key: (word["xMin"] + word["xMax"]) / 2 for key, word in headers.items()}
        if list(centers.values()) != sorted(centers.values()):
            raise StatementError("명세서 열 순서가 변경됐습니다.")
        merchant_left = (centers["이용일"] + centers["가맹점"]) / 2
        merchant_right = (centers["가맹점"] + centers["할부개월"]) / 2
        dates = sorted([w for w in words if w["xMax"] < merchant_left
                        and w["yMin"] > headers["이용일"]["yMax"]
                        and re.fullmatch(r"\d{6}|\d{8}", w["text"])], key=lambda w: w["yMin"])
        if not dates:
            raise StatementError(f"{page_no}쪽에 읽을 수 있는 거래 일자가 없습니다.")
        height = statistics.median(w["yMax"] - w["yMin"] for w in dates)
        def merchant_words(y):
            return [w for w in words if merchant_left < (w["xMin"] + w["xMax"]) / 2 < merchant_right
                    and y - 2 * height <= w["yMin"] <= y + 1.4 * height]
        def billed(y):
            amounts = [w for w in words if abs((w["xMin"] + w["xMax"]) / 2 - centers["청구금액"]) < 2 * height
                       and -height / 2 <= w["yMin"] - y < 2.5 * height
                       and re.fullmatch(r"-?[\d,]+", w["text"])]
            if len(amounts) != 1:
                raise StatementError(f"{page_no}쪽: 청구금액을 한 개로 식별할 수 없습니다.")
            return int(amounts[0]["text"].replace(",", ""))
        for word in dates:
            date = (month[:2] + word["text"]) if len(word["text"]) == 6 else word["text"]
            dt.datetime.strptime(date, "%Y%m%d")
            if date[:6] != month.replace("-", ""):
                raise StatementError("명세서에 지정한 사용월 밖의 거래가 있습니다. 사용월별 명세서를 지정하세요.")
            y = word["yMin"]
            for column in ("할부개월", "청구회차"):
                values = [w["text"] for w in words if abs((w["xMin"] + w["xMax"]) / 2 - centers[column]) < height
                          and abs(w["yMin"] - y) < height / 2]
                if values != ["0"]:
                    raise StatementError("할부·청구회차가 있는 거래는 자동 처리하지 않습니다.")
            lines = []
            for entry in sorted(merchant_words(y), key=lambda w: w["yMin"]):
                if re.fullmatch(r"-?[\d,]+", entry["text"]) and entry["yMin"] < y - height:
                    continue
                if not lines or entry["yMin"] - lines[-1][0]["yMin"] > height * 0.8:
                    lines.append([])
                lines[-1].append(entry)
            name = "".join(w["text"] for line in lines for w in sorted(line, key=lambda w: w["xMin"]))
            if not merchant_key(name):
                raise StatementError("가맹점명이 비어 있습니다.")
            currencies = [w["text"] for w in words if re.fullmatch(r"[A-Z]{3}", w["text"])
                          and abs((w["xMin"] + w["xMax"]) / 2 - centers["할부개월"]) < 2 * height
                          and height < w["yMin"] - y < 3.5 * height]
            if len(currencies) > 1:
                raise StatementError("외화 통화를 식별할 수 없습니다.")
            charges.append({"id": f"p{page_no}r{len(charges) + 1}", "page": page_no, "date": date,
                            "merchant": name, "amount": billed(y), "currency": currencies[0] if currencies else "KRW"})
        for word in words:
            if word["text"] == "총합계":
                counts = [w["text"] for w in merchant_words(word["yMin"])
                          if re.fullmatch(r"\d+", w["text"]) and 0 <= w["yMin"] - word["yMin"] <= height]
                if len(counts) != 1:
                    raise StatementError("총합계의 건수를 식별할 수 없습니다.")
                summaries.append((int(counts[0]), billed(word["yMin"])))
    if len(summaries) != 1 or not charges or summaries[0] != (len(charges), sum(row["amount"] for row in charges)):
        raise StatementError("추출한 거래 건수·금액이 PDF의 총합계와 다릅니다.")
    # ponytail: 텍스트가 있는 현대카드 인쇄 표만 지원. 다른 서식은 실제 파일로 별도 검증 후 추가.
    return {"format": "hyundai-print-v1", "file": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "count": len(charges), "total": summaries[0][1], "charges": charges}


def match_candidates(charge, rows, aliases=()):
    same_date = [row for row in rows if row["expense"]["cardDt"] == charge["date"] and not row.get("excluded")]
    named = [row for row in same_date if same_merchant(charge["merchant"], row["expense"]["trNm"], aliases)]
    exact = [row for row in named if row["expense"]["sumAm"] == charge["amount"]]
    if exact:
        return exact, "날짜·가맹점·금액 일치"
    if charge["currency"] != "KRW" and charge["amount"] > 0:
        foreign = [row for row in named if row["expense"]["sumAm"] > 0 and not row["expense"]["vatAm"]]
        if foreign:
            return foreign, "해외 거래 청구액 차이"
    return [row for row in same_date if row["expense"]["sumAm"] == charge["amount"]], "가맹점 확인 필요"
