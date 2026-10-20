#!/usr/bin/env python3
"""MYSC 전문가 활용비(외부인건비, 원천징수) 지출결의 CLI. Python 표준 라이브러리만 사용한다.

흐름은 법인카드·세금계산서와 같다: 서류 판독 → 검토안(prepare) → 질문 해소 → check → 사용자 확인 → draft.
- 1210 지출결의: 한 사람당 한 행. 계정 0300 기타소득지급수수료(8.8%) 또는 0290 사업소득지급수수료(3.3%), 증빙 기타.
- 1212 원천세 자금이체: 사업 전용 계좌에서 지급할 때만, 원천세를 운영비 계좌로 옮긴다. 운영비 계좌에서 바로 지급하면 없다.
- 신분증·통장 사본은 AI가 읽고 그 결과를 spec(JSON)으로 넘긴다. 이 도구는 이미지 판독을 하지 않는다.
- 주민등록번호는 앞 6자리만 받는다. 전체 번호는 검토안·작업 기록 어디에도 남기지 않는다.

근거 문서: MYSC-2025-1509(기타 4인), 2025-4940/4939(사업소득+원천세 이체), 2025-4938(기타),
2026-5019(운영비 계좌 지급, 첨부 3종).
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

import expense as ex
import taxinvoice as tx
from expense import API, CONFIG, Client, ExpenseError, date_value, read_private, won, write_private


FORM_ID = "1210"
TRANSFER_FORM_ID = "1212"
INCOME = {
    "other": {"cashCd": "0300", "cashNm": "기타소득지급수수료", "label": "기타소득", "rate": "8.8%"},
    "business": {"cashCd": "0290", "cashNm": "사업소득지급수수료", "label": "사업소득", "rate": "3.3%"},
}
DOCUMENTS = (("confirmation", "비용지급확인서"), ("idCard", "신분증 사본"), ("bankbook", "통장 사본"))
OPERATING_NOTE = "본 사업({program})은 사업비 전용 계좌 없이, 운영비 계좌를 사용하고 있어 운영비 계좌에서 출금 요청드립니다."
EVIDENCE_OTHER = "9"   # 공통코드 A-11 증빙구분: 9 = 기타 (1=세금계산서, 2=계산서, 8=법인카드 …)
DRAFTS = CONFIG / "expert-drafts"
# 원천세를 모으는 회사 운영비 계좌. 근거: 사업비 관리 시트 머리말 "[지출결의 시 참고] MYSC 운영비 계좌",
# 실제 1212 문서(MYSC-2026-4112 등)의 수취 계좌 801…641·예금주 (주)엠와이소셜컴퍼니와 일치.
OPERATING = {"bankCd": "040", "bankNm": "국민", "account": "80173704008641", "holder": "(주)엠와이소셜컴퍼니"}
TRANSFER_CASH = "0360"   # 1212 양식 용도: 보통예금
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FILES = CONFIG / "state" / "expertfee-files"


def floor10(value):
    return int(value) // 10 * 10


def withholding(income_type, gross):
    """원천세. 기타소득(인적용역)은 필요경비 60%·과세최저한, 사업소득은 3%·소액부징수. 10원 미만 절사."""
    gross = int(gross)
    if gross <= 0:
        raise ExpenseError("지급액은 0보다 커야 합니다.")
    if income_type == "other":
        income = gross - floor10(gross * 0.6)
        if income <= 50000:
            return {"incomeTax": 0, "localTax": 0, "tax": 0, "net": gross, "incomeAmount": income,
                    "note": "기타소득금액 5만원 이하라 과세최저한으로 원천세가 없습니다."}
        income_tax = floor10(income * 0.2)
    elif income_type == "business":
        income = gross
        income_tax = floor10(gross * 0.03)
        if income_tax < 1000:
            return {"incomeTax": 0, "localTax": 0, "tax": 0, "net": gross, "incomeAmount": income,
                    "note": "소득세 1,000원 미만이라 소액부징수로 원천세가 없습니다."}
    else:
        raise ExpenseError("소득 구분은 other(기타소득) 또는 business(사업소득)입니다.")
    local_tax = floor10(income_tax * 0.1)
    return {"incomeTax": income_tax, "localTax": local_tax, "tax": income_tax + local_tax,
            "net": gross - income_tax - local_tax, "incomeAmount": income}


def read_confirmation(path):
    """비용지급확인서 PDF의 글자 층에서 체크된 소득유형과 금액만 읽는다. 스캔본이면 None(AI가 읽는다)."""
    path = Path(path).expanduser()
    if path.suffix.lower() != ".pdf":
        return None
    try:
        text = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, check=True).stdout.decode("utf-8", "replace")
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return parse_confirmation(text)


def parse_confirmation(text):
    if not text.strip():
        return None
    checked = re.findall(r"(?:■|◼️|◼|☑|✔|✓|\bV\b)\s*(사업소득|기타소득)", text)
    amounts = [won(value.replace(",", "")) for value in re.findall(r"([\d,]{5,})\s*원", text)]
    income_type = {"사업소득": "business", "기타소득": "other"}.get(checked[0]) if len(set(checked)) == 1 else None
    digits = re.sub(r"\D", "", text)
    births = sorted({m[1] for m in re.finditer(r"(?<!\d)(\d{6})\s*-?\s*[1-8]\d{6}(?!\d)", text)})
    return {"incomeType": income_type, "amounts": amounts, "digits": digits, "birthFronts": births}


def jpeg_to_pdf(source, target):
    """신분증 사진(JPEG)을 한 장짜리 PDF로 감싼다. 전자결재 첨부는 PDF만 올린다."""
    raw = Path(source).read_bytes()
    if not raw.startswith(b"\xff\xd8"):
        raise ExpenseError(f"JPEG 이미지가 아닙니다: {Path(source).name}")
    index, width, height = 2, None, None
    while index < len(raw) - 9:
        if raw[index] != 0xFF:
            index += 1
            continue
        marker = raw[index + 1]
        length = int.from_bytes(raw[index + 2:index + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            height = int.from_bytes(raw[index + 5:index + 7], "big")
            width = int.from_bytes(raw[index + 7:index + 9], "big")
            break
        index += 2 + length
    if not width or not height:
        raise ExpenseError(f"이미지 크기를 읽을 수 없습니다: {Path(source).name}")
    scale = min(500 / width, 700 / height, 1)
    w, h = round(width * scale), round(height * scale)
    content = f"q {w} 0 0 {h} 50 {792 - 50 - h} cm /Im0 Do Q".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n"
        % (width, height, len(raw)) + raw + b"\nendstream",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    Path(target).write_bytes(bytes(out))
    return Path(target)


IMAGE_CONVERT = (".png", ".heic", ".heif", ".webp", ".tif", ".tiff")
SCAN_SUFFIXES = (".pdf", ".jpg", ".jpeg") + IMAGE_CONVERT
DOC_KEYWORDS = {
    "confirmation": ("비용지급확인서", "지급확인서", "확인서", "지급신청서", "지급내역서"),
    "idCard": ("신분증", "주민등록증", "운전면허", "면허증", "여권", "외국인등록"),
    "bankbook": ("통장", "계좌", "bankbook"),
    "resume": ("이력서", "경력", "프로필", "resume", "cv"),
    "quote": ("견적서", "산출내역", "견적"),
    "businessReg": ("사업자등록증", "사업자등록"),
}
# 필수 3종 다음에 근거로 함께 첨부하는 서류(있을 때만)
EXTRA_DOCUMENTS = (("quote", "견적서"), ("businessReg", "사업자등록증"))
CORPORATE = re.compile(r"(주식회사|\(주\)|㈜|유한회사|법인|재단|사단|협동조합)")
# 파일·폴더 이름에서 사람 이름을 고를 때 빼는 말
NAME_STOPWORDS = {word for words in DOC_KEYWORDS.values() for word in words} | {
    "사본", "원본", "스캔", "최종", "수정", "자문비", "심사비", "강의비", "강사비", "연사비", "사례비", "자문", "심사",
    "강의", "전문가", "활용비", "지출결의", "증빙", "서류", "첨부", "개인정보", "동의서", "제공", "사업", "다운로드", "바탕화면", "문서"}


def to_jpeg(source, target):
    """PNG·HEIC 같은 사진을 macOS 기본 도구(sips)로 JPEG로 바꾼다."""
    try:
        subprocess.run(["sips", "-s", "format", "jpeg", str(source), "--out", str(target)], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise ExpenseError(f"이미지를 JPEG로 바꾸지 못했습니다: {Path(source).name}. PDF나 JPEG로 저장해 주세요.") from None
    return Path(target)


def document_kinds(path):
    """파일 이름에 들어 있는 서류 종류를 모두 돌려준다. '신분증, 통장사본_갈민정.pdf'는 신분증·통장 둘 다."""
    text = path.stem.lower()
    kinds = [kind for kind, words in DOC_KEYWORDS.items() if any(word.lower() in text for word in words)]
    if not kinds:
        folder = path.parent.name.lower()
        kinds = [kind for kind, words in DOC_KEYWORDS.items() if any(word.lower() in folder for word in words)]
    return kinds


def document_kind(path):
    kinds = document_kinds(path)
    return kinds[0] if kinds else None


def name_candidates(path, known=()):
    """파일 이름 → 괄호 안 → 폴더 이름 순서로 사람 이름(한글 2~4자)을 찾는다. 알려진 이름이 들어 있으면 그 이름."""
    texts = [path.stem, path.parent.name]
    for text in texts:
        for name in known:
            if name and name in text:
                return [name]
    found = []
    for text in texts:
        # 한글 단어를 통째로 본다. 이름은 2~4자라 '비용지급확인서' 같은 긴 단어는 쪼개지 않고 버린다.
        inside = re.findall(r"\(([가-힣]+)\)", text)
        words = inside + re.findall(r"[가-힣]+", re.sub(r"\([^)]*\)", " ", text))
        found.extend(word for word in words if 2 <= len(word) <= 4 and word not in NAME_STOPWORDS
                     and not any(stop in word for stop in NAME_STOPWORDS))
        if found:
            break
    return list(dict.fromkeys(found))


def scan_folders(folders, names=(), aliases=None):
    """흩어진 증빙 파일을 사람별·서류 종류별로 묶는다. 판단이 안 되는 파일은 unassigned로 남긴다(추측 금지)."""
    files = []
    for folder in folders:
        root = Path(folder).expanduser().resolve()
        if not root.exists():
            raise ExpenseError(f"폴더를 찾을 수 없습니다: {folder}")
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        files.extend(path for path in candidates if path.is_file() and path.suffix.lower() in SCAN_SUFFIXES
                     and not path.name.startswith("."))
    people, unassigned = {}, []
    aliases = dict(aliases or {})   # 영문 파일명 → 한글 이름 (예: "Minjung Kal" → "갈민정")
    known = list(names) + list(aliases.values())
    for path in files:
        kinds = document_kinds(path)
        alias = next((name for key, name in aliases.items() if key.lower() in path.name.lower()), None)
        found = [alias] if alias else name_candidates(path, known)
        if not kinds or len(found) != 1:
            unassigned.append({"path": str(path), "kind": kinds[0] if kinds else None, "names": found,
                               "reason": "서류 종류를 알 수 없음" if not kinds else ("이름 없음(영문이면 --alias)" if not found else "이름 후보가 여럿")})
            continue
        person = people.setdefault(found[0], {"name": found[0], "documents": {}, "extra": []})
        for kind in kinds:
            if kind in person["documents"]:
                person["extra"].append({"kind": kind, "path": str(path)})
            else:
                person["documents"][kind] = str(path)
    for person in people.values():
        confirmation = person["documents"].get("confirmation")
        person["fromConfirmation"] = None
        if confirmation:
            parsed = read_confirmation(confirmation)
            if parsed:
                person["fromConfirmation"] = {"incomeType": parsed["incomeType"], "amounts": parsed["amounts"],
                                              "birthFronts": parsed["birthFronts"]}
        person["missing"] = [label for key, label in DOCUMENTS if key not in person["documents"]]
    return {"people": sorted(people.values(), key=lambda item: item["name"]), "unassigned": unassigned, "fileCount": len(files)}


def init_spec(scan, common=None):
    """scan 결과로 spec 뼈대를 만든다. 확인서에서 한 값으로 정해진 것만 미리 채우고 나머지는 비워 둔다."""
    payees = []
    for person in scan["people"]:
        found = person.get("fromConfirmation") or {}
        amounts = sorted(set(found.get("amounts") or []))
        births = found.get("birthFronts") or []
        payees.append({"name": person["name"], "purpose": None, "incomeType": found.get("incomeType"),
                       "gross": amounts[-1] if len(amounts) == 1 else None,
                       "bank": None, "account": None, "accountDisplay": None, "holder": None, "accountSource": None,
                       "rrnFront": births[0] if len(births) == 1 else None, "bankbookBusiness": None,
                       "documents": {key: person["documents"][key] for key, _ in DOCUMENTS + EXTRA_DOCUMENTS if key in person["documents"]}})
    spec = {"program": None, "purpose": None, "memoTemplate": "{program}_{purpose}({name})", "project": None,
            "accountingDate": None, "funding": None, "payees": payees}
    spec.update({key: value for key, value in (common or {}).items() if value is not None})
    return spec


def scan_table(scan):
    mark = lambda person, key: "✓" if key in person["documents"] else "✗"
    lines = ["이름 | 확인서 | 신분증 | 통장 | 확인서 금액 | 소득유형"]
    for person in scan["people"]:
        found = person.get("fromConfirmation") or {}
        amount = ", ".join(f"{a:,}" for a in sorted(set(found.get("amounts") or []))) or "-"
        kind = INCOME.get(found.get("incomeType"), {}).get("label", "-")
        lines.append(f"{person['name']} | {mark(person, 'confirmation')} | {mark(person, 'idCard')} | {mark(person, 'bankbook')} | {amount} | {kind}")
    return lines


def memo_for(spec, payee):
    """적요. 사람마다 용도가 다르면(서류 심사비·발표 심사비) payee.purpose가 앞선다. 이름은 한글 이름(name)."""
    if payee.get("memo"):          # 사람마다 사업명이 다르면 확인받은 적요를 그대로 쓴다
        return payee["memo"]
    template = spec.get("memoTemplate") or "{program}_{purpose}({name})"
    return template.format(program=spec["program"], purpose=payee.get("purpose") or spec["purpose"], name=payee["name"])


def common_purpose(purposes):
    """사람별 용도의 공통 부분. '해외 데모데이(Singapore) 서류 심사비'·'… 발표 심사비' → '해외 데모데이(Singapore) 심사비'.
    앞쪽 공통 단어 + 뒤쪽 공통 단어. 공통이 없으면 None(사람에게 묻는다)."""
    words = [str(p or "").split() for p in purposes]
    if not words or not all(words):
        return None
    if all(w == words[0] for w in words):
        return " ".join(words[0])
    head = []
    for group in zip(*words):
        if len(set(group)) != 1:
            break
        head.append(group[0])
    tail = []
    for group in zip(*(list(reversed(w)) for w in words)):
        if len(set(group)) != 1 or len(head) + len(tail) >= min(map(len, words)):
            break
        tail.append(group[0])
    joined = head + list(reversed(tail))
    return " ".join(joined) or None


def title_for(spec, payees):
    """제목 규칙(실제 사례): 한 명이면 적요와 같다(5019·4940). 여러 명이면 '사업_공통 용도(첫 사람 외 N인)'(4446).
    spec.titleStyle이 'total'이면 2025 형식 '사업_공통 용도_(총N인)'(1509)."""
    if spec.get("title"):
        return spec["title"]
    if len(payees) == 1:
        return memo_for(spec, payees[0])
    purpose = common_purpose([payee.get("purpose") or spec.get("purpose") for payee in payees]) or spec.get("purpose")
    if spec.get("titleStyle") == "total":
        return f"{spec['program']}_{purpose}_(총{len(payees)}인)"
    return f"{spec['program']}_{purpose}({payees[0]['name']} 외 {len(payees) - 1}인)"


def transfer_title(spec, names, purpose):
    """1212 원천세 이체 제목(4112·5649·5337): '자금이체_{출금 계좌} -> 국민 운영비 계좌_{용도} 원천세 이체(이름, …)'."""
    who = names[0] if len(names) == 1 else (", ".join(names) if len(names) <= 5 else f"{len(names)}인")
    return f"자금이체_{spec.get('projectAccount') or '<사업 계좌>'} -> 국민 운영비 계좌_{purpose} 원천세 이체({who})"


def attachments_for(spec, payees, workdir):
    """첨부 순서와 이름: 사람마다 '번호. 서류명(이름)'. 그룹웨어 파일명이 되도록 그 이름으로 복사하고,
    이미지는 PDF로 바꿔 붙인다."""
    folder = Path(workdir) / secrets.token_hex(6)
    folder.mkdir(mode=0o700, parents=True)
    files, number = [], 1
    for payee in payees:
        documents = payee.get("documents") or {}
        # 같은 파일이 여러 서류를 담으면(신분증+통장) 한 번만, 이름은 서류명을 이어 붙인다.
        groups = {}
        for key, label in DOCUMENTS + EXTRA_DOCUMENTS:
            if documents.get(key):
                groups.setdefault(str(Path(documents[key]).expanduser().resolve()), []).append(label)
        for source, labels in groups.items():
            source = Path(source)
            label = ", ".join(labels) if len(labels) == 1 else ", ".join(l.replace(" 사본", "") for l in labels[:-1]) + ", " + labels[-1]
            name = f"{number}. {label}({payee['name']})"
            target = folder / f"{name}.pdf"
            if source.suffix.lower() in (".jpg", ".jpeg"):
                jpeg_to_pdf(source, target)
            elif source.suffix.lower() in IMAGE_CONVERT:
                jpeg_to_pdf(to_jpeg(source, folder / f"{number}.jpg"), target)
            elif source.suffix.lower() == ".pdf" and source.read_bytes().startswith(b"%PDF-"):
                shutil.copyfile(source, target)
            else:
                raise ExpenseError(f"PDF·이미지(JPEG·PNG·HEIC)만 첨부할 수 있습니다: {source.name}")
            target.chmod(0o600)
            raw = target.read_bytes()
            files.append({"name": name, "path": str(target), "source": str(source),
                          "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
            number += 1
    return files


def build_plan(spec, workdir):
    """spec(AI가 서류를 읽고 사용자와 확인한 값)으로 1210·1212 검토안을 만든다. 네트워크를 쓰지 않는다."""
    issues, warnings, payments, transfers = [], [], [], []
    payees = spec.get("payees") or []
    if not payees:
        raise ExpenseError("지급 대상(payees)이 없습니다.")
    for key in ("program", "purpose", "accountingDate", "funding"):
        if not spec.get(key):
            issues.append(f"{key} 확인 필요")
    funding = spec.get("funding")
    if funding not in (None, "operating", "project"):
        raise ExpenseError("funding은 operating(운영비 계좌에서 지급) 또는 project(사업 전용 계좌에서 지급)입니다.")
    for index, payee in enumerate(payees, 1):
        label = f"{index}번째({payee.get('name') or '이름 없음'})"
        for key in ("name", "incomeType", "gross", "bank", "account", "holder"):
            if payee.get(key) in (None, ""):
                issues.append(f"{label}: {key} 확인 필요")
        if payee.get("rrn"):
            raise ExpenseError("주민등록번호 전체는 받지 않습니다. 대조용 앞 6자리만 rrnFront로 넣으세요.")
        if payee.get("rrnFront") and not re.fullmatch(r"\d{6}", str(payee["rrnFront"])):
            issues.append(f"{label}: rrnFront는 생년월일 6자리입니다.")
        # 은행·계좌번호는 지급 사고와 직결된다. 통장 사본에서 읽은 값만 받는다.
        if payee.get("account") and payee.get("accountSource") != "bankbook":
            issues.append(f"{label}: 계좌는 통장 사본에서 읽어 accountSource: \"bankbook\"으로 넣어야 합니다(확인서 손글씨 계좌는 대조용).")
        account_digits = re.sub(r"\D", "", str(payee.get("account") or ""))
        if account_digits and not 10 <= len(account_digits) <= 16:
            issues.append(f"{label}: 계좌번호가 {len(account_digits)}자리입니다. 통장 사본을 다시 확인하세요.")
        income_type = payee.get("incomeType")
        if income_type not in INCOME or not payee.get("gross"):
            continue
        gross = int(payee["gross"])
        tax = withholding(income_type, gross)
        account = re.sub(r"\D", "", str(payee.get("account") or ""))
        documents = payee.get("documents") or {}
        waived = set(payee.get("documentsWaived") or [])
        for key, name in DOCUMENTS:
            if not documents.get(key):
                if key in waived:
                    warnings.append(f"{label}: {name} 없이 올립니다(사용자 확인). 재경팀이 요청할 수 있습니다.")
                else:
                    issues.append(f"{label}: {name} 첨부 필요")
        # 법인에 주는 용역 대가는 원천징수(사업·기타소득)가 아니라 세금계산서로 처리한다.
        if CORPORATE.search(str(payee.get("holder") or "") + " " + str(payee.get("name") or "")) or documents.get("businessReg"):
            message = f"{label}: 사업자(법인)에게 주는 대가는 원천징수 대상이 아닙니다. 세금계산서를 받아 세금계산서 흐름으로 처리하세요."
            if payee.get("corporateOverrideForTest"):
                warnings.append(message + " (테스트 전용으로 진행)")
            else:
                issues.append(message)
        confirmation = read_confirmation(documents["confirmation"]) if documents.get("confirmation") else None
        if confirmation:
            if confirmation["incomeType"] and confirmation["incomeType"] != income_type:
                issues.append(f"{label}: 비용지급확인서에는 {INCOME[confirmation['incomeType']]['label']}로 체크돼 있습니다.")
            if confirmation["amounts"] and gross not in confirmation["amounts"]:
                issues.append(f"{label}: 비용지급확인서 금액({', '.join(f'{a:,}' for a in confirmation['amounts'])}원)과 지급액 {gross:,}원이 다릅니다.")
            if account and account not in confirmation["digits"]:
                # 확인서 계좌는 손글씨가 많다. 통장 사본이 기준이고, 다르면 사람이 확인해야 넘어간다.
                message = f"{label}: 비용지급확인서의 계좌번호가 통장 사본({payee.get('accountDisplay') or payee.get('account')})과 다릅니다."
                if payee.get("accountConfirmed"):
                    warnings.append(message + " 통장 사본 기준으로 올립니다(확인됨).")
                else:
                    issues.append(message + " 통장 사본을 다시 보고, 맞으면 accountConfirmed: true로 확인하세요.")
            if payee.get("rrnFront") and str(payee["rrnFront"]) not in confirmation["digits"]:
                warnings.append(f"{label}: 비용지급확인서에서 생년월일({payee['rrnFront']})을 찾지 못했습니다. 신분증과 직접 대조하세요.")
        elif documents.get("confirmation"):
            warnings.append(f"{label}: 비용지급확인서에 글자 층이 없어 AI가 읽은 값으로만 대조합니다.")
        if payee.get("holder") and payee.get("name") and payee["holder"].replace(" ", "") != payee["name"].replace(" ", ""):
            # 영문 예금주(HONG JOONKEE ↔ 홍준기)처럼 같은 사람일 수 있다. 사람이 확인하면 통과.
            if payee.get("holderConfirmed"):
                warnings.append(f"{label}: 거래처·예금주는 통장 이름 '{payee['holder']}', 적요는 '{payee['name']}'로 올립니다.")
            else:
                issues.append(f"{label}: 통장 예금주({payee['holder']})가 이름과 다릅니다. 같은 사람이면 holderConfirmed: true로 확인하세요.")
        if income_type == "other" and payee.get("bankbookBusiness"):
            warnings.append(f"{label}: 통장이 사업용 계좌입니다. 반복적으로 용역을 제공하는 사업자면 사업소득(3.3%)일 수 있어 재경팀 확인을 권합니다.")
        if tax.get("note"):
            warnings.append(f"{label}: {tax['note']}")
        memo = memo_for(spec, payee)
        payments.append({"name": payee["name"], "purpose": payee.get("purpose") or spec.get("purpose"), "incomeType": income_type,
                         "project": payee.get("project") or spec.get("project"), **{k: INCOME[income_type][k] for k in ("cashCd", "cashNm")},
                     "memo": memo, "gross": gross, "vat": 0, **tax, "bank": payee.get("bank"), "account": account,
                     "holder": payee.get("holder"), "accountDisplay": payee.get("accountDisplay") or payee.get("account"),
                     "evidence": "기타", "evidenceDate": spec.get("accountingDate")})
        if funding == "project" and tax["tax"]:
            purpose = payee.get("purpose") or spec.get("purpose")
            transfers.append({"name": payee["name"], "amount": tax["tax"], "incomeType": income_type,
                              "memo": transfer_title(spec, [payee["name"]], purpose)})
    # 적요·제목·프로젝트는 과거 사례로 초안을 쓰고 사람에게 보여준 뒤에만 저장한다.
    if not spec.get("memoConfirmed"):
        issues.append("적요·제목 초안을 사용자에게 보여주고 확인받은 뒤 memoConfirmed: true로 표시하세요.")
    if not spec.get("project") and not all(payee.get("project") for payee in payees):
        issues.append("project 확인 필요 (문서 공통 또는 사람별)")
    title = title_for(spec, payees) if spec.get("program") and payees else ""
    if not spec.get("titleConfirmed"):
        issues.append(f"제목 '{title}'을 사용자에게 따로 보여주고 확인받은 뒤 titleConfirmed: true로 표시하세요.")
    if spec.get("program") and title and not title.startswith(str(spec["program"])):
        warnings.append(f"제목이 사업명({spec['program']})으로 시작하지 않습니다. 실제 결의 제목은 모두 사업명으로 시작합니다.")
    programs = {payee.get("program") or spec.get("program") for payee in payees}
    projects = {payee.get("project") or spec.get("project") for payee in payees}
    if len(programs) > 1 or len(projects) > 1:
        issues.append("사업(프로젝트)이 다른 사람이 한 문서에 섞였습니다. 제목을 규칙대로 만들 수 없으니 사업별로 문서를 나누세요.")
    if not spec.get("projectConfirmed"):
        issues.append("프로젝트를 사용자에게 확인받은 뒤 projectConfirmed: true로 표시하세요.")
    if funding == "project" and not spec.get("projectAccount"):
        issues.append("사업 전용 계좌 표기(projectAccount)가 필요합니다. 과거 1212 제목과 같은 표기로. 예: '국민 2026핀테크 236243', '신한 25 중서원 130549 계좌'")
    notes = spec.get("notes") or ([OPERATING_NOTE.format(program=spec.get("program"))] if funding == "operating" else ["-"])
    withdraw = spec.get("withdraw") or (["국민 운영비 계좌"] if funding == "operating" else [spec.get("projectAccount") or ""])
    plan = {
        "schema": 1, "kind": "expertfee", "formId": FORM_ID,
        "head": {"title": title_for(spec, payees), "accountingDate": spec.get("accountingDate"), "project": spec.get("project"),
                 "payDate": spec.get("payDate")},
        "payments": payments, "body": {"withdraw": withdraw, "notes": notes},
        "attachments": attachments_for(spec, payees, workdir),
        "transfer": {"formId": TRANSFER_FORM_ID, "required": bool(transfers), "rows": transfers, "plan": None,
                     "title": transfer_title(spec, [r["name"] for r in transfers],
                                             common_purpose([p.get("purpose") or spec.get("purpose") for p in payees if p.get("name") in {r["name"] for r in transfers}]) or spec.get("purpose")) if transfers else None,
                     "total": sum(item["amount"] for item in transfers)} if funding == "project" else {"required": False, "plan": None,
                     "reason": "운영비 계좌에서 바로 지급해 원천세 이체 문서가 필요 없습니다."},
        "totals": {"gross": sum(r["gross"] for r in payments), "tax": sum(r["tax"] for r in payments), "net": sum(r["net"] for r in payments)},
        "issues": issues, "warnings": warnings,
    }
    if plan["transfer"].get("required"):
        plan["transfer"]["plan"] = build_transfer(spec, plan, workdir)
    return plan


def tax_calc_pdf(spec, payments, target):
    """첨부 '0. 원천세 계산': 사람별 총지급액·소득유형·소득세·지방소득세·원천세·실지급액 표. Chrome으로 PDF 인쇄."""
    import html as _html
    rows = "".join(
        f"<tr><td>{i}</td><td>{_html.escape(p['name'])}</td><td>{INCOME[p['incomeType']]['label']} {INCOME[p['incomeType']]['rate']}</td>"
        f"<td>{p['gross']:,}</td><td>{p['incomeTax']:,}</td><td>{p['localTax']:,}</td><td><b>{p['tax']:,}</b></td><td>{p['net']:,}</td></tr>"
        for i, p in enumerate(payments, 1))
    total = {k: sum(p[k] for p in payments) for k in ("gross", "incomeTax", "localTax", "tax", "net")}
    page = f"""<!doctype html><meta charset="utf-8"><style>
