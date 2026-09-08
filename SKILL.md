---
name: jichool
description: MYSC 그룹웨어 지출결의(양식 1211) 초안을 만들고 임시보관한다. 현대카드 PDF 명세서와 카드 원본 거래를 대조하고, 적요·용도·프로젝트를 사용자와 확인한 뒤 전자결재에 임시보관까지 진행한다. "지출결의", "카드값 정산", "경비 처리", "명세서 올려줄게" 같은 요청에 사용한다.
allowed-tools: Bash, Read, AskUserQuestion
---

# 지출결의 (jichool)

`expense.py`가 그룹웨어를 직접 조회·저장한다. 이 스킬은 CLI의 JSON 응답을 읽고, 사람에게 물어야 할 것을 묶어서 묻고, 확인된 답만 되돌려 넣는 진행자 역할만 한다.

**CLI가 판단하지 않는 단 하나: 이번 지출의 실제 업무 목적.** 그건 사용자만 안다. 과거 이력은 근거일 뿐 증거가 아니다.

## 실행 규칙

- 아래 `$SKILL_DIR`는 **이 SKILL.md가 있는 폴더**다. 기본 설치 경로는 `~/.claude/skills/jichool`. 세션에서 처음 명령을 실행하기 전에 실제 경로를 한 번 확인하고, 이후에는 절대 경로로 실행한다. `expense.py`는 같은 폴더의 `statement.py`를 불러오므로 두 파일을 떼어놓지 않는다.
- 명령은 항상 스킬 폴더의 파일로 실행한다: `python3 "$SKILL_DIR/expense.py" ...`
- 작업 파일은 `~/.config/mysc-expense/state/` 아래에 둔다. 프로젝트 폴더나 스킬 폴더에 만들지 않는다.
- **`--out`은 매번 새 파일 이름이어야 한다.** 기존 파일에 덮어쓰면 CLI가 거부한다. `september.plan.json` → `september-edited.plan.json` → `september-matched.plan.json` 처럼 단계마다 이름을 늘린다.
- 오류가 나면 그대로 사용자에게 보여준다. 저장이 안 됐는데 됐다고 말하지 않는다.

## 1. 최초 1회: 인증

세션이 있는지 먼저 본다.

```sh
python3 "$SKILL_DIR/expense.py" auth --status
```

실패하면 사용자에게 이렇게 안내한다 (토큰을 채팅·명령행에 붙여넣게 하지 않는다):

> 1. 브라우저에서 `https://gw.mysc.co.kr`에 평소 방식으로 로그인해주세요.
> 2. 개발자도구 Network에서 `auth_a_token`, `hash_key`, `ucUserInfo`, `erpUserInfo`가 들어 있는 로그인 성공 응답을 찾아 **Copy response** 하세요.
> 3. 복사한 상태로 알려주시면 제가 클립보드에서 바로 읽어올게요.

그 다음 `auth --clipboard`를 실행한다. macOS 기준이며, Linux는 `auth`(숨김 입력)를 사용자가 직접 실행한다.

인증 실패·만료·권한 오류는 재로그인 안내로 끝낸다. 자동 로그인을 만들지 않는다.

## 2. 최초 1회: 개인 프로필

카드 이름은 그룹웨어에 표시되는 정확한 이름이어야 한다. 모르면 물어본다.

```sh
python3 "$SKILL_DIR/expense.py" profile --card '카드 이름' --project '기본 프로젝트 코드' \
  --out ~/.config/mysc-expense/state/me.profile.json
```

프로필은 계정에 귀속된다. 다른 사람 프로필을 재사용하지 않는다.

## 3. 매달: 초안 만들기

사용월(`YYYY-MM`)과 현대카드 PDF 경로를 사용자에게 받는다. 둘 다 없으면 진행하지 않는다.

```sh
python3 "$SKILL_DIR/expense.py" prepare --month 2026-09 --pdf '/받은/경로.pdf' \
  --profile ~/.config/mysc-expense/state/me.profile.json \
  --out ~/.config/mysc-expense/state/2026-09.plan.json
```

출력의 `count`/`total`은 현재 검토안, `statementCount`/`statementTotal`은 명세서 목표다. **두 숫자가 다르면 그 차이를 사용자에게 먼저 보고한다.** 합계를 맞추려고 임의로 행을 제외하지 않는다.

## 4. 질문 해소 (이 스킬의 핵심)

```sh
python3 "$SKILL_DIR/expense.py" inspect <plan>
```

`questions`가 빌 때까지 반복한다. 종류별 처리:

