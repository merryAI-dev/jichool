#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["openpyxl", "xlrd"]
# ///
"""통장내역 탭 작성 도구. 은행 화면과 시트에는 직접 접속하지 않는다.

Claude가 빠른조회 표를 읽어 넘기면 중복과 잔액을 검증하고, 과거 행을 근거로 칸을 제안하고,
승인 내용을 시트에 쓸 범위로 바꾼다. 통장내역 탭은 머리글 위쪽을 읽지 않는다.
"""
import argparse
import collections
import csv
import datetime
import difflib
import html.parser
import json
import random
import re
import sys
from pathlib import Path

# Windows 콘솔·파이프 기본 인코딩 대신 UTF-8로 내보내야 AI가 한글을 그대로 읽는다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

BANK_TAB = "통장내역(MYSC법인계좌/e나라도움제외)"
BANK_TAB_PREFIX = "통장내역"
USAGE_TAB = "사용내역(통장내역기준/취소내역,불인정포함)"
USAGE_TAB_PREFIX = "사용내역"
HEADER_ROW = 8
FIRST_ROW = 9
CARD_TYPES = ("체크카드", "직불출금", "카드입금")
EXPENSE_FORMS = {"1210", "1211", "1212", "1214"}
CANCELLED_STATE = "-3"
CARD_MATCH_DAYS = 5
OTHER_MATCH_DAYS = 45
BANK_HEADERS = ["통장번호", "거래일시", "적요", "의뢰인/수취인", "내통장표시내용", "출금금액", "입금금액", "잔액", "취급점", "구분", "비목", "세목", "공급가액", "부가세", "CashFlow", "적요"]
REQUIRED_HEADERS = ("통장번호", "거래일시", "출금금액", "입금금액", "잔액")
USAGE_HEADERS = {"작성자": "A", "No.": "B", "지출구분": "F"}
BANK_MAPS = {
    "kb": {"dt": "거래일시", "type": "적요", "payee": "의뢰인/수취인", "display": "내통장표시내용", "out": "출금금액", "in": "입금금액", "bal": "잔액", "branch": ("취급점", "거래점"), "gubun": "구분"},
    "hana": {"date": "거래일자", "time": "거래시간", "type": "구분", "payee": "적요", "in": "입금액", "out": "출금액", "bal": "잔액", "branch": "거래점"},
    "shinhan": {"date": "거래일자", "time": "시간", "type": "적요", "payee": "내용", "out": "출금(원)", "in": "입금(원)", "bal": "잔액(원)", "branch": "거래점"},
}
FIELDS = ["no", "dt", "type", "payee", "display", "out", "in", "bal", "branch", "gubun", "K", "L", "M", "N", "O", "P"]
VLOOKUP_LIMIT_ROW = 299
USAGE_DROPDOWN_LIMIT_ROW = 201
CATEGORY_COLS = ("L", "O")


class ToolError(ValueError):
    pass


def num(value):
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        return value
    text = re.sub(r"[,\s원]", "", str(value))
    try:
        return float(text) if text else 0
    except ValueError:
        return 0


def text(value):
    return "" if value is None else str(value).replace("\xa0", " ").strip()


def norm(value):
    return re.sub(r"\s+", "", text(value)).lower()


def row_dict(values, row):
    values = list(values) + [None] * (16 - len(values))
    item = dict(zip(FIELDS, values[:16]))
    item["row"] = row
    item["cancel"] = "취소" in text(item["gubun"])
    for field in ("out", "in", "bal"):
        item[field] = num(item[field])
    for field in ("type", "payee", "display", "K", "L", "O", "P"):
        item[field] = text(item[field])
    if item["L"] == "-":
        item["L"] = ""
    return item


def check_header(cells):
    got = [norm(c) for c in list(cells)[:16]] + [""] * 16
    want = [norm(h) for h in BANK_HEADERS]
    wrong = [col_letter(i) for i, name in enumerate(want) if got[i] != name]
    if wrong:
        raise ToolError(f"양식이 다릅니다. 통장내역 탭 {', '.join(wrong)}열 머리글이 공통양식과 다릅니다.")


def column_report(cells):
    """머리글 이름으로 칸을 찾아 사용자에게 물어볼 제안을 만든다. 확정은 사용자가 한다."""
    got = [norm(c) for c in list(cells)[:26]]
    want = [norm(h) for h in BANK_HEADERS]
    exact = {i for i, name in enumerate(want) if i < len(got) and got[i] == name}
    used, found = set(exact), []
    for i, name in enumerate(want):
        if i in exact:
            found.append(i)
            continue
        hit = next((j for j, value in enumerate(got) if value == name and j not in used), None)
        if hit is not None:
            used.add(hit)
        found.append(hit)
    differences = [{"field": BANK_HEADERS[i], "expected": col_letter(i), "suggested": col_letter(f) if f is not None else None} for i, f in enumerate(found) if f != i]
    others = [{"column": col_letter(j), "header": text(cells[j])} for j, value in enumerate(got) if value and j not in used]
    return {"same": not differences, "differences": differences, "unmatchedHeaders": others, "columns": [col_letter(f) if f is not None else None for f in found]}


