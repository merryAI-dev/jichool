# /// script
# requires-python = ">=3.10"
# dependencies = ["pypdfium2>=4.30", "pillow>=10", "xlrd>=2"]
# ///
"""세금계산서 인식용 결의의 결제 증빙을 찾고, 카드 전표 캡처를 첨부용 PDF로 만든다."""
import argparse
from datetime import datetime, timedelta
from itertools import combinations
import json
from pathlib import Path
import sys
import urllib.parse

from expense import CONFIG, Client, ExpenseError, ORIGIN, read_private, won

PROFILE = CONFIG / "state" / "me.profile.json"
# 카드 전표 화면을 800×1184 창에서 찍었을 때의 위치다.
WIDTH, HEIGHT = 800, 1184
CROP = (42, 20, 758, 580)
MASK = (562, 417, 666, 439)
HIGHLIGHTS = ((84, 48, 718, 160), (84, 535, 718, 563))
RED = (220, 0, 0)
GRAY = (120, 120, 120)
MAX_MATCHES = 20


def receipt_url(row):
    query = {"MicroModuleCode": "personal", "coCd": row["coCd"], "issDt": row["issDt"],
             "issSq": row["issSq"], "callComp": "APB1020PCard", "cardFg": row["cardFg"]}
    return ORIGIN + "/#/popup?" + urllib.parse.urlencode(query)


def merchant_of(row):
    return row.get("chainName") or row.get("chainTrNm") or ""


def payment_sets(rows, total):
    """합계가 맞는 거래 묶음을 건수가 적은 순으로 돌려준다."""
    if len(rows) > MAX_MATCHES:
        raise ExpenseError(f"후보가 {len(rows)}건으로 많습니다. 가맹점 이름이나 기간을 좁혀 주세요.")
    found = []
    for size in range(1, len(rows) + 1):
        for group in combinations(rows, size):
            if sum(won(row["sunginAm"]) for row in group) == total:
                found.append(list(group))
    return found


def find_payments(client, card, merchant, issued, total, days):
    day = datetime.strptime(issued, "%Y%m%d")
    start = (day - timedelta(days=days)).strftime("%Y%m%d")
    end = (day + timedelta(days=days)).strftime("%Y%m%d")
    needle = merchant.replace(" ", "").lower()
    rows = [row for row in client.cards(start, end, card, include_applied=True)
            if needle in merchant_of(row).replace(" ", "").lower()]
    rows.sort(key=lambda row: (row["issDt"], row["issSq"]))
    view = lambda row: {"date": row["issDt"], "amount": won(row["sunginAm"]), "merchant": merchant_of(row),
                        "usedInExpense": row.get("appYn") == "Y", "receiptUrl": receipt_url(row)}
    return {"period": [start, end], "candidates": [view(row) for row in rows],
            "matches": [[view(row) for row in group] for group in payment_sets(rows, total)]}


def card_name(args):
    if args.card:
        return args.card
    if not PROFILE.exists():
        raise ExpenseError("카드 프로필이 없습니다. expense.py profile을 먼저 실행하세요.")
    return read_private(PROFILE)["cardName"]


def find_command(args):
    result = find_payments(Client(), card_name(args), args.merchant, args.issued, args.total, args.days)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def mark_image(path):
    from PIL import Image, ImageDraw

    image = Image.open(path).convert("RGB")
    if abs(image.width / image.height - WIDTH / HEIGHT) > 0.01:
        raise ExpenseError(f"{Path(path).name}: 전표 창을 {WIDTH}×{HEIGHT}으로 맞춰 다시 찍어 주세요.")
    scale = image.width / WIDTH
    box = lambda rect: [round(value * scale) for value in rect]
    draw = ImageDraw.Draw(image)
    draw.rectangle(box(MASK), fill=GRAY)
    for rect in HIGHLIGHTS:
        draw.rectangle(box(rect), outline=RED, width=max(3, round(3 * scale)))
    return image.crop(box(CROP))


def mark_command(args):
    out = Path(args.out)
    if out.suffix.lower() != ".pdf":
        raise ExpenseError("결과 파일은 .pdf여야 합니다. 그룹웨어 첨부는 PDF만 받습니다.")
    if out.exists():
        raise ExpenseError(f"이미 있는 파일입니다: {out.name}. 새 이름을 지정하세요.")
    pages = [mark_image(path) for path in args.images]
    pages[0].save(out, save_all=True, append_images=pages[1:], resolution=110)
    print(json.dumps({"out": str(out), "pages": len(pages)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    command = commands.add_parser("find", help="세금계산서 합계와 맞는 카드 결제 묶음 찾기 (조회만)")
    command.add_argument("--merchant", required=True, help="그룹웨어 가맹점 이름의 일부")
    command.add_argument("--issued", required=True, help="세금계산서 작성일자 YYYYMMDD")
    command.add_argument("--total", required=True, type=int, help="세금계산서 합계")
    command.add_argument("--days", type=int, default=31, help="작성일자 앞뒤로 찾을 일수")
    command.add_argument("--card", help="그룹웨어 카드 이름. 없으면 프로필의 카드")
    command.set_defaults(run=find_command)
    command = commands.add_parser("mark", help="카드 전표 캡처를 강조·가림 처리해 PDF로 묶기")
    command.add_argument("images", nargs="+", help="전표 캡처 이미지")
    command.add_argument("--out", required=True, help="새 PDF 경로")
    command.set_defaults(run=mark_command)
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