| kind | 뜻 | 할 일 |
| --- | --- | --- |
| `field` | 필수 항목 누락·형식 오류 | `edit --set`으로 채운다. 코드값을 모르면 묻는다 |
| `history` | 과거 선택이 갈리거나 근거 부족 | `context`로 이력을 보고, **선택지와 문서 건수를 사용자에게 보여주고 고르게 한다.** 다수결로 정하지 않는다 |
| `purpose_review` | 과거 적요에 야근·출장·회의·접대·회식 | 이번 목적을 사용자에게 **반드시 확인한다.** 가맹점·결제 시각만으로 추정하지 않는다 |
| `unmatched_card` | 카드 거래가 명세서와 연결 안 됨 | 사용자가 확인하면 `match`, 이번 결의 대상이 아니면 `exclude` (사유 필수) |
| `unmatched_statement` | 명세서 거래가 카드 원본과 연결 안 됨 | `candidateRows`를 사용자에게 제시하고 확인받아 `match --charge <id> --row <n>` |
| `amount_mismatch` | 증빙일·금액 불일치 | 외화·부분취소 등 원인을 사용자와 확인. 금액 수정은 근거 PDF 필수 |
| `confirmation` | 검토안 전체 수준의 확인 | 메시지를 그대로 사용자에게 전달 |

행 근거 조회:

```sh
python3 "$SKILL_DIR/expense.py" context <plan> --row 1
python3 "$SKILL_DIR/expense.py" context <plan> --row 1 --query 택시   # 가맹점명이 달라도 검색
```

확인된 답 반영 (같은 값을 다시 지정해도 "검토 완료"로 기록된다):

```sh
python3 "$SKILL_DIR/expense.py" edit <plan> --row 1 \
  --set 'description=확인한 실제 업무 목적' --set purpose=<용도코드> --set project=<프로젝트코드> \
  --out <새-plan>
```

`edit` 지원 항목: `description` `purpose` `project` `vendor` `employee` `department` `bank` `account` `holder` `vehicle` `receipt-date` `pay-date` `amount` `vat`.

금액 수정은 반드시 근거를 함께 남긴다:

```sh
python3 "$SKILL_DIR/expense.py" edit <plan> --row 3 --set amount=... --set vat=... \
  --evidence '/근거.pdf' --page 2 --reason '확인한 사유' --out <새-plan>
```

연결·제외:

```sh
python3 "$SKILL_DIR/expense.py" match <plan> --charge p1r3 --row 3 --reason '사용자 확인 내용' --out <새-plan>
python3 "$SKILL_DIR/expense.py" exclude <plan> --row 9 --reason '사용자 확인 제외 사유' --out <새-plan>
```

질문을 낱개로 던지지 말고 관련된 것끼리 묶어 자연어로 한 번에 묻는다.

## 5. 검토 화면과 저장

```sh
python3 "$SKILL_DIR/expense.py" review <plan> --out <plan>.review.html
python3 "$SKILL_DIR/expense.py" check <plan>
python3 "$SKILL_DIR/expense.py" draft <plan>
python3 "$SKILL_DIR/expense.py" status --verify
```

`review` HTML은 사용자에게 파일로 전달해 눈으로 확인받는다. `check`가 통과하지 못하면 그 문제를 해결하기 전에는 `draft`를 실행하지 않는다.

`draft`는 **임시보관(`doc_sts=10`)만** 한다. 상신·승인·지급은 이 도구의 범위가 아니며, 사용자가 요청해도 만들지 않는다. 저장 후 `status --verify`로 서버 상태를 재확인해 보고한다.

## 6. 중단·재개

```sh
python3 "$SKILL_DIR/expense.py" status
python3 "$SKILL_DIR/expense.py" resume '<기록.json>'
```

- 중복 저장이 무서워도 기록을 지우고 다시 하지 않는다. 항상 `status --verify`로 서버를 먼저 확인한다.
- `pdf_upload_attempted`, `link_attempted`, `erp_attempted`는 응답 미확인 상태다. 자동 재전송하지 않고 사용자에게 보고한다.
- 사용자가 이미 상신했거나 상태를 바꿨으면 옛 초안을 복원하지 않는다.

## 7. 영구 규칙은 사용자가 명시적으로 결정할 때만

이번 달 답변은 이번 달에만 적용된다. "앞으로도 계속 이렇게 해줘"라고 사용자가 **명시적으로** 말한 경우에만:

```sh
python3 "$SKILL_DIR/expense.py" rule <profile> --merchant '가맹점명' \
  --description '앞으로 적용할 적요' --purpose <용도코드> --project <프로젝트코드> \
  --statement-merchant '명세서상 이름' --out <새-profile>
```

매달 목적이 달라지는 업체(택시·식당 등)는 규칙 등록을 **권하지 않는다.** 그렇게 조언한다.

## 하지 않는 것

- 토큰·카드번호를 출력하거나 채팅에 남기지 않는다.
- 확인 안 된 용도·프로젝트·거래처 코드를 추측해서 넣지 않는다.
- 합계를 맞추려고 거래를 제외하지 않는다.
- 과거 이력을 이번 목적의 증거로 단정하지 않는다.
- 저장 실패를 성공처럼 요약하지 않는다.
- 상신·승인·지급을 실행하지 않는다.

## 지원 범위

MYSC 그룹웨어 양식 1211 / 본인·현재 부서 카드 / 지정한 한 사용월의 현대카드 텍스트 인쇄 PDF. 다른 카드사, 스캔본, 할부, 달을 걸친 명세서는 자동 해석하지 않는다. 범위를 벗어나면 추정하지 말고 사용자에게 알린다.

자세한 동작 근거는 [README.md](README.md) 참조.