def apply_columns(values, columns, confirmed):
    trim = lambda cells: [norm(c) for c in cells][:max((i + 1 for i, c in enumerate(cells) if norm(c)), default=0)]
    if trim(values[0]) != trim(confirmed):
        raise ToolError("지난번 확인한 칸 배치와 머리글이 달라졌습니다. 칸 위치를 다시 확인해 주세요.")
    if len(columns) != len(BANK_HEADERS):
        raise ToolError("칸 배치는 공통양식 16칸 순서대로 적어야 합니다.")
    for name, column in zip(BANK_HEADERS, columns):
        if column is None and name in REQUIRED_HEADERS:
            raise ToolError(f"{name} 칸 위치 없이는 읽을 수 없습니다.")
    index = [None if c is None else ord(c) - ord("A") for c in columns]
    return [[line[i] if i is not None and i < len(line) else None for i in index] for line in values]


def history_layout(path):
    path = Path(path).expanduser()
    if path.suffix.lower() != ".json":
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return {k: data[k] for k in ("columns", "bankTab") if data.get(k)}


def history_header(path):
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    return data["values"][0]


def load_history(path):
    path = Path(path).expanduser()
    first_row = FIRST_ROW
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        columns = data.get("columns")
        if data.get("startRow") != HEADER_ROW and not columns:
            raise ToolError("통장내역 탭은 8행(머리글)부터 읽어야 합니다. 그 위는 읽지 않습니다. 머리글이 다른 행이면 사용자에게 확인한 뒤 칸 배치를 적어 주세요.")
        first_row = data["startRow"] + 1
        values = data["values"]
        if columns:
            if not data.get("confirmedHeader"):
                raise ToolError("칸 배치는 사용자가 확인한 머리글(confirmedHeader)과 함께 적어야 합니다.")
            values = apply_columns(values, columns, data["confirmedHeader"])
        else:
            check_header(values[0])
        if "dataStartRow" in data:
            if data["dataStartRow"] < first_row:
                raise ToolError("거래 행은 머리글 아래부터 읽어야 합니다.")
            rows = [row_dict(v, data["dataStartRow"] + i) for i, v in enumerate(values[1:])]
            return [r for r in rows if text(r["dt"])]
        if not values:
            raise ToolError("머리글을 읽지 못했습니다.")
        rows = [row_dict(v, first_row + i) for i, v in enumerate(values[1:])]
        return [r for r in rows if text(r["dt"])]
    else:
        import openpyxl

        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        name = next((n for n in book.sheetnames if n.startswith(BANK_TAB_PREFIX)), None)
        if not name:
            raise ToolError("통장내역 탭을 찾지 못했습니다.")
        values = list(book[name].iter_rows(min_row=HEADER_ROW, max_col=16, values_only=True))
    if not values:
        raise ToolError("머리글을 읽지 못했습니다.")
    check_header(values[0])
    rows = [row_dict(v, FIRST_ROW + i) for i, v in enumerate(values[1:])]
    return [r for r in rows if text(r["dt"])]


def key(item):
    return re.sub(r"\D", "", text(item["dt"])), item["out"], item["in"], item["bal"]


def direction(item):
    return "in" if item["in"] else "out"


def tiers(target):
    return [
        ("같은 수취인·표시내용", lambda h: norm(h["payee"]) == norm(target["payee"]) and norm(h["display"]) == norm(target["display"]), True),
        ("같은 수취인", lambda h: norm(h["payee"]) == norm(target["payee"]), True),
        ("같은 표시내용", lambda h: target["display"] and norm(h["display"]) == norm(target["display"]), True),
        ("같은 거래 종류·방향", lambda h: h["type"] == target["type"] and direction(h) == direction(target), False),
    ]


def vote(target, history, col):
    usable = [h for h in history if h[col] and not h["cancel"]]
    for label, match, specific in tiers(target):
        hits = [h for h in usable if match(h)]
        if not hits:
            continue
        counts = collections.Counter(h[col] for h in hits)
        value, top = counts.most_common(1)[0]
        share = top / len(hits)
        if not specific:
            confidence = "low"
        elif len(hits) >= 2 and share == 1:
            confidence = "high"
        elif share >= 0.6:
            confidence = "medium"
        else:
            confidence = "low"
        sample = [h["no"] for h in hits if h[col] == value][-3:]
        return {"value": value, "confidence": confidence, "basis": f"{label} 과거 {len(hits)}건 중 {top}건", "sampleNo": sample}
    return {"value": "", "confidence": "none", "basis": "근거 없음", "sampleNo": []}


def parent_of(detail, history):
    prefix = detail.split("-")[0] if "-" in detail else ""
    if not prefix:
        return ""
    return next((h["K"] for h in reversed(history) if h["K"].startswith(prefix + ".")), "")


def memo_vote(target, history):
    usable = [h for h in history if h["P"] and not h["cancel"]]
    for label, match, specific in tiers(target)[:3]:
        hits = [h for h in usable if match(h)]
        if hits:
            confidence = "medium" if len(hits) >= 2 else "low"
            return {"value": hits[-1]["P"], "confidence": confidence, "basis": f"{label} 최근 적요 복사", "sampleNo": [hits[-1]["no"]]}
    return {"value": "", "confidence": "none", "basis": "근거 없음", "sampleNo": []}