body{{font-family:'Apple SD Gothic Neo',sans-serif;margin:32px;font-size:12px}} h1{{font-size:16px}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #999;padding:6px 8px;text-align:right}}
td:nth-child(2),td:nth-child(3),th{{text-align:center}} tfoot td{{font-weight:bold;background:#f3f3f3}}
p{{color:#444;line-height:1.6}}</style>
<h1>원천세 계산 — {_html.escape(spec.get('program') or '')} {_html.escape(spec.get('purpose') or '')}</h1>
<p>회계일 {spec.get('accountingDate') or ''} · 출금 {_html.escape(spec.get('projectAccount') or '')} → 국민 운영비 계좌</p>
<table><thead><tr><th>No</th><th>성명</th><th>소득구분</th><th>총지급액</th><th>소득세</th><th>지방소득세</th><th>원천세 합계</th><th>실지급액</th></tr></thead>
<tbody>{rows}</tbody><tfoot><tr><td colspan="3">합계</td><td>{total['gross']:,}</td><td>{total['incomeTax']:,}</td>
<td>{total['localTax']:,}</td><td>{total['tax']:,}</td><td>{total['net']:,}</td></tr></tfoot></table>
<p>기타소득: 필요경비 60% 공제 후 소득세 20%, 지방소득세 = 소득세의 10% (소득금액 5만원 이하 과세최저한).<br>
사업소득: 소득세 3%, 지방소득세 = 소득세의 10% (소득세 1,000원 미만 소액부징수). 10원 미만 절사.</p>"""
    source = Path(target).with_suffix(".html")
    source.write_text(page, encoding="utf-8")
    try:
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={target}", source.as_uri()], capture_output=True, check=True, timeout=60)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ExpenseError("원천세 계산표 PDF를 만들지 못했습니다(Chrome 필요).") from None
    finally:
        source.unlink(missing_ok=True)
    if not Path(target).exists() or not Path(target).read_bytes().startswith(b"%PDF-"):
        raise ExpenseError("원천세 계산표 PDF를 만들지 못했습니다.")
    Path(target).chmod(0o600)
    return Path(target)


def build_transfer(spec, plan, workdir):
    """1212 원천세 자금이체 검토안(사업 전용 계좌에서 지급할 때만). 1210 검토안과 같은 모양이라 같은 저장 흐름을 쓴다.
    사람마다 한 행(원천세 0원인 사람은 없음), 계정 0360 보통예금, 수취 = 운영비 계좌, 첨부 = 원천세 계산 + 비용지급확인서."""
    taxed = [p for p in plan["payments"] if p["tax"]]
    if spec.get("funding") != "project" or not taxed:
        return None
    issues, warnings = [], []
    if not spec.get("transferConfirmed"):
        issues.append("원천세 이체(1212)를 함께 올릴지 사용자에게 묻고, 올리기로 하면 transferConfirmed: true로 표시하세요.")
    folder = Path(workdir) / secrets.token_hex(6)
    folder.mkdir(mode=0o700, parents=True)
    calc = tax_calc_pdf(spec, taxed, folder / "0. 원천세 계산.pdf")
    raw = calc.read_bytes()
    attachments = [{"name": "0. 원천세 계산", "path": str(calc), "source": "generated",
                    "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}]
    payees = {p["name"]: p for p in spec.get("payees") or []}
    for index, payment in enumerate(taxed, 1):
        source = (payees.get(payment["name"], {}).get("documents") or {}).get("confirmation")
        if not source:
            warnings.append(f"{payment['name']}: 비용지급확인서가 없어 원천세 이체 첨부에서 빠집니다.")
            continue
        target = folder / f"{index}-1. 비용지급확인서({payment['name']}).pdf"
        shutil.copyfile(Path(source).expanduser(), target)
        target.chmod(0o600)
        data = target.read_bytes()
        attachments.append({"name": target.stem, "path": str(target), "source": str(source),
                            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    purpose = common_purpose([p.get("purpose") for p in taxed]) or spec.get("purpose")
    return {"schema": 1, "kind": "expertfee-transfer", "formId": TRANSFER_FORM_ID,
            "head": {"title": spec.get("transferTitle") or transfer_title(spec, [p["name"] for p in taxed], purpose),
                     "accountingDate": spec.get("accountingDate"), "project": spec.get("project"), "payDate": spec.get("payDate")},
            "payments": [{"name": p["name"], "amount": p["tax"], "incomeType": p["incomeType"], "project": p.get("project"),
                          "memo": transfer_title(spec, [p["name"]], p.get("purpose") or spec.get("purpose"))} for p in taxed],
            "body": {"withdraw": [spec.get("projectAccount") or ""], "notes": spec.get("transferNotes") or ["-"]},
            "attachments": attachments, "totals": {"tax": sum(p["tax"] for p in taxed)},
            "issues": issues + [i for i in plan["issues"] if "사업 전용 계좌" in i], "warnings": warnings}


def transfer_summary(transfer):
    sub = transfer.get("plan")
    if not sub:
        return {"필요": transfer.get("required", False), "이유": transfer.get("reason")}
    return {"필요": True, "제목": sub["head"]["title"],
            "행": [{"대상": p["name"], "원천세": p["amount"], "적요": p["memo"]} for p in sub["payments"]],
            "합계": sub["totals"]["tax"], "수취": f"{OPERATING['bankNm']} {OPERATING['account']} {OPERATING['holder']} (운영비 계좌)",
            "출금계좌": sub["body"]["withdraw"], "첨부": [a["name"] for a in sub["attachments"]],
            "확인필요": sub["issues"], "참고": sub["warnings"]}


def summary(plan):
    return {
        "문서": f"{plan['formId']} 지출결의", "제목": plan["head"]["title"], "회계일": plan["head"]["accountingDate"],
        "프로젝트": plan["head"]["project"],
        "행": [{"지급대상": r["name"], "계정": f"{r['cashCd']} {r['cashNm']}", "적요": r["memo"], "증빙": r["evidence"],
               "총지급액": r["gross"], "원천세": r["tax"], "실지급액": r["net"],
               "수취(통장 사본 기준)": f"{r['bank']} {r['accountDisplay']} {r['holder']}"} for r in plan["payments"]],
        "합계": plan["totals"], "출금계좌": plan["body"]["withdraw"], "비고": plan["body"]["notes"],
        "첨부": [item["name"] for item in plan["attachments"]],
        "원천세 이체(1212)": transfer_summary(plan["transfer"]),
        "확인필요": plan["issues"], "참고": plan["warnings"],
    }


def bank_code(banks, name):
    """'하나', '하나은행', 'KEB하나' → 810. 하나로 정해지지 않으면 None(사용자에게 묻는다)."""
    wanted = re.sub(r"\s|은행|\(.*?\)", "", str(name or ""))
    if not wanted:
        return None
    exact = [b for b in banks if b.get("bankNm") == wanted]
    found = exact or [b for b in banks if wanted in str(b.get("bankNm")) or str(b.get("bankNm")).replace("KEB", "") == wanted]
    return found[0] if len(found) == 1 else None


def project_code(projects, wanted):
    wanted = str(wanted or "").strip()
    exact = [p for p in projects if wanted in (p.get("pjtCd"), p.get("pjtNm"))]
    found = exact or [p for p in projects if wanted and wanted in str(p.get("pjtNm"))]
    return found[0] if len(found) == 1 else None


def gather(client, spec):
    """1210 저장에 필요한 그룹웨어 값(조회만)."""
    today = tx.today_kst()
    accounting = date_value(spec.get("accountingDate") or today)
    divisions = client.post(API + "0ap00017", {"empCd": client.erp["userCode"]})
    if len(divisions) != 1:
        raise ExpenseError("회계단위가 여러 개입니다. 적용할 회계단위 확인이 필요합니다.")
    since = (dt.datetime.strptime(accounting, "%Y%m%d") - dt.timedelta(days=366)).strftime("%Y%m%d")
    return {"today": today, "account": client.erp, "division": divisions[0],
            "form": tx.form_record(client, FORM_ID),
            "gisu": client.post("/system/financialApi/selectGisuData", {"frDt": accounting}),
            "dateRules": client.post("/personal/modulecommon/config/getConfig", {
                "syscfg": [{"moduleCd": "A", "ctrCd": key} for key in ("51", "60")]}),
            "cashTypes": client.post("/personal/APCodePicker/ApAcashcdTypeCode", {
                "helpTy": "ACASHCD_TYPE_CODE", "langkind": "KOR", "multiCode": [int(FORM_ID)]}),
            "projects": client.codepicker("SPJT_CODE_AUTH", empCd=client.erp["userCode"], useYn="ALL"),
            "banks": client.codepicker("SBANK_CODE", searchText=""),
            "documents": tx.tax_documents(client, since, today),
            **({"transferForm": tx.form_record(client, TRANSFER_FORM_ID),
                "transferCashTypes": client.post("/personal/APCodePicker/ApAcashcdTypeCode", {
                    "helpTy": "ACASHCD_TYPE_CODE", "langkind": "KOR", "multiCode": [int(TRANSFER_FORM_ID)]})}
               if spec.get("funding") == "project" else {})}


def erp_plan(plan, context):
    """검토안에 ERP 머리·행을 붙인다. 세금계산서(1210)와 같은 행 모양에 용도·증빙(기타)·거래처(사람)·수취 계좌만 다르다."""
    account, company = context["account"], context["division"]["divNm"]
    issues = plan["issues"]
    accounting = date_value(plan["head"]["accountingDate"] or context["today"])
    usable = {str(item["typeCd"]) for item in context["cashTypes"]}
    snapshot = {"dateRules": context["dateRules"], "accountingDate": accounting,
                "cashTypes": context["cashTypes"], "projects": context["projects"]}
    rows = []
    for index, payment in enumerate(plan["payments"], 1):
        # 사람(행)마다 프로젝트가 다를 수 있다(한 문서에 여러 사업). 없으면 문서 공통 프로젝트.
        wanted = payment.get("project") or plan["head"]["project"]
        project = project_code(context["projects"], wanted)
        if not project:
            issues.append(f"{index}행: 프로젝트 '{wanted}'를 하나로 찾지 못했습니다. 프로젝트 코드로 지정하세요.")
        bank = bank_code(context["banks"], payment["bank"])
        if not bank:
            issues.append(f"{index}행: 은행 '{payment['bank']}'의 코드를 하나로 찾지 못했습니다.")
        if payment["cashCd"] not in usable:
            issues.append(f"{index}행: 1210 양식에서 쓸 수 없는 용도({payment['cashCd']})입니다.")
        for duplicate in tx.find_duplicates(context["documents"], {"issued": accounting, "total": payment["gross"], "supplier": payment["holder"] or payment["name"]}):
            message = f"{index}행: 같은 날짜·금액·지급대상의 1210 문서가 있습니다 — {duplicate['docNo'] or '번호 없음'} (상태 {duplicate['state']}) {duplicate['title']}"
            (plan["warnings"] if duplicate["state"] == "4" else issues).append(message)
        row = {"coCd": account["companyCode"], "cardDt": "", "cardSq": 0, "sunginNb": "", "vatGroupDc": "",
               "cardCd": "", "cardNm": "", "cardTrCd": "", "cardFg": "",
               "issDt": accounting, "sumAm": payment["gross"], "vatAm": 0, "supAm": payment["gross"],
               "trCd": "", "trNm": payment["holder"] or payment["name"], "regNb": "",
               "empCd": account["userCode"], "empNm": account["userName"],
               "deptCd": account["deptCode"], "deptNm": account["deptName"],
               "bankCd": (bank or {}).get("bankCd", ""), "bankNm": (bank or {}).get("bankNm", ""),
               "baNb": payment["account"], "depositorDc": payment["holder"] or "",
               "realDepositorDc": "", "realDepositorFg": "",
               "cashCd": payment["cashCd"], "cashNm": "", "rmkDc": payment["memo"],
               "pjtCd": (project or {}).get("pjtCd", ""), "pjtNm": "", "carCd": "", "carNb": "",
               "issNo": "", "issSq": 0, "cashDt": "", "cashSq": None, "taxTy": "",
               "payDt": date_value(plan["head"].get("payDate") or context["today"]),
               "fileGroup": "0", "fileDc": "", "fileInfoList": [], "budgetSts": "0",
               "formId": int(FORM_ID), "linkKey": "", "attrCd": EVIDENCE_OTHER}
        issues.extend(f"{index}행: {message}" for message in ex.row_issues(row, snapshot))
        rows.append(row)
    if accounting[:6] < context["today"][:6]:
        plan["warnings"].append(f"{int(accounting[4:6])}월 회계분입니다. 재경팀 월마감 전에 상신·결재까지 끝내야 합니다.")
    plan["head"] = {**plan["head"], "coCd": account["companyCode"], "empCd": account["userCode"], "empNm": account["userName"],
                    "deptCd": account["deptCode"], "deptNm": account["deptName"],
                    "divCd": context["division"]["divCd"], "divNm": company,
                    "gisu": context["gisu"]["gisu"], "isuDt": accounting, "docTitle": plan["head"]["title"],
                    "docugroupYn": "Y", "onlyCashFg": "1", "fileGroup": "0", "fileDc": "",
                    "pjtCd": "", "pjtNm": "", "consDocTitle": "", "exdocuYn": "N"}
    plan["rows"] = rows
    plan["source"] = {"form": context["form"], "gisu": context["gisu"]}
    return plan


def erp_transfer(sub, context):
    """1212 원천세 이체 ERP 행: 0360 보통예금, 증빙 9(기타), 거래처 없음, 수취 = 운영비 계좌."""
    account = context["account"]
    accounting = date_value(sub["head"]["accountingDate"] or context["today"])
    usable = {str(item["typeCd"]) for item in context["transferCashTypes"]}
    if TRANSFER_CASH not in usable:
        sub["issues"].append("1212 양식에서 보통예금(0360) 용도를 쓸 수 없습니다.")
    snapshot = {"dateRules": context["dateRules"], "accountingDate": accounting,
                "cashTypes": context["transferCashTypes"], "projects": context["projects"]}
    rows = []
    for index, payment in enumerate(sub["payments"], 1):
        wanted = payment.get("project") or sub["head"]["project"]
        project = project_code(context["projects"], wanted)
        if not project:
            sub["issues"].append(f"{index}행: 프로젝트 '{wanted}'를 하나로 찾지 못했습니다.")
        row = {"coCd": account["companyCode"], "cardDt": "", "cardSq": 0, "sunginNb": "", "vatGroupDc": "",
               "cardCd": "", "cardNm": "", "cardTrCd": "", "cardFg": "",
               "issDt": accounting, "sumAm": payment["amount"], "vatAm": 0, "supAm": payment["amount"],
               "trCd": "", "trNm": "", "regNb": "",
               "empCd": account["userCode"], "empNm": account["userName"],
               "deptCd": account["deptCode"], "deptNm": account["deptName"],
               "bankCd": OPERATING["bankCd"], "bankNm": OPERATING["bankNm"], "baNb": OPERATING["account"],
               "depositorDc": OPERATING["holder"], "realDepositorDc": "", "realDepositorFg": "",
               "cashCd": TRANSFER_CASH, "cashNm": "", "rmkDc": payment["memo"],
               "pjtCd": (project or {}).get("pjtCd", ""), "pjtNm": "", "carCd": "", "carNb": "",
               "issNo": "", "issSq": 0, "cashDt": "", "cashSq": None, "taxTy": "",
               "payDt": date_value(sub["head"].get("payDate") or context["today"]),
               "fileGroup": "0", "fileDc": "", "fileInfoList": [], "budgetSts": "0",
               "formId": int(TRANSFER_FORM_ID), "linkKey": "", "attrCd": EVIDENCE_OTHER}
        sub["issues"].extend(f"{index}행: {message}" for message in ex.row_issues(row, snapshot))
        rows.append(row)
    sub["head"] = {**sub["head"], "coCd": account["companyCode"], "empCd": account["userCode"], "empNm": account["userName"],
                   "deptCd": account["deptCode"], "deptNm": account["deptName"],
                   "divCd": context["division"]["divCd"], "divNm": context["division"]["divNm"],
                   "gisu": context["gisu"]["gisu"], "isuDt": accounting, "docTitle": sub["head"]["title"],
                   "docugroupYn": "Y", "onlyCashFg": "1", "fileGroup": "0", "fileDc": "",
                   "pjtCd": "", "pjtNm": "", "consDocTitle": "", "exdocuYn": "N"}
    sub["rows"] = rows
    sub["source"] = {"form": context["transferForm"], "gisu": context["gisu"]}
    return sub


def validate_expert(client, plan):
    """저장 직전 현재 그룹웨어 상태로 다시 확인한다. 쓰기 없음."""
    head = plan["head"]
    if plan.get("kind") not in ("expertfee", "expertfee-transfer"):
        raise ExpenseError("전문가 활용비 검토안이 아닙니다.")
    if str(head["coCd"]) != str(client.erp["companyCode"]) or str(head["empCd"]) != str(client.erp["userCode"]):
        raise ExpenseError("검토 파일과 현재 로그인 계정이 다릅니다.")
    if plan["issues"]:
        raise ExpenseError("확인할 항목이 남았습니다:\n" + "\n".join(plan["issues"]))
    if any(not tx.plain_account(row["baNb"]) for row in plan["rows"]):
        raise ExpenseError("수취 계좌번호가 숫자가 아닙니다.")
    if any(row["attrCd"] != EVIDENCE_OTHER or row["vatAm"] != 0 for row in plan["rows"]):
        raise ExpenseError("전문가 활용비 행은 증빙 기타, 부가세 0원이어야 합니다.")
    for item in plan["attachments"]:
        if hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ExpenseError("첨부할 파일이 검토 이후 변경됐습니다: " + item["name"])
    since = (dt.datetime.strptime(head["isuDt"], "%Y%m%d") - dt.timedelta(days=366)).strftime("%Y%m%d")
    if plan["kind"] == "expertfee-transfer":
        # 같은 적요의 원천세 이체가 이미 있으면 막는다(반려 제외)
        own = client.post(API + "0ap00001", {"appDateFrom": since, "appDateTo": tx.today_kst(), "docTitle": "", "fromAm": "",
                                             "toAm": "", "docNo": "", "docStatus": "", "dateFg": "1"})
        memos = {row["rmkDc"] for row in plan["rows"]}
        for document in own:
            if str(document.get("formId")) != TRANSFER_FORM_ID or str(document.get("approState")) in ("-3", "4"):
                continue
            if memos & {row.get("rmkDc") for row in client.document(document["linkKey"])["detail"]}:
                raise ExpenseError(f"같은 적요의 원천세 이체(1212) 문서가 이미 있습니다: {document.get('docTitle')}")
    documents = tx.tax_documents(client, since, tx.today_kst()) if plan["kind"] == "expertfee" else []
    for payment in plan["payments"] if plan["kind"] == "expertfee" else []:
        blocking = [d for d in tx.find_duplicates(documents, {"issued": head["isuDt"], "total": payment["gross"], "supplier": payment["holder"] or payment["name"]})
                    if d["state"] != "4"]
        if blocking:
            raise ExpenseError(f"{payment['name']} {payment['gross']:,}원과 같은 1210 문서가 이미 있습니다: "
                               + ", ".join(f"{d['docNo'] or d['linkKey']}(상태 {d['state']})" for d in blocking))
    close_date = client.post(API + "0ap00050", {"divCd": head["divCd"]})
    if close_date and close_date != "00000000" and head["isuDt"] <= str(close_date):
        raise ExpenseError("회계처리일자가 지출결의 마감일 이전입니다.")


def verify_expert(client, journal):
    """저장 후 ERP 행·증빙구분·수취 계좌·본문 비고·첨부를 다시 읽어 검토안과 대조한다."""
    plan = journal["plan"]
    saved = client.document(journal["linkKey"])
    if str(saved["head"]["approState"]) != "5" or len(saved["detail"]) != len(plan["rows"]):
        raise ExpenseError(f"ERP 상태={saved['head']['approState']}, 행 수={len(saved['detail'])}. "
                           f"임시보관 상태 5와 {len(plan['rows'])}행이 필요합니다.")
    for field in ("isuDt", "docTitle", "empCd", "deptCd", "divCd"):
        if str(saved["head"].get(field) or "") != str(plan["head"].get(field) or ""):
            raise ExpenseError("저장한 문서의 기본 정보가 검토안과 다릅니다: " + field)
    loaded_rows = sorted(saved["detail"], key=lambda row: int(row.get("lnSq") or 0))
    observed = []
    for loaded, row in zip(loaded_rows, plan["rows"]):
        for field in ("sumAm", "supAm", "vatAm", "issDt", "payDt", "cashCd", "rmkDc", "pjtCd", "trNm",
                      "empCd", "deptCd", "attrCd", "bankCd", "depositorDc"):
            left, right = loaded.get(field), row.get(field)
            differs = won(left) != won(right) if field in ("sumAm", "supAm", "vatAm") else str(left or "") != str(right or "")
            if differs:
                raise ExpenseError(f"저장 후 재조회 값이 다릅니다: {field} (보낸 값 {right!r}, 저장된 값 {left!r})")
        if loaded.get("attrNm") and loaded["attrNm"] != "기타":
            raise ExpenseError(f"저장된 증빙구분({loaded['attrNm']})이 기타가 아닙니다.")
        if str(loaded.get("decryptBaNb") or "") != row["baNb"]:
            raise ExpenseError("저장 후 재조회한 수취 계좌가 검토안과 다릅니다.")
        observed.append({key: loaded.get(key) for key in sorted(loaded) if key not in ("baNb", "decryptBaNb", "regNb")})
    journal["observedRows"] = observed
    eap = ex.eap_init(client, str(plan.get("formId") or FORM_ID), journal["approKey"], journal["docId"])
    info = eap["resultMap"]["appdocinfo"]
    if len(info) != 1 or str(info[0]["doc_sts"]) != "10" or info[0]["approkey"] != journal["approKey"]:
        raise ExpenseError("전자결재 임시보관 상태를 확인할 수 없습니다.")
    contents = eap["resultMap"]["appdoccontent"][0]["doc_contents"]
    if "%3C" in contents[:30].upper():
        contents = __import__("urllib.parse").parse.unquote(contents)
    if any(__import__("html").escape(line) not in contents for line in plan["body"]["notes"]):
        raise ExpenseError("저장된 본문에 비고 문장이 없습니다.")
    if len(eap["resultMap"]["fileAttachInfo"]) != len(plan["attachments"]):
        raise ExpenseError("전자결재 첨부 개수가 검토안과 다릅니다.")
    journal["status"] = "verified"


def project_candidates(client, keyword=None, days=365):
    """내가 올린 지출결의(모든 양식)에서 쓴 프로젝트를 많이 쓴 순·최근 순으로. keyword가 있으면
    권한 있는 전체 프로젝트에서 이름·코드가 맞는 것도 덧붙인다(사용 이력 0회)."""
    today = tx.today_kst()
    since = (dt.datetime.strptime(today, "%Y%m%d") - dt.timedelta(days=days)).strftime("%Y%m%d")
    documents = client.post(API + "0ap00001", {"appDateFrom": since, "appDateTo": today, "docTitle": "", "fromAm": "",
                                               "toAm": "", "docNo": "", "docStatus": "", "dateFg": "1"})
    used = {}
    for document in documents:
        if str(document.get("approState")) == "-3":
            continue
        try:
            rows = client.document(document["linkKey"])["detail"]
        except ExpenseError:
            continue
        for row in rows:
            code = row.get("pjtCd")
            if not code:
                continue
            entry = used.setdefault(code, {"pjtCd": code, "pjtNm": row.get("pjtNm") or "", "count": 0, "lastUsed": "", "forms": set()})
            entry["count"] += 1
            entry["lastUsed"] = max(entry["lastUsed"], row.get("issDt") or document.get("isuDt") or "")
            entry["forms"].add(str(document.get("formId")))
    ranked = sorted(used.values(), key=lambda item: (-item["count"], item["lastUsed"][::-1]))
    result = [{**item, "forms": sorted(item["forms"])} for item in ranked]
    if keyword:
        needle = keyword.lower()
        result = [item for item in result if needle in (item["pjtCd"] + item["pjtNm"]).lower()]
        known = {item["pjtCd"] for item in result}
        for project in client.codepicker("SPJT_CODE_AUTH", empCd=client.erp["userCode"], useYn="ALL"):
            if project.get("pjtCd") not in known and needle in (str(project.get("pjtCd")) + str(project.get("pjtNm"))).lower():
                result.append({"pjtCd": project["pjtCd"], "pjtNm": project.get("pjtNm") or "", "count": 0, "lastUsed": "", "forms": []})
    return result


def memo_examples(keyword, limit=8):
    """열람 가능한 과거 1210 문서에서 소득지급수수료 행의 실제 제목·적요를 모은다(조회만). 적요 초안의 근거."""
    import amaranth as am
    client = am.ReadClient()
    found = am.run(client, "document_search", {"title": keyword, "page_size": 30})
    results = []
    for document in found["documents"]:
        if str(document.get("formId")) != FORM_ID or len(results) >= limit:
            continue
        view = am.run(client, "document_view", {"doc_id": document["docId"]})
        memos = [row[2] for row in view["body"] if len(row) >= 3 and "소득지급수수료" in row[1]]
        if memos:
            results.append({"docNo": document["docNo"], "title": document["title"], "drafter": document["drafter"],
                            "date": (document.get("reportedAt") or "")[:10], "memos": memos})
    return results


def examples_command(args):
    print(json.dumps(memo_examples(args.keyword, args.limit), ensure_ascii=False, indent=2))


def projects_command(args):
    print(json.dumps(project_candidates(Client(), args.keyword)[:args.limit], ensure_ascii=False, indent=2))


def scan_command(args):
    aliases = dict(item.split("=", 1) for item in (args.alias or []))
    result = scan_folders(args.folder, args.name or (), aliases)
    if args.out:
        write_private(Path(args.out).expanduser(), result, replace=True)
    print("\n".join(scan_table(result)))
    print(json.dumps({"unassigned": result["unassigned"], "extraFiles": {p["name"]: p["extra"] for p in result["people"] if p["extra"]},
                      "missing": {p["name"]: p["missing"] for p in result["people"] if p["missing"]}}, ensure_ascii=False, indent=2))


def init_command(args):
    scan = read_private(Path(args.scan).expanduser())
    common = {"program": args.program, "purpose": args.purpose, "project": args.project,
              "accountingDate": args.accounting_date, "funding": args.funding}
    spec = init_spec(scan, common)
    write_private(Path(args.out).expanduser(), spec)
    empty = {p["name"]: [k for k in ("incomeType", "gross", "bank", "account", "holder", "rrnFront") if p.get(k) in (None, "")] for p in spec["payees"]}
    print(json.dumps({"spec": args.out, "채울 칸": empty,
                      "공통 칸": [k for k in ("program", "purpose", "project", "accountingDate", "funding") if not spec.get(k)]},
                     ensure_ascii=False, indent=2))


def tax_command(args):
    print(json.dumps({"type": INCOME[args.type]["label"], "gross": args.gross, **withholding(args.type, args.gross)}, ensure_ascii=False, indent=2))


def prepare_command(args):
    spec = json.loads(Path(args.spec).expanduser().read_text())
    FILES.mkdir(mode=0o700, parents=True, exist_ok=True)
    plan = build_plan(spec, FILES)
    context = gather(Client(), spec)
    plan = erp_plan(plan, context)
    if plan["transfer"].get("plan"):
        plan["transfer"]["plan"] = erp_transfer(plan["transfer"]["plan"], context)
    write_private(Path(args.out).expanduser(), plan)
    print(json.dumps(summary(plan), ensure_ascii=False, indent=2))


def check_command(args):
    plan = read_private(Path(args.plan).expanduser())
    if args.transfer:
        plan = (plan.get("transfer") or {}).get("plan")
        if not plan:
            raise ExpenseError("이 검토안에는 원천세 이체(1212)가 없습니다.")
    validate_expert(Client(), plan)
    print(f"저장 전 검증 통과: {len(plan['rows'])}명, 중복 문서 없음, 첨부 변경 없음, 마감일 확인")


def journal_key(plan):
    basis = "|".join([plan["kind"], plan["head"]["isuDt"], plan["head"]["docTitle"]]
                     + [f"{p['name']}:{p.get('gross', p.get('amount'))}" for p in plan["payments"]])
    return hashlib.sha256(("expert:" + basis).encode()).hexdigest()


def draft_command(args):
    client = Client()
    plan = read_private(Path(args.plan).expanduser())
    if plan.get("kind") != "expertfee":
        raise ExpenseError("전문가 활용비 검토안이 아닙니다.")
    if args.transfer:
        plan = (plan.get("transfer") or {}).get("plan")
        if not plan:
            raise ExpenseError("이 검토안에는 원천세 이체(1212)가 없습니다(운영비 계좌 지급이거나 원천세 0원).")
    DRAFTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = DRAFTS / (journal_key(plan) + ".json")
    with (DRAFTS / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            journal = read_private(path)
            if journal["plan"] != plan:
                raise ExpenseError("이 지급 건은 이미 저장을 시도했습니다. status로 기존 문서를 먼저 확인하세요.")
        else:
            journal = {"status": "prepared", "approKey": "ERP_" + "_".join(secrets.token_hex(2) for _ in range(5)), "plan": plan}
            write_private(path, journal)
        tx.continue_draft(client, journal, path, validate_plan=validate_expert, verify=verify_expert)
        print(f"임시보관 및 재조회 확인 완료: 문서 {journal['docId']} / {plan['head']['docTitle']}")
        print(f"작업 기록: {path}")


def status_command(args):
    results = []
    for path in sorted(DRAFTS.glob("*.json")) if DRAFTS.exists() else []:
        journal = read_private(path)
        results.append({"journal": str(path), "status": journal["status"], "docId": journal.get("docId"),
                        "title": journal["plan"]["head"]["docTitle"], "error": journal.get("saveResponseError")})
    print(json.dumps(results, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("tax", help="원천세 계산 (네트워크 없음)")
    command.add_argument("--type", choices=tuple(INCOME), required=True)
    command.add_argument("--gross", type=int, required=True)
    command.set_defaults(run=tax_command)
    command = commands.add_parser("scan", help="흩어진 증빙 파일을 사람별·서류별로 묶기 (네트워크 없음)")
    command.add_argument("folder", nargs="+", help="폴더(하위 폴더 포함) 또는 파일. 여러 개 가능")
    command.add_argument("--name", action="append", help="지급 대상 이름을 알면 반복해서 지정 (이름 판단이 정확해짐)")
    command.add_argument("--alias", action="append", help="영문 파일명 연결. 예: \"Minjung Kal=갈민정\"")
    command.add_argument("--out", help="묶음 결과 저장 파일 (init에 씀)")
    command.set_defaults(run=scan_command)
    command = commands.add_parser("init", help="scan 결과로 spec 뼈대 만들기")
    command.add_argument("--scan", required=True)
    command.add_argument("--out", required=True)
    command.add_argument("--program")
    command.add_argument("--purpose")
    command.add_argument("--project")
    command.add_argument("--accounting-date")
    command.add_argument("--funding", choices=("operating", "project"))
    command.set_defaults(run=init_command)
    command = commands.add_parser("examples", help="과거 전문가 활용비 문서의 실제 제목·적요 (적요 초안 근거)")
    command.add_argument("keyword", help="사업명·용도 등 제목에 들어갈 말. 예: 핀테크, 심사비")
    command.add_argument("--limit", type=int, default=8)
    command.set_defaults(run=examples_command)
    command = commands.add_parser("projects", help="과거 지출결의에서 쓴 프로젝트 후보 (많이 쓴 순·최근 순)")
    command.add_argument("--keyword", help="이름·코드 일부. 쓰면 권한 있는 전체 프로젝트에서도 찾는다")
    command.add_argument("--limit", type=int, default=10)
    command.set_defaults(run=projects_command)
    command = commands.add_parser("prepare", help="spec(JSON)으로 1210·1212 검토안 만들기 (쓰기 없음)")
    command.add_argument("--spec", required=True)
    command.add_argument("--out", required=True)
    command.set_defaults(run=prepare_command)
    command = commands.add_parser("check", help="현재 그룹웨어 데이터로 저장 전 검증 (쓰기 없음)")
    command.add_argument("plan")
    command.add_argument("--transfer", action="store_true", help="원천세 이체(1212) 검토안을 검증")
    command.set_defaults(run=check_command)
    command = commands.add_parser("draft", help="1210 임시보관 후 ERP·본문·첨부 재조회 (--transfer면 1212 원천세 이체)")
    command.add_argument("plan")
    command.add_argument("--transfer", action="store_true", help="같은 검토안의 원천세 이체(1212)를 임시보관")
    command.set_defaults(run=draft_command)
    command = commands.add_parser("status", help="전문가 활용비 작업 기록")
    command.set_defaults(run=status_command)
    args = parser.parse_args()
    try:
        args.run(args)
    except (ExpenseError, OSError, ValueError, KeyError) as error:
        message = str(error) if isinstance(error, ExpenseError) else f"입력 또는 로컬 파일을 확인하세요 ({type(error).__name__}: {error})."
        print(f"오류: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
