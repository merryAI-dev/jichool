#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""로컬 학습 기억. 묻기 전에 찾아보고(recall), 끝나면 답과 정정을 남긴다(record·note·run).

지난 실행의 결과가 다음 실행의 기본값이 된다. 기억은 이 PC에만 있고 밖으로 보내지 않는다.
기억은 기본값과 질문 여부만 바꾼다. 저장·쓰기 전 승인과 금지 규칙은 바꾸지 않는다.
"""
import argparse
import datetime
import json
import os
import re
import sys
import tempfile
from pathlib import Path

SOURCES = ("accepted", "answer", "correction", "always")
KINDS = ("preference", "procedure")
ALL = "*"


def memory_path():
    return Path(os.environ.get("MYSC_MEMORY") or Path.home() / ".config" / "mysc-expense" / "memory.json")


def load():
    path = memory_path()
    if not path.exists():
        return {"values": {}, "notes": [], "runs": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save(data):
    path = memory_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def norm(value):
    return re.sub(r"\s+", "", str(value if value is not None else "")).lower()


def slot(flow, scope, field, key):
    return "|".join((flow, scope or ALL, field, norm(key)))


def today():
    return datetime.date.today().isoformat()


def record(data, flow, field, key, value, source="answer", replaced=None, scope=None):
    """accepted: 채운 값을 그대로 승인. answer: 물어서 받은 답. correction: 채운 값을 고침. always: 앞으로 계속."""
    if source not in SOURCES:
        raise ValueError(f"source는 {SOURCES} 중 하나입니다.")
    value = str(value)
    entry = data["values"].setdefault(slot(flow, scope, field, key), {"key": str(key), "choices": {}})
    choices = entry["choices"]
    if source == "always":
        choices.clear()
    if replaced not in (None, "") and str(replaced) != value and str(replaced) in choices:
        choices[str(replaced)]["no"] += 1
    choice = choices.setdefault(value, {"ok": 0, "no": 0, "last": ""})
    choice["ok"] += 2 if source == "always" else 1
    choice["last"] = today()
    return entry


def recall(data, flow, field, key, scope=None):
    """settled: 묻지 않고 채움. once: 채우고 '지난번과 같게'로 묶어 보여 줌. mixed: 후보를 보여 주고 물음. none: 기억 없음."""
    for where in ([scope, None] if scope else [None]):
        entry = data["values"].get(slot(flow, where, field, key))
        if not entry:
            continue
        live = sorted(((v, c) for v, c in entry["choices"].items() if c["ok"] > c["no"]), key=lambda i: (i[1]["last"], i[1]["ok"]), reverse=True)
        if not live:
            continue
        options = [{"value": v, "times": c["ok"], "last": c["last"]} for v, c in live]
        if len(live) > 1:
            return {"status": "mixed", "value": options[0]["value"], "options": options}
        return {"status": "settled" if live[0][1]["ok"] >= 2 else "once", "value": options[0]["value"], "options": options}
    return {"status": "none", "value": "", "options": []}


def note(data, flow, kind, text_):
    if kind not in KINDS:
        raise ValueError(f"kind는 {KINDS} 중 하나입니다.")
    for item in data["notes"]:
        if item["flow"] == flow and norm(item["text"]) == norm(text_):
            item["hits"] += 1
            item["date"] = today()
            return item
    item = {"flow": flow, "kind": kind, "text": text_, "date": today(), "hits": 1}
    data["notes"].append(item)
    return item


def log_run(data, flow, scope, rows, asked, filled=0, corrected=0):
    data["runs"].append({"flow": flow, "scope": scope or ALL, "date": today(), "rows": rows, "asked": asked, "filled": filled, "corrected": corrected})
    data["runs"] = data["runs"][-200:]


def brief(data, flow, scope=None):
    """실행을 시작할 때 읽는다. 지켜야 할 말투·절차, 이미 아는 사실, 질문 수 추이."""
    notes = [n for n in data["notes"] if n["flow"] in (flow, ALL)]
    facts = {}
    for name, entry in data["values"].items():
        owner, where, field, key = name.split("|", 3)
        if owner in (flow, ALL) and where in (scope or ALL, ALL) and key == "-":
            got = recall(data, owner, field, "-", None if where == ALL else where)
            if got["status"] != "none":
                facts[field] = got["value"]
    runs = [r for r in data["runs"] if r["flow"] == flow and (not scope or r["scope"] == scope)][-5:]
    return {"notes": [{"kind": n["kind"], "text": n["text"], "hits": n["hits"]} for n in sorted(notes, key=lambda n: -n["hits"])], "facts": facts, "recentRuns": runs, "learnedValues": sum(1 for name in data["values"] if name.startswith(flow + "|"))}


def forget(data, flow, field=None, key=None, scope=None, text_=None):
    if text_:
        before = len(data["notes"])
        data["notes"] = [n for n in data["notes"] if not (n["flow"] == flow and norm(text_) in norm(n["text"]))]
        return before - len(data["notes"])
    return int(data["values"].pop(slot(flow, scope, field, key), None) is not None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(name, helper):
        cmd = sub.add_parser(name, help=helper)
        cmd.add_argument("--flow", required=True, help="expense, taxinvoice, expertfee, tongjang, evidence 또는 * (전체)")
        cmd.add_argument("--scope", help="시트 ID·프로젝트 등 적용 범위. 없으면 전체")
        return cmd

    common("brief", "시작할 때: 지킬 것, 아는 사실, 질문 수 추이")
    one = common("recall", "묻기 전에: 기억한 값 찾기")
    one.add_argument("--field", required=True)
    one.add_argument("--key", default="-", help="가맹점·지급처 등. 사실 하나면 생략")
    many = common("recall-many", "묻기 전에: [{field, key}] 목록을 한 번에")
    many.add_argument("file")
    rec = common("record", "답·정정·승인을 남김")
    rec.add_argument("--field", required=True)
    rec.add_argument("--key", default="-")
    rec.add_argument("--value", required=True)
    rec.add_argument("--replaced", help="정정이면 원래 채워져 있던 값")
    rec.add_argument("--source", choices=SOURCES, default="answer")
    recs = common("record-many", "[{field, key, value, source, replaced}] 목록을 한 번에")
    recs.add_argument("file")
    memo = common("note", "대화에서 나온 말투·절차 피드백을 남김")
    memo.add_argument("--kind", choices=KINDS, required=True)
    memo.add_argument("--text", required=True)
    run = common("run", "끝날 때: 이번 실행의 질문 수를 남김")
    for name in ("rows", "asked"):
        run.add_argument("--" + name, type=int, required=True)
    for name in ("filled", "corrected"):
        run.add_argument("--" + name, type=int, default=0)
    gone = common("forget", "틀린 기억 지우기")
    gone.add_argument("--field")
    gone.add_argument("--key", default="-")
    gone.add_argument("--text")
    args = parser.parse_args()
    data = load()
    show = lambda value: print(json.dumps(value, ensure_ascii=False, indent=1))
    if args.command == "brief":
        return show(brief(data, args.flow, args.scope))
    if args.command == "recall":
        return show(recall(data, args.flow, args.field, args.key, args.scope))
    if args.command == "recall-many":
        items = json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))
        return show([{**i, **recall(data, args.flow, i["field"], i.get("key", "-"), args.scope)} for i in items])
    if args.command == "record":
        record(data, args.flow, args.field, args.key, args.value, args.source, args.replaced, args.scope)
    elif args.command == "record-many":
        for i in json.loads(Path(args.file).expanduser().read_text(encoding="utf-8")):
            record(data, args.flow, i["field"], i.get("key", "-"), i["value"], i.get("source", "answer"), i.get("replaced"), args.scope)
    elif args.command == "note":
        note(data, args.flow, args.kind, args.text)
    elif args.command == "run":
        log_run(data, args.flow, args.scope, args.rows, args.asked, args.filled, args.corrected)
    elif args.command == "forget":
        if not args.text and not args.field:
            sys.exit("--field 또는 --text가 필요합니다.")
        show({"removed": forget(data, args.flow, args.field, args.key, args.scope, args.text)})
    save(data)
    if args.command != "forget":
        show({"ok": True})


if __name__ == "__main__":
    main()