def vat_candidate(target, history):
    if not target["out"]:
        return None
    hits = [h for h in history if norm(h["payee"]) == norm(target["payee"]) and not h["cancel"] and (h["M"] not in (None, "") or h["N"] not in (None, ""))]
    if not hits:
        return None
    if sum(1 for h in hits if num(h["N"]) > 0) >= len(hits) / 2:
        supply = round(target["out"] * 10 / 11)
        return {"M": supply, "N": target["out"] - supply, "basis": "같은 수취인에 부가세 이력 있음. 10/11 후보"}
    return {"M": target["out"], "N": 0, "basis": "같은 수취인에 부가세 이력 없음. 전액 사업비 후보"}


def suggest(target, history):
    detail = vote(target, history, "L")
    parent = {"value": parent_of(detail["value"], history), "confidence": detail["confidence"], "basis": "세목에서 정해짐", "sampleNo": []}
    if not parent["value"]:
        parent = vote(target, history, "K")
    return {"K": parent, "L": detail, "O": vote(target, history, "O"), "P": memo_vote(target, history), "vat": vat_candidate(target, history)}


def evaluate(rows):
    columns = ["K", "L", "O", "P", "M", "N"]
    stats = {c: collections.Counter() for c in columns}
    seen = collections.defaultdict(collections.Counter)
    excluded = sum(1 for r in rows if r["cancel"])
    for index, row in enumerate(rows):
        if row["cancel"]:
            continue
        result = suggest(row, rows[:index])
        for col in columns:
            stat = stats[col]
            if col in ("M", "N"):
                if row[col] in (None, "") or not row["out"]:
                    continue
                stat["scored"] += 1
                if result["vat"]:
                    stat["filled"] += 1
                    stat["correct"] += abs(num(row[col]) - result["vat"][col]) <= 1
                continue
            if not row[col]:
                continue
            stat["scored"] += 1
            pick = result[col]
            common = seen[col].most_common(1)
            stat["baseline"] += bool(common) and common[0][0] == row[col]
            seen[col][row[col]] += 1
            if pick["value"]:
                stat["filled"] += 1
                stat["correct"] += pick["value"] == row[col]
                stat[pick["confidence"]] += 1
                stat[pick["confidence"] + "_correct"] += pick["value"] == row[col]
    return {"rows": len(rows), "cancelledExcluded": excluded, "columns": {c: dict(stats[c]) for c in columns}}


def read_bank(path, order):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    items = data["rows"] if isinstance(data, dict) else data
    rows = []
    for item in items:
        if not text(item.get("dt")):
            raise ToolError("거래일시가 빈 행이 있습니다. 은행 표를 다시 읽어 주세요.")
        rows.append(row_dict([None, item["dt"], item.get("type"), item.get("payee"), item.get("display"), item.get("out"), item.get("in"), item.get("bal"), item.get("branch"), item.get("gubun")], 0))
    return list(reversed(rows)) if order == "newest-first" else rows


def plan(history, bank, references=()):
    """참고 시트는 제안 근거로만 쓴다. 중복·잔액·행 번호는 이번 시트 기준이다."""
    known = {key(h) for h in history}
    oldest = re.sub(r"\D", "", text(history[0]["dt"])) if history else ""
    if oldest and any(re.sub(r"\D", "", text(r["dt"])) < oldest for r in bank):
        raise ToolError("은행 표에 읽어 둔 시트 행보다 오래된 거래가 있습니다. 시트를 더 앞에서부터 읽어 주세요.")
    fresh = [r for r in bank if key(r) not in known]
    issues = []
    previous = history[-1] if history else None
    for row in fresh:
        if previous and abs(previous["bal"] - row["out"] + row["in"] - row["bal"]) > 0.5:
            issues.append({"dt": text(row["dt"]), "expectedBalance": previous["bal"] - row["out"] + row["in"], "bankBalance": row["bal"]})
        previous = row
    next_no = int(max([num(h["no"]) for h in history] or [0])) + 1
    next_row = (history[-1]["row"] + 1) if history else FIRST_ROW
    out = []
    pool = [{**h, "no": f"참고 {h['no']}"} for h in references] + list(history)
    for offset, row in enumerate(fresh):
        row["no"] = next_no + offset
        row["row"] = next_row + offset
        out.append({**{f: row[f] for f in ("no", "row", "dt", "type", "payee", "display", "out", "in", "bal", "branch", "gubun")}, "suggest": suggest(row, pool)})
    return {"bankRowsRead": len(bank), "alreadyInSheet": len(bank) - len(fresh), "balanceIssues": issues, "rows": out}


def shift_formula(formula, delta):
    parts = re.split(r"(\"(?:[^\"]|\"\")*\"|'(?:[^']|'')*')", formula)
    ref = re.compile(r"(?<![A-Za-z0-9_$])(\$?[A-Z]{1,3})(\d+)(?![\d(A-Za-z_])")
    for index in range(0, len(parts), 2):
        parts[index] = ref.sub(lambda m: f"{m.group(1)}{int(m.group(2)) + delta}", parts[index])
    return "".join(parts)


def col_letter(index):
    return chr(ord("A") + index)


