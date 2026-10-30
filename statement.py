"""현대카드 인쇄 PDF의 텍스트 표를 읽는다. 원본 파일은 변경하지 않는다."""
import datetime as dt
import hashlib
from pathlib import Path
import re
import statistics
import unicodedata

import pdftext


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
    """현대카드 명세서. 인쇄 PDF와 홈페이지에서 내려받은 엑셀(.xls·.xlsx)을 모두 읽는다."""
    path = Path(path).resolve(strict=True)
    raw = path.read_bytes()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise StatementError("YYYY-MM 형식의 사용월을 지정하세요.")
    dt.datetime.strptime(month, "%Y-%m")
    if raw.startswith(b"%PDF-"):
        return _read_pdf(path, raw, month)
    if raw.startswith(b"\xd0\xcf\x11\xe0") or raw.startswith(b"PK"):
        return _read_sheet(path, raw, month)
    raise StatementError("현대카드 명세서 PDF나 엑셀(.xls·.xlsx) 파일을 지정하세요.")


def _sheet_rows(path, raw):
    """엑셀 첫 시트의 행들. .xls는 xlrd, .xlsx는 표준 라이브러리(zip·XML)로 읽는다."""
    if raw.startswith(b"PK"):
        import zipfile
        from xml.etree import ElementTree
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with zipfile.ZipFile(path) as book:
            shared = []
            if "xl/sharedStrings.xml" in book.namelist():
                for item in ElementTree.fromstring(book.read("xl/sharedStrings.xml")).findall("m:si", ns):
                    shared.append("".join(node.text or "" for node in item.iter(f"{{{ns['m']}}}t")))
            sheet = ElementTree.fromstring(book.read("xl/worksheets/sheet1.xml"))
        rows = []
        for row in sheet.iter(f"{{{ns['m']}}}row"):
            cells = {}
            for cell in row.findall("m:c", ns):
                column = re.sub(r"\d", "", cell.get("r"))
                index = 0
                for char in column:
                    index = index * 26 + ord(char) - 64
                value = cell.find("m:v", ns)
                text = "".join(node.text or "" for node in cell.iter(f"{{{ns['m']}}}t"))
                if cell.get("t") == "s" and value is not None:
                    text = shared[int(value.text)]
                elif value is not None:
                    text = value.text or ""
                cells[index - 1] = text
            rows.append([cells.get(i, "") for i in range(max(cells, default=-1) + 1)])
        return rows
    try:
        import xlrd
    except ImportError:
        raise StatementError("엑셀(.xls) 명세서를 읽으려면 xlrd가 필요합니다. `uv run`으로 실행하세요.") from None
    try:
        sheet = xlrd.open_workbook(file_contents=raw).sheet_by_index(0)
    except xlrd.XLRDError:
        raise StatementError("엑셀 명세서를 열지 못했습니다. 암호가 걸려 있으면 풀어서 다시 저장하세요.") from None
    return [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]


def _read_sheet(path, raw, month):
    """현대카드 홈페이지 '청구내역상세' 엑셀. 승인번호가 있어 카드 원본과 정확히 짝지을 수 있다."""
    rows = [[unicodedata.normalize("NFKC", str(cell)).strip() for cell in row] for row in _sheet_rows(path, raw)]
    header_at = next((i for i, row in enumerate(rows) if "이용일자" in row and "가맹점" in row), None)
    if header_at is None:
        raise StatementError("엑셀에서 '이용일자'·'가맹점' 머리글을 찾을 수 없습니다. 현대카드 청구내역 엑셀인지 확인하세요.")
    header = rows[header_at]
    def column(*names):
        found = [header.index(name) for name in names if name in header]
        return found[0] if found else None
    date_at, merchant_at = column("이용일자"), column("가맹점")
    amount_at = column("국내청구금액", "청구금액", "이용금액", "국내이용금액")
    currency_at, approval_at = column("통화코드"), column("승인번호")
    if amount_at is None:
        raise StatementError("엑셀에서 청구금액 열을 찾을 수 없습니다.")
    def number(text):
        text = text.replace(",", "")
        if not re.fullmatch(r"-?\d+(\.0+)?", text):
            raise StatementError(f"엑셀 금액을 읽을 수 없습니다: {text}")
        return int(float(text))
    charges = []
    for index, row in enumerate(rows[header_at + 1:], header_at + 2):
        if len(row) <= max(date_at, merchant_at, amount_at) or not row[date_at]:
            continue
        digits = re.sub(r"\D", "", row[date_at])
        if len(digits) != 8:
            continue  # 합계·안내 행
        dt.datetime.strptime(digits, "%Y%m%d")
        if digits[:6] != month.replace("-", ""):
            raise StatementError("명세서에 지정한 사용월 밖의 거래가 있습니다. 사용월별 명세서를 지정하세요.")
        currency = row[currency_at] if currency_at is not None else ""
        charge = {"id": f"x{index}", "page": index, "date": digits, "merchant": row[merchant_at],
                  "amount": number(row[amount_at]),
                  "currency": currency if re.fullmatch(r"[A-Z]{3}", currency or "") else "KRW"}
        if approval_at is not None and row[approval_at]:
            charge["approval"] = row[approval_at]
        charges.append(charge)
    if not charges:
        raise StatementError("엑셀에서 거래 행을 찾을 수 없습니다.")
    return {"format": "hyundai-excel-v1", "file": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "count": len(charges), "total": sum(charge["amount"] for charge in charges), "charges": charges}


def _read_pdf(path, raw, month):
    try:
        document = pdftext.pages(path)
    except pdftext.PdfTextError as error:
        raise StatementError(str(error)) from None
    charges, summaries = [], []
    for page_no, page in enumerate(document, 1):
        words = [{**word, "text": unicodedata.normalize("NFKC", word["text"])} for word in page]
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
    if charge.get("approval"):  # 엑셀 명세서: 승인번호가 같으면 가맹점 표기가 달라도 같은 거래다.
        approved = [row for row in same_date
                    if str(row["expense"].get("sunginNb") or "").lstrip("0") == charge["approval"].lstrip("0")]
        if len(approved) == 1:
            return approved, "날짜·승인번호 일치"
    named = [row for row in same_date if same_merchant(charge["merchant"], row["expense"]["trNm"], aliases)]
    exact = [row for row in named if row["expense"]["sumAm"] == charge["amount"]]
    if exact:
        return exact, "날짜·가맹점·금액 일치"
    if charge["currency"] != "KRW" and charge["amount"] > 0:
        foreign = [row for row in named if row["expense"]["sumAm"] > 0 and not row["expense"]["vatAm"]]
        if foreign:
            return foreign, "해외 거래 청구액 차이"
    return [row for row in same_date if row["expense"]["sumAm"] == charge["amount"]], "가맹점 확인 필요"
