"""PDF 글자 층 읽기. Poppler(pdftotext) 없이 pypdfium2만 쓴다.

pypdfium2는 macOS(Intel·Apple Silicon)·Windows·Linux용 완성본(wheel)이 있어 빌드가 필요 없다.
좌표는 pdftotext -bbox와 같게 쪽 왼쪽 위가 원점이고 아래로 갈수록 y가 커진다.
"""
import ctypes
import unicodedata
from pathlib import Path


class PdfTextError(Exception):
    pass


def _pdfium():
    try:
        import pypdfium2
        import pypdfium2.raw
    except ImportError:
        raise PdfTextError("PDF를 읽으려면 pypdfium2가 필요합니다. `uv run 스크립트.py …`로 실행하세요.") from None
    return pypdfium2


def _chars(textpage, height, raw):
    """쪽의 글자마다 (문자, 왼쪽, 위, 오른쪽, 아래, 글자크기, 글꼴). 공백·줄바꿈은 None 자리표시.
    위·아래는 기준선에서 글자크기의 0.7/0.15로(pdftotext의 숫자 글꼴 높이와 비슷) 잡는다. 글꼴마다 다른 ascent를 쓰면
    같은 줄의 한글과 숫자 높이가 어긋나므로, 같은 기준선이면 같은 세로 범위가 되게 한다."""
    font, flags = ctypes.create_string_buffer(256), ctypes.c_int()
    x, y = ctypes.c_double(), ctypes.c_double()
    for index in range(textpage.count_chars()):
        char = textpage.get_text_range(index, 1)
        if not char or char.isspace() or raw.FPDFText_IsGenerated(textpage.raw, index):
            yield None
            continue
        left, bottom, right, top = textpage.get_charbox(index, loose=True)
        size = raw.FPDFText_GetFontSize(textpage.raw, index) or (top - bottom)
        raw.FPDFText_GetCharOrigin(textpage.raw, index, x, y)
        baseline = height - y.value
        raw.FPDFText_GetFontInfo(textpage.raw, index, font, len(font), ctypes.byref(flags))
        yield char, left, baseline - size * 0.7, right, baseline + size * 0.15, size, font.value


def _words(chars):
    """공백, 글꼴·크기 변경, 벌어진 글자 간격, 줄바꿈에서 낱말을 끊는다(pdftotext와 같은 기준).
    현대카드 명세서는 한글과 숫자를 다른 글꼴로 붙여 써서 글꼴이 바뀌는 곳이 곧 낱말 경계다."""
    words, current = [], None
    for char in chars:
        if char is None:
            current = None
            continue
        text, left, top, right, bottom, size, font = char
        if current is not None and (font != current["font"] or abs(size - current["size"]) > 0.5):
            current = None
        if current is not None:
            same_line = abs((top + bottom) / 2 - (current["yMin"] + current["yMax"]) / 2) < size * 0.5
            gap = left - current["xMax"]
            if not same_line or gap > size * 0.2 or gap < -size * 0.5:
                current = None
        if current is None:
            current = {"text": "", "xMin": left, "yMin": top, "xMax": right, "yMax": bottom, "size": size, "font": font}
            words.append(current)
        current["text"] += text
        current["xMin"], current["xMax"] = min(current["xMin"], left), max(current["xMax"], right)
        current["yMin"], current["yMax"] = min(current["yMin"], top), max(current["yMax"], bottom)
    for word in words:
        word["text"] = unicodedata.normalize("NFC", word["text"])
        del word["font"]
    return words


def pages(path):
    """쪽마다 낱말 목록. 낱말은 {"text", "xMin", "yMin", "xMax", "yMax", "size"}."""
    pdfium = _pdfium()
    try:
        document = pdfium.PdfDocument(str(Path(path)))
    except pdfium.PdfiumError:
        raise PdfTextError("PDF를 열지 못했습니다. 암호·손상 여부를 확인하세요.") from None
    try:
        result = []
        for page in document:
            textpage = page.get_textpage()
            result.append(_words(_chars(textpage, page.get_height(), pdfium.raw)))
            textpage.close()
            page.close()
        return result
    finally:
        document.close()


def _lines(words):
    """세로 가운데가 겹치는 낱말을 한 줄로 묶는다. 표의 좌우 칸이 같은 줄에 놓인다."""
    lines = []
    for word in sorted(words, key=lambda w: ((w["yMin"] + w["yMax"]) / 2, w["xMin"])):
        middle = (word["yMin"] + word["yMax"]) / 2
        if lines and abs(middle - lines[-1]["middle"]) < min(word["size"], lines[-1]["size"]) * 0.5:
            lines[-1]["words"].append(word)
        else:
            lines.append({"middle": middle, "size": word["size"], "words": [word]})
    return [sorted(line["words"], key=lambda w: w["xMin"]) for line in lines]


def layout_text(path):
    """pdftotext -layout처럼 가로 위치를 공백으로 살린 글자. 쪽 사이는 \\f로 나눈다."""
    result = []
    for words in pages(path):
        if not words:
            result.append("")
            continue
        sizes = sorted(w["size"] for w in words)
        cell = sizes[len(sizes) // 2] * 0.5  # 한 칸 폭: 보통 크기 글자의 절반(영숫자 폭)
        left_edge = min(w["xMin"] for w in words)
        rows = []
        for line in _lines(words):
            row, used = "", 0  # used: 지금까지 차지한 칸 수. 한글은 한 글자가 두 칸이라 len(row)와 다르다.
            for word in line:
                column = round((word["xMin"] - left_edge) / cell)
                spaces = max(1 if row else 0, column - used)
                row += " " * spaces + word["text"]
                used += spaces + max(len(word["text"]), round((word["xMax"] - word["xMin"]) / cell))
            rows.append(row.rstrip())
        result.append("\n".join(rows))
    return "\f".join(result)