def usage_columns(template, answers):
    """사용내역 칸이 공통양식과 다르면 사용자가 답한 usageColumns 없이는 쓰지 않는다."""
    header = template.get("header")
    if header is None:
        raise ToolError("사용내역 탭 머리글 행을 usage_template.json의 header로 읽어 주세요.")
    if answers.get("usageColumns"):
        return {**USAGE_HEADERS, **answers["usageColumns"]}
    got = [norm(c) for c in header] + [""] * 26
    wrong = [f"{letter}열({name})" for name, letter in USAGE_HEADERS.items() if got[ord(letter) - ord("A")] != norm(name)]
    if wrong:
        raise ToolError(f"양식이 다릅니다. 사용내역 탭 {', '.join(wrong)} 위치를 사용자에게 확인해 usageColumns로 적어 주세요.")
    return dict(USAGE_HEADERS)


def usage_rows(template, numbers, author, columns=USAGE_HEADERS, tab=USAGE_TAB):
    """공통양식은 사용내역 수식이 미리 깔려 있다. 깔린 행에는 작성자만 쓰고, 그 아래만 수식을 늘린다."""
    base = template["row"]
    values = list(template["values"]) + [None] * (23 - len(template["values"]))
    base_no = int(num(values[ord(columns["No."]) - ord("A")]))
    offset = base - base_no
    warnings = []
    ranges = []
    existing = [no for no in numbers if no <= base_no]
    writer = columns["작성자"]
    if existing and writer:
        ranges.append({"tab": tab, "range": f"{writer}{existing[0] + offset}:{writer}{existing[-1] + offset}", "values": [[author] for _ in existing]})
    extra = [no for no in numbers if no > base_no]
    if not extra:
        return ranges, warnings
    if columns != USAGE_HEADERS:
        raise ToolError("칸 배치가 다른 사용내역은 수식을 자동으로 늘리지 않습니다. 시트에서 수식을 먼저 늘려 주세요.")
    left, right = [], []
    for no in extra:
        step = no - base_no
        line = []
        for index in range(23):
            cell = values[index]
            if index == 0:
                line.append(author)
            elif index == 1:
                line.append(no)
            elif isinstance(cell, str) and cell.startswith("="):
                line.append(shift_formula(cell, step))
            else:
                line.append("")
                if no == extra[0] and cell not in (None, "") and index not in (4, 19, 20, 13):
                    warnings.append(f"사용내역 {col_letter(index)}열 원본이 수식이 아니라 비워 둡니다.")
        left.append(line[:4])
        right.append(line[5:])
    first = extra[0] + offset
    last = extra[-1] + offset
    if last > USAGE_DROPDOWN_LIMIT_ROW:
        warnings.append(f"사용내역 드롭다운은 {USAGE_DROPDOWN_LIMIT_ROW}행까지만 걸려 있습니다.")
    ranges.append({"tab": USAGE_TAB, "range": f"A{first}:D{last}", "values": left})
    ranges.append({"tab": USAGE_TAB, "range": f"F{first}:W{last}", "values": right})
    return ranges, warnings


def bank_ranges(tab, letters, matrix, first, last):
    """칸 배치가 끊겨 있으면 이어진 칸끼리 나눠 쓴다. 없는 칸은 쓰지 않는다."""
    placed = sorted((ord(c) - ord("A"), i) for i, c in enumerate(letters) if c)
    blocks = []
    for position, field in placed:
        if blocks and blocks[-1][-1][0] == position - 1:
            blocks[-1].append((position, field))
        else:
            blocks.append([(position, field)])
    return [{"tab": tab, "range": f"{col_letter(b[0][0])}{first}:{col_letter(b[-1][0])}{last}", "values": [[line[f] for _, f in b] for line in matrix]} for b in blocks]


def payment_method(kind, card):
    return card if text(kind) in CARD_TYPES else "계좌이체"


def finalize(planned, answers, template, author):
    if planned["balanceIssues"] and not answers.get("acceptBalanceIssues"):
        raise ToolError("잔액이 이어지지 않는 행이 있어 쓸 범위를 만들지 않습니다. 은행 표를 다시 읽거나 사용자 확인을 받으세요.")
    rows = planned["rows"]
    if not rows:
        return {"ranges": [], "warnings": [], "unanswered": []}
    edits = {str(k): v for k, v in answers.get("rows", {}).items()}
    accept_vat = answers.get("acceptVat", [])
    matrix, unanswered, lessons = [], [], []
    for row in rows:
        chosen = edits.get(str(row["no"]), {})
        s = row["suggest"]
        cells = {}
        for col in ("K", "L", "O", "P"):
            pick = s[col]
            auto = pick["value"] if pick["confidence"] in ("high", "medium") and col != "P" else ""
            known = (row.get("learned") or {}).get(col) or {}
            if known.get("status") in ("settled", "once"):
                auto = known["value"]
            cells[col] = chosen.get(col, auto)
            if cells[col] != "" and text(row["payee"]):
                lessons.append({"field": col, "key": row["payee"], "value": cells[col], "replaced": auto, "source": "accepted" if cells[col] == auto else ("correction" if auto else "answer")})
        vat = s["vat"] if (accept_vat is True or row["no"] in accept_vat) else None
        cells["M"] = chosen.get("M", vat["M"] if vat else "")
        cells["N"] = chosen.get("N", vat["N"] if vat else "")
        missing = [c for c in ("K", "L", "O", "M", "P") if cells[c] == ""] if row["out"] else [c for c in ("O",) if cells[c] == ""]
        if missing:
            unanswered.append({"no": row["no"], "missing": missing})
        matrix.append([row["no"], row["dt"], row["type"], row["payee"], row["display"], row["out"], row["in"], row["bal"], row["branch"], row["gubun"], cells["K"], cells["L"], cells["M"], cells["N"], cells["O"], cells["P"]])
    first, last = rows[0]["row"], rows[-1]["row"]
    warnings = []
    if last > VLOOKUP_LIMIT_ROW:
        warnings.append(f"사용내역 수식은 통장내역 {VLOOKUP_LIMIT_ROW}행까지만 찾습니다. 수식 범위를 늘려야 합니다.")
    layout = planned.get("layout") or {}
    letters = layout.get("columns") or [col_letter(i) for i in range(16)]
    bank_tab = layout.get("bankTab", BANK_TAB)
    usage_tab = template.get("tab", USAGE_TAB)
    columns = usage_columns(template, answers)
    ranges = bank_ranges(bank_tab, letters, matrix, first, last)
    extra, notes = usage_rows(template, [row["no"] for row in rows], author, columns, usage_tab)
    card = answers.get("card")
    payment = columns["지출구분"]
    if card and payment:
        top = rows[0]["no"] + template["row"] - int(num(template["values"][ord(columns["No."]) - ord("A")]))
        extra.append({"tab": usage_tab, "range": f"{payment}{top}:{payment}{top + len(rows) - 1}", "values": [[payment_method(r["type"], card)] for r in rows]})
    positions = [ord(c) - ord("A") for c in letters if c]
    overwrite = {bank_tab: [ord(letters[i]) - ord("A") for i in (0, 13) if letters[i]], usage_tab: [ord(payment) - ord("A")] if payment else []}
    format_copy = {"tab": bank_tab, "sourceRow": first - 1, "firstRow": first, "lastRow": last, "startColumn": min(positions), "endColumn": max(positions) + 1}
    return {"ranges": ranges + extra, "overwrite": overwrite, "formatCopy": format_copy, "warnings": warnings + notes, "unanswered": unanswered, "learn": lessons, "asked": answers.get("asked", sum(len(v) for v in edits.values()))}


def attach_learned(planned, scope):
    """지난 실행에서 사용자가 승인하거나 고친 값을 행마다 붙인다. 지급처가 같을 때만 쓴다."""
    import learn

    memory = learn.load()
    for row in planned["rows"]:
        if text(row["payee"]):
            found = {col: learn.recall(memory, "tongjang", col, row["payee"], scope) for col in ("K", "L", "O", "P")}
            row["learned"] = {col: got for col, got in found.items() if got["status"] != "none"}
    planned["scope"] = scope
    return planned


def remembered(scope, field):
    import learn

    return learn.recall(learn.load(), "tongjang", field, "-", scope)["value"]


def reflect(result, scope, facts):
    """쓰기와 확인이 끝난 뒤 부른다. 이번에 확정된 값과 질문 수가 다음 실행의 기본값이 된다."""
    import learn

    memory = learn.load()
    for item in result.get("learn", []):
        learn.record(memory, "tongjang", item["field"], item["key"], item["value"], item["source"], item["replaced"], scope)
    for field, value in facts.items():
        if value and learn.recall(memory, "tongjang", field, "-", scope)["value"] != value:
            learn.record(memory, "tongjang", field, "-", value, "always", scope=scope)
    counts = collections.Counter(i["source"] for i in result.get("learn", []))
    rows = len({i["key"] for i in result.get("learn", [])})
    learn.log_run(memory, "tongjang", scope, rows, result.get("asked", 0), counts["accepted"], counts["correction"])
    learn.save(memory)
    return {"recorded": len(result.get("learn", [])), "asked": result.get("asked", 0), "accepted": counts["accepted"], "corrected": counts["correction"], "answered": counts["answer"]}


def format_dt(value, clock=None):
    digits = re.sub(r"\D", "", f"{text(value)}{text(clock)}")
    if len(digits) < 8:
        raise ToolError("거래일시를 읽지 못했습니다. 은행 표를 다시 읽어 주세요.")
    day = f"{digits[:4]}.{digits[4:6]}.{digits[6:8]}"
    if len(digits) < 12:
        return day
    moment = digits[8:14].ljust(6, "0")
    return f"{day}\n {moment[:2]}:{moment[2:4]}:{moment[4:6]}"


class TableText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def read_matrix(path):
    path = Path(path).expanduser()
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return [data["headers"]] + data["rows"] if isinstance(data, dict) else data
    if suffix == ".csv":
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "cp949"):
            try:
                return list(csv.reader(raw.decode(encoding).splitlines()))
            except UnicodeDecodeError:
                continue
        raise ToolError("파일 글자 형식을 읽지 못했습니다.")
    if suffix in (".xlsx", ".xlsm"):
        import openpyxl

        sheet = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
        return [list(r) for r in sheet.iter_rows(values_only=True)]
    if suffix == ".xls":
        raw = path.read_bytes()
        if b"<table" in raw[:20000].lower():
            for encoding in ("utf-8-sig", "cp949"):
                try:
                    body = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raise ToolError("파일 글자 형식을 읽지 못했습니다.")
            table = TableText()
            table.feed(body)
            return table.rows
        import xlrd

        sheet = xlrd.open_workbook(file_contents=raw).sheet_by_index(0)
        return [sheet.row_values(i) for i in range(sheet.nrows)]
    raise ToolError("csv, xlsx, xls 파일만 읽을 수 있습니다.")


def normalize_bank(matrix, bank):
    spec = {field: (name if isinstance(name, tuple) else (name,)) for field, name in BANK_MAPS[bank].items()}
    wanted = {field: {norm(n) for n in names} for field, names in spec.items()}
    optional = {"branch", "gubun", "display"}
    for index, line in enumerate(matrix):
        cells = [norm(c) for c in line]
        where = {f: next((i for i, c in enumerate(cells) if c in names), None) for f, names in wanted.items()}
        if all(where[f] is not None for f in where if f not in optional):
            break
    else:
        raise ToolError("은행 표의 머리글을 찾지 못했습니다. 양식이 다릅니다.")
    rows = []
    for line in matrix[index + 1:]:
        cell = lambda field: line[where[field]] if where.get(field) is not None and where[field] < len(line) else None
        when = cell("dt") if "dt" in where else cell("date")
        if not re.match(r"\s*\d{4}\D?\d{2}\D?\d{2}", text(when)):
            continue
        rows.append({
            "dt": format_dt(when, cell("time")), "type": text(cell("type")), "payee": text(cell("payee")), "display": text(cell("display")),
            "out": num(cell("out")), "in": num(cell("in")), "bal": num(cell("bal")), "branch": text(cell("branch")), "gubun": text(cell("gubun")),
        })
    return {"rows": rows}


def same_value(left, right):
    if norm(left) == norm(right):
        return True
    clean = lambda v: re.sub(r"[,\s원]", "", str(v if v is not None else ""))
    try:
        return abs(float(clean(left)) - float(clean(right))) < 0.5
    except ValueError:
        return False


def walk(ranges, current):
    items = ranges["ranges"]
    if len(items) != len(current):
        raise ToolError("다시 읽은 범위 수가 쓸 범위 수와 다릅니다.")
    for item, now in zip(items, current):
        start = ord(re.match(r"[A-Z]", item["range"]).group(0)) - ord("A")
        top = int(re.search(r"\d+", item["range"]).group(0))
        for r, line in enumerate(item["values"]):
            have = now[r] if r < len(now) else []
            for c, value in enumerate(line):
                yield item, start + c, top + r, value, have[c] if c < len(have) else ""


def guard(ranges, current):
    """쓰기 직전 대상 칸이 비었는지 본다. 통장내역 A·N열과 사용내역 F열은 양식 값이 미리 깔려 있어 덮어쓴다."""
    allowed = ranges.get("overwrite") or {BANK_TAB: (0, 13), USAGE_TAB: (5,)}
    return [f"{i['tab'][:4]} {col_letter(c)}{r}" for i, c, r, _, have in walk(ranges, current) if text(have) and c not in allowed.get(i["tab"], ())]


def verify(ranges, current):
    return [f"{i['tab'][:4]} {col_letter(c)}{r}" for i, c, r, value, have in walk(ranges, current) if not same_value(value, have)]


def workbook_options(path):
    import openpyxl

    book = openpyxl.load_workbook(Path(path).expanduser())
    sheet = book[next(n for n in book.sheetnames if n.startswith(USAGE_TAB_PREFIX))]
    options = {}
    for rule in sheet.data_validations.dataValidation:
        col = str(rule.sqref)[:1]
        if col in ("H", "J"):
            options["L" if col == "H" else "O"] = [v.strip() for v in str(rule.formula1).strip('"').split(",")]
    return options


def brief(row):
    return {"no": row["no"], "date": text(row["dt"])[:10], "type": row["type"], "payee": row["payee"], "display": row["display"], "out": row["out"], "in": row["in"]}


def eval_pack(rows, options, parts):
    """정답을 가린 채점 묶음. 각 행은 그 앞 행만 근거로 받는다."""
    entries = []
    for index, row in enumerate(rows):
        if row["cancel"] or not (row["L"] or row["O"] or row["P"]):
            continue
        before = [h for h in rows[:index] if not h["cancel"]]
        similar = [h for h in before if norm(h["payee"]) == norm(row["payee"]) or (row["display"] and norm(h["display"]) == norm(row["display"]))][-5:]
        recent = [h for h in before if h["P"]][-3:]
        solved = lambda h: {**brief(h), "L": h["L"], "O": h["O"], "P": h["P"]}
        entries.append({"id": row["row"], "target": brief(row), "similarEarlier": [solved(h) for h in similar], "recentEarlier": [solved(h) for h in recent]})
    size = len(entries) // parts + 1
    return [{"options": options, "entries": entries[i:i + size]} for i in range(0, len(entries), size)]


def eval_score(rows, answers, sample=20, seed=7):
    stats = {c: collections.Counter() for c in ("K", "L", "O", "P")}
    pairs = []
    for index, row in enumerate(rows):
        answer = answers.get(row["row"])
        if not answer or row["cancel"]:
            continue
        before = rows[:index]
        rule = suggest(row, before)
        guess = dict(answer)
        guess["K"] = parent_of(text(answer.get("L")), before)
        for col, stat in stats.items():
            if not row[col]:
                continue
            value = text(guess.get(col))
            stat["scored"] += 1
            stat["filled"] += bool(value)
            stat["correct"] += norm(value) == norm(row[col])
            stat["rule"] += norm(rule[col]["value"]) == norm(row[col])
            if col == "P":
                stat["close"] += difflib.SequenceMatcher(None, norm(value), norm(row[col])).ratio() >= 0.8
            elif col == "L":
                level = text(answer.get("confidence")) or "none"
                stat[level] += 1
                stat[level + "_correct"] += norm(value) == norm(row[col])
        if row["P"]:
            pairs.append({"no": row["no"], "payee": row["payee"], "human": row["P"], "claude": text(answer.get("P"))})
    random.Random(seed).shuffle(pairs)
    return {"columns": {c: dict(s) for c, s in stats.items()}}, pairs[:sample]


def fetch_expenses(since, until):
    """본인이 기안한 지출결의를 jichool의 조회 기능으로 읽는다. 그룹웨어에 쓰지 않는다."""
    jichool = Path(__file__).resolve().parent.parent / "jichool"
    if not (jichool / "amaranth.py").exists():
        raise ToolError("지출결의 스킬(jichool)이 설치되어 있지 않습니다.")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(jichool))
    import amaranth
    import expense

    items = []
    try:
        client = amaranth.ReadClient()
        documents = amaranth.run(client, "expense_list", {"date_from": since, "date_to": until})
        for document in documents:
            if str(document.get("formId")) not in EXPENSE_FORMS or str(document.get("state")) == CANCELLED_STATE:
                continue
            detail = amaranth.run(client, "expense_document", {"linkKey": document["linkKey"]})
            for row in detail["detail"]:
                items.append({
                    "title": text(document.get("title")), "state": str(document.get("state")), "form": str(document.get("formId")),
                    "issue": text(row.get("issDt")), "pay": text(row.get("payDt")), "card": text(row.get("cardDt")),
                    "sum": num(row.get("sumAm")), "sup": num(row.get("supAm")), "vat": num(row.get("vatAm")),
                    "memo": text(row.get("rmkDc")), "purpose": text(row.get("cashNm")), "project": text(row.get("pjtNm")), "vendor": text(row.get("trNm")),
                })
    except expense.ExpenseError as error:
        raise ToolError(f"그룹웨어 연결이 필요합니다. {error}") from None
    return items


def day_of(value):
    digits = re.sub(r"\D", "", text(value))
    if len(digits) < 8 or digits[:8] == "00000000":
        return None
    try:
        return datetime.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def same_party(left, right):
    a, b = norm(left), norm(right)
    return bool(a and b and (a in b or b in a))


def expense_view(item):
    return {k: item[k] for k in ("memo", "purpose", "project", "vendor", "sum", "sup", "vat", "issue", "state")}


def attach_expenses(planned, items, project=""):
    """금액이 같고 날짜가 가까운 지출결의를 찾아 붙인다. 한 결의 행은 한 거래에만 쓴다."""
    pool = [i for i in items if i["state"] != CANCELLED_STATE and (not project or project in i["project"])]
    used = set()
    for row in planned["rows"]:
        when = day_of(row["dt"])
        found = []
        if row["out"] and when:
            limit = CARD_MATCH_DAYS if row["type"] == "체크카드" else OTHER_MATCH_DAYS
            for index, item in enumerate(pool):
                gaps = [abs((d - when).days) for d in map(day_of, (item["pay"], item["card"], item["issue"])) if d]
                if index not in used and abs(item["sum"] - row["out"]) < 1 and gaps and min(gaps) <= limit:
                    found.append((not same_party(row["payee"], item["vendor"]), item["state"] != "1", min(gaps), index))
        found.sort()
        same_memo = len({pool[f[3]]["memo"] for f in found}) == 1
        decided = len(found) == 1 or (len(found) > 1 and (same_memo or (not found[0][0] and found[0][:3] < found[1][:3])))
        if decided:
            used.add(found[0][3])
        like = [i for i in pool if same_party(row["payee"], i["vendor"]) and i["memo"]][-3:]
        recent = [i for i in pool if i["memo"] and i not in like][-3:]
        row["expense"] = {
            "match": "exact" if decided else "candidates" if found else "none",
            "items": [expense_view(pool[f[3]]) for f in (found[:1] if decided else found[:5])],
            "similar": [expense_view(i) for i in like + recent],
        }
    return planned


def write_json(data, path=None):
    body = json.dumps(data, ensure_ascii=False, indent=1)
    if path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        print(target)
    else:
        print(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="시트의 마지막 거래 위치")
    status.add_argument("history")
    layout = sub.add_parser("columns", help="머리글이 공통양식과 다를 때 사용자에게 물어볼 칸 위치 제안")
    layout.add_argument("history")
    score = sub.add_parser("eval", help="과거 행으로 칸별 정확도 측정. 합계만 출력")
    score.add_argument("history")
    make = sub.add_parser("plan", help="은행 표를 검증하고 칸을 제안")
    make.add_argument("history")
    make.add_argument("bank")
    make.add_argument("--order", choices=["newest-first", "oldest-first"], default="newest-first")
    make.add_argument("--reference", action="append", default=[], help="작년 시트 등 제안 근거로 쓸 통장내역")
    make.add_argument("--scope", default="", help="시트 ID. 이 시트에서 배운 값을 제안에 붙인다")
    make.add_argument("--out")
    learned = sub.add_parser("reflect", help="쓰기 확인 뒤: 이번에 확정된 값을 다음 실행의 기본값으로 남김")
    learned.add_argument("ranges")
    learned.add_argument("--scope", default="")
    learned.add_argument("--author")
    learned.add_argument("--card")
    fetch = sub.add_parser("expenses", help="본인이 기안한 지출결의 읽기")
    fetch.add_argument("--since", required=True)
    fetch.add_argument("--until", required=True)
    fetch.add_argument("--out", required=True)
    link = sub.add_parser("attach", help="제안에 지출결의를 붙임")
    link.add_argument("plan")
    link.add_argument("expenses")
    link.add_argument("--project", default="")
    bank_cmd = sub.add_parser("bank", help="은행 표나 내려받은 파일을 표준 형식으로 변환")
    bank_cmd.add_argument("source")
    bank_cmd.add_argument("--bank", choices=sorted(BANK_MAPS), required=True)
    bank_cmd.add_argument("--out")
    for name, helper in (("guard", "쓰기 직전 대상 칸이 비었는지 확인"), ("verify", "쓴 뒤 값이 같은지 확인")):
        check = sub.add_parser(name, help=helper)
        check.add_argument("ranges")
        check.add_argument("current")
    pack = sub.add_parser("eval-pack", help="정답을 가린 채점 묶음 생성")
    pack.add_argument("history")
    pack.add_argument("outdir")
    pack.add_argument("--parts", type=int, default=2)
    graded = sub.add_parser("eval-score", help="Claude 답을 칸별로 채점")
    graded.add_argument("history")
    graded.add_argument("answers", nargs="+")
    graded.add_argument("--review")
    final = sub.add_parser("finalize", help="승인 내용을 시트에 쓸 범위로 변환")
    final.add_argument("plan")
    final.add_argument("answers")
    final.add_argument("usage_template")
    final.add_argument("--author", help="생략하면 지난번 작성자")
    final.add_argument("--out")
    args = parser.parse_args()
    try:
        if args.command == "status":
            rows = load_history(args.history)
            last = rows[-1]
            write_json({"rows": len(rows), "lastRow": last["row"], "lastNo": last["no"], "lastDate": text(last["dt"])[:10], "lastBalance": last["bal"]})
        elif args.command == "columns":
            write_json(column_report(history_header(args.history)))
        elif args.command == "eval":
            write_json(evaluate(load_history(args.history)))
        elif args.command == "plan":
            references = [row for path in args.reference for row in load_history(path)]
            write_json(attach_learned({**plan(load_history(args.history), read_bank(args.bank, args.order), references), "layout": history_layout(args.history)}, args.scope), args.out)
        elif args.command == "reflect":
            result = json.loads(Path(args.ranges).expanduser().read_text(encoding="utf-8-sig"))
            write_json(reflect(result, args.scope, {"작성자": args.author, "카드": args.card}))
        elif args.command == "expenses":
            items = fetch_expenses(args.since, args.until)
            write_json(items, args.out)
            write_json({"rows": len(items), "projects": collections.Counter(i["project"] for i in items).most_common()})
        elif args.command == "attach":
            load = lambda p: json.loads(Path(p).expanduser().read_text(encoding="utf-8-sig"))
            write_json(attach_expenses(load(args.plan), load(args.expenses), args.project), args.plan)
        elif args.command == "bank":
            write_json(normalize_bank(read_matrix(args.source), args.bank), args.out)
        elif args.command in ("guard", "verify"):
            load = lambda p: json.loads(Path(p).expanduser().read_text(encoding="utf-8-sig"))
            problems = (guard if args.command == "guard" else verify)(load(args.ranges), load(args.current))
            write_json({"ok": not problems, "cells": problems})
            sys.exit(0 if not problems else 3)
        elif args.command == "eval-pack":
            parts = eval_pack(load_history(args.history), workbook_options(args.history), args.parts)
            for number, part in enumerate(parts, 1):
                write_json(part, Path(args.outdir) / f"pack_{number}.json")
        elif args.command == "eval-score":
            answers = {}
            for path in args.answers:
                answers.update({item["id"]: item for item in json.loads(Path(path).read_text(encoding="utf-8-sig"))["answers"]})
            report, review = eval_score(load_history(args.history), answers)
            write_json(report)
            if args.review:
                lines = ["| No. | 지급처 | 사람이 쓴 적요 | Claude 적요 | 그대로 써도 되나요 (O/X) |", "|---|---|---|---|---|"]
                lines += [f"| {p['no']} | {p['payee']} | {p['human']} | {p['claude']} |  |" for p in review]
                Path(args.review).write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            load = lambda p: json.loads(Path(p).expanduser().read_text(encoding="utf-8-sig"))
            planned, answers = load(args.plan), load(args.answers)
            scope = planned.get("scope", "")
            author = args.author or remembered(scope, "작성자")
            if not author:
                raise ToolError("작성자 닉네임이 필요합니다. 처음 한 번만 물어 --author로 넘겨 주세요.")
            if "card" not in answers and remembered(scope, "카드"):
                answers["card"] = remembered(scope, "카드")
            result = finalize(planned, answers, load(args.usage_template), author)
            write_json({**result, "author": author, "card": answers.get("card", ""), "scope": scope}, args.out)
    except ToolError as error:
        print(f"중단: {error}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
