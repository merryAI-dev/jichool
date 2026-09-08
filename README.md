# MYSC 지출결의 CLI

정상 로그인한 본인 계정으로 카드·원본 영수증을 조회하고, 현대카드 PDF 명세서와 대조한 뒤 전자결재에 임시보관합니다. CLI와 Python 표준 라이브러리를 사용하며, PDF 텍스트 추출에 설치된 Poppler의 `pdftotext`를 사용합니다. 그룹웨어 API를 직접 호출합니다. 외부 AI API·웹 서버·브라우저 자동화는 사용하지 않습니다.

## Claude Code 스킬로 사용하기

아래 한 줄이면 설치가 끝납니다. 설치 후 Claude를 새로 시작하고 "지출결의 해줘"라고 말하면, 나머지 준비(도구 설치·그룹웨어 연결·카드 프로필)는 스킬이 한 단계씩 안내합니다. 아래 명령을 직접 외울 필요는 없습니다.

```sh
git clone https://github.com/merryAI-dev/jichool.git ~/.claude/skills/jichool
```

업데이트는 `cd ~/.claude/skills/jichool && git pull` 입니다. Claude가 따르는 진행 절차는 같은 폴더의 `SKILL.md`에 있으며, 스킬은 아래 명령과 JSON 응답을 호출할 뿐 새로운 API 연동을 추가하지 않습니다. 이 아래부터는 명령을 직접 실행하려는 사용자를 위한 설명입니다.

## 처음 한 번: 설치와 정상 로그인

이 폴더의 `expense.py`, `statement.py`를 함께 보관합니다. Python 3.10 이상, macOS 또는 Linux가 필요합니다. Windows에서는 파일 잠금을 위해 WSL 환경이 필요합니다.

```sh
python3 --version
pdftotext -v
# pdftotext가 없으면 macOS에서 실행
brew install poppler
```

1. 사용자가 브라우저에서 `https://gw.mysc.co.kr`에 평소 방식으로 로그인합니다. SSO·추가 인증도 직접 완료합니다.
2. 개발자 도구의 Network를 열고 정상 로그인 과정의 응답을 확인합니다. `auth_a_token`, `hash_key`, `ucUserInfo`, `erpUserInfo`가 들어 있는 로그인 성공 응답을 찾습니다. 필요하면 Network를 연 상태에서 정상 로그인 과정을 다시 진행합니다.
3. 해당 요청의 **Copy response**로 응답을 클립보드에 복사합니다. 인증정보를 채팅, 코드, 명령행 인수에 붙여 넣지 않습니다.
4. 아래 명령을 실행합니다. 세션 객체 자체와 `resultData.sessionInfo`로 감싼 로그인 응답을 모두 받습니다.

```sh
python3 expense.py auth --clipboard
python3 expense.py auth --status
```

CLI는 정상 세션의 유효성을 확인한 뒤 `~/.config/mysc-expense/session.json`에 권한 600으로 저장합니다. 복사한 내용이 바뀌지 않았으면 클립보드도 비웁니다. Linux는 숨김 입력인 `python3 expense.py auth` 또는 신뢰할 수 있는 로컬 입력의 `auth --stdin`을 사용합니다.

`auth --status`는 인증 성공 여부와 사원·부서만 출력합니다. 만료·로그아웃·권한 오류가 나오면 정상 로그인 후 다시 가져옵니다. 자동 로그인이나 별도의 토큰 발급 서버는 구현하지 않았습니다. 원본 키를 코드에 넣지 말고, 노출한 세션은 로그아웃 등으로 폐기 후 다시 로그인합니다. `.env`·세션·개인 프로필을 커밋하지 않습니다.

## 개인 프로필

```sh
python3 expense.py profile \
  --card '그룹웨어에 표시되는 정확한 카드 이름' \
  --project '본인 기본 프로젝트 코드' \
  --out .expense-state/me.profile.json
```

프로필은 회사·사원에 귀속됩니다. 사원·부서는 로그인 정보로 조회하고, 회계처리일은 사용월 말일, 지급요청일은 작성일의 한국 날짜로 확정합니다. 다시 읽거나 재개한다고 날짜를 갱신하지 않습니다. 기본 프로젝트는 승인 이력으로 채우지 못한 경우에만 제안하며, 과거 프로젝트 선택이 엇갈리면 다시 질문합니다.

처음 만든 프로필에는 영구 적요 규칙이 없습니다. 매달 다른 적요·용도·프로젝트를 과거 한 번의 선택으로 고정하지 않습니다.

다른 사용자도 본인 계정으로 로그인하고 자기 카드·프로젝트의 프로필을 만들면 같은 명령을 사용합니다. 승인 이력은 본인·같은 회사·선택 카드로 한정합니다. 같은 카드를 썼더라도 다른 사원의 분류를 개인 규칙처럼 적용하지 않습니다.

## 매달: PDF와 사용월로 초안 만들기

```sh
python3 expense.py prepare \
  --month 2026-09 \
  --pdf '/실제/경로/현대카드.pdf' \
  --profile .expense-state/me.profile.json \
  --out .expense-state/september.plan.json

python3 expense.py inspect .expense-state/september.plan.json
```

`prepare`는 카드·영수증·최근 183일 승인 이력·현재 프로젝트/용도 목록을 조회합니다. 명세서 열 제목을 찾아 날짜·가맹점·원화 청구액·통화를 추출하고, PDF의 총 건수·총액과 독립적으로 대조합니다. 검토 JSON과 `.review.html`을 권한 600으로 생성합니다. 이미 처리한 거래는 본인 저장 기록과 실제 문서 상태로 확인합니다.

명세서에 승인번호가 없으므로 날짜·가맹점·금액이 한 건으로 연결되는 경우만 자동 대조합니다. 외화는 날짜·가맹점으로 유일하게 연결되고 부가세가 없는 경우 원화 청구액 수정안을 만들며, 원본 승인금액과 영수증 식별자는 보존합니다. 같은 날짜의 비슷한 거래나 다른 가맹점 표기는 질문으로 남깁니다. 명세서 밖 취소 거래는 자동 제외하지 않습니다.

## 모호하면 적극적으로 질문

외부 모델을 호출하지 않으므로 CLI에 `temperature` 설정은 없습니다. 확인 기준을 코드가 적용합니다.

- 승인 이력이 없거나 참고할 승인 문서가 한 건뿐이면 확인합니다.
- 과거 적요·용도·프로젝트·거래처 코드가 다르면 여러 선택지와 승인 문서 수를 보여줍니다. 단순 다수결로 선택하지 않습니다.
- 과거 문서가 두 건 이상이고 값이 모두 일치하면 제안할 수 있습니다. 업무 목적이 매번 같은지 판단할 근거가 부족하면 스킬에서도 추가 질문합니다.
- 과거 적요에 야근·출장·회의·접대·회식이 들어가면 `purpose_review`를 남겨 이번 목적을 검토하도록 합니다. 예를 들어 카카오모빌리티의 과거 적요가 ‘야근 택시비’여도 가맹점과 늦은 결제 시각만으로 이번 야근 귀가를 확정하지 않습니다.
- 현재 유효성을 확인하지 않은 거래처 코드는 자동으로 입력하지 않습니다. 필수 코드와 계좌 정보는 별도 확인이 필요합니다.
- PDF 거래 연결과 사용자 확인이 끝나지 않으면 `draft`가 저장을 막습니다.

`inspect`의 JSON은 `count`, `total`, `statementCount`, `statementTotal`, `alreadySaved`, `questions`를 반환합니다. 질문에는 종류(`kind`), 행 번호(`row`), 대상 필드(`field`), 설명(`message`), 과거 선택지(`options`), 연결 후보(`candidateRows`)가 들어갑니다. 행 번호는 1부터 시작합니다. `count/total`은 현재 검토안의 포함 행이며, `statementCount/statementTotal`은 명세서 목표입니다. 저장 가능 여부는 `questions`가 비어 있는지와 `check` 결과로 판단합니다.

```sh
# 해당 거래의 승인 시각·금액과 같은 가맹점의 과거 승인 적요·용도·프로젝트
python3 expense.py context .expense-state/september.plan.json --row 1

# 가맹점 이름이 달라도 본인·같은 카드의 관련 승인 이력 검색
python3 expense.py context .expense-state/september.plan.json --row 1 --query 택시
```

`context`는 가장 최근 승인 이력 12건과 전체 검색 건수를 JSON으로 반환합니다. 외부 모델을 호출하거나 토큰·카드번호를 출력하지 않습니다. 래핑하는 Astra/Opus 등의 모델은 이 근거와 이번 대화의 사용 목적을 함께 검토하고, 의심되는 목적·용도·프로젝트·거래처는 사용자에게 물어봅니다. 과거 패턴을 이번 사실의 증거로 단정하거나 근거 없는 답변을 `edit`로 확정하지 않습니다. 충분한 이번 근거가 있으면 검토 결과를, 부족하면 사용자 답변을 `edit`로 반영합니다. 일회성 검토가 영구 규칙이 되지는 않습니다.

스킬은 관련 질문을 묶어 자연어로 보여주고, 확인받은 내용을 다음 명령으로 반영하면 됩니다. 저장이 안 된 오류를 성공처럼 표현하거나, 합계를 맞추려고 임의 제외하지 않습니다.

```sh
# 이번 건의 검토 결과 또는 사용자 답변을 반영. 같은 값을 지정해도 검토 완료로 기록됨.
python3 expense.py edit .expense-state/september.plan.json --row 1 \
  --set 'description=사용자가 확인한 실제 업무 목적' \
  --set purpose=확인한용도코드 --set project=확인한프로젝트코드 \
  --out .expense-state/september-edited.plan.json

# 모호한 명세서 거래와 카드 원본을 사용자가 확인한 경우
python3 expense.py match .expense-state/september-edited.plan.json \
  --charge p1r3 --row 3 --reason '명세서 가맹점과 원본 영수증 대조 후 사용자 확인' \
  --out .expense-state/september-matched.plan.json

# 이번 명세서에 없는 거래를 제외하기로 확인한 경우
python3 expense.py exclude .expense-state/september-matched.plan.json --row 9 \
  --reason '사용자가 확인한 이번 결의 제외 사유' \
  --out .expense-state/september-reviewed.plan.json

python3 expense.py inspect .expense-state/september-reviewed.plan.json
python3 expense.py review .expense-state/september-reviewed.plan.json \
  --out .expense-state/september-reviewed.review.html
```

각 예시의 행 번호·코드는 실제 검토안에 맞춰 지정합니다. `edit` 지원 항목은 `description`, `purpose`, `project`, `vendor`, `employee`, `department`, `bank`, `account`, `holder`, `vehicle`, `receipt-date`, `pay-date`, `amount`, `vat`입니다. 거래처 이름은 카드 원본을 유지하고 `vendor`는 사용자가 확인한 거래처 코드입니다. 사원·부서·계좌·차량 변경의 실제 저장은 현재 지원 범위를 벗어나면 거절합니다. 실명조회 결과는 입력·복제하지 않습니다.

금액을 직접 수정할 때에는 `--set amount=... --set vat=... --evidence PDF --page ... --reason ...`를 지정합니다. 원본 PDF의 SHA-256과 승인금액 대비 차이를 기록합니다. 제공한 PDF의 총액만 맞추는 것으로 검증을 끝내지 않고 각 행의 연결과 금액을 확인합니다.

## 영구 규칙은 명시적으로 등록

이번 답변을 다음 달에도 적용하기로 사용자가 결정한 경우에만 실행합니다. `edit`, `match`, `exclude`는 프로필을 수정하지 않습니다.

```sh
python3 expense.py rule .expense-state/me.profile.json \
  --merchant '확인한 그룹웨어 가맹점명' \
  --description '앞으로도 적용할 적요' --purpose 확인한용도코드 \
  --project 확인한프로젝트코드 \
  --statement-merchant '명세서에 표시되는 같은 업체의 이름' \
  --out .expense-state/me-updated.profile.json
```

영구 규칙은 본인·선택 카드·정규화된 가맹점에 한정되며 승인 이력보다 우선합니다. 다른 가맹점의 규칙과 충돌하지 않도록 한 가맹점에는 한 규칙을 둡니다. 매번 목적이나 프로젝트가 달라지는 업체는 영구 규칙을 등록하지 않는 편이 맞습니다. 같은 PDF의 저장 작업 기록에는 그 작업의 확인 내용을 보관하지만 다른 PDF로 자동 전파하지 않습니다.

## 검증 후 임시보관

```sh
python3 expense.py check .expense-state/september-reviewed.plan.json
python3 expense.py draft .expense-state/september-reviewed.plan.json
python3 expense.py status --verify
```

`prepare`로 만든 검토안의 PDF는 `draft`에서 자동 첨부합니다. 기존 `preview` 방식의 검토안에는 필요하면 `draft --pdf /경로/근거.pdf`를 사용합니다. 현재 금액 수정 허용 설정, 마감일, 원본 잔액, 프로젝트·용도, 다른 메뉴 반영 여부와 카드 권한을 다시 확인합니다. 본문은 그룹웨어 양식 1211과 원본 영수증 링크로 생성합니다.

저장 후 ERP 필드, 전자결재 임시보관 상태, 본문 영수증 연결, 첨부 PDF 다운로드 SHA-256을 검증합니다. `doc_sts=10` 임시보관만 요청하며 상신·승인·지급은 구현하지 않습니다.

## 중단·재실행

```sh
python3 expense.py status
python3 expense.py resume '/status에 표시된/작업기록.json'
```

작업 기록은 `~/.config/mysc-expense/drafts/`에 저장합니다. `status`의 기본 상태는 마지막 로컬 기록이며, `--verify`의 `serverChecked`와 `error`로 현재 서버 확인 결과를 구분합니다.

- 완료 문서는 실제 서버에서 재확인하고 새로 생성하지 않습니다.
- PDF 업로드 완료, 연결키 발급 완료, ERP 저장 완료 등 확인된 단계에서 이어갑니다.
- `pdf_upload_attempted`, `link_attempted`, `erp_attempted`처럼 응답을 확인하지 못한 쓰기는 자동 재전송하지 않습니다.
- 전자결재 저장이 오류를 반환했어도 실제 연결된 문서를 찾으면 재조회하여 완료 여부를 판단합니다.
- 사용자가 상신하거나 문서 상태를 바꾼 경우에는 옛 초안을 자동 복원하지 않습니다. 삭제 상태가 조회된다고 해서 상신 완료라고 추정하지도 않습니다.
- 중복 방지는 로컬 기록과 현재 서버 검증을 함께 사용합니다. 다른 컴퓨터로 옮길 때에는 해당 계정의 기록을 안전하게 함께 옮기고, 기록을 삭제해 재시도하지 않습니다.

## 검증과 현재 범위

```sh
python3 test_expense.py
python3 test_expense.py /비공개/실제.plan.json /비공개/신규양식조회.json /비공개/저장기록.json
```

비공개 원본 PDF로 건수·총액 추출, 해외 거래 청구액 수정안, 모호한 연결 질문, 적요 규칙과 일회성 확인 분리를 검증했습니다. 비공개 저장 문서 데이터로 단계별 중단·재개와 저장 후 오류 응답을 재현하여 중복 쓰기 방지를 검증했습니다. 이 재현은 메모리 클라이언트이며 서버에 새로 저장하지 않습니다. 원본 PDF·개인 프로필·실제 거래·저장 기록은 저장소에 포함하지 않습니다.

사용자별로 다른 승인 패턴을 분리하고, 야근 택시비 검토·관련 이력 조회·미확인 저장 차단도 검사했습니다. 실제 다른 직원 계정이나 Astra/Opus의 판단 정확도를 검증한 것은 아닙니다.

실제 서버에서 정상 인증, `prepare` 조회, 문서 상태 변화 감지, 임시보관·영수증 연결·PDF 업로드와 다운로드를 검증했습니다. 재사용 기능 개발 중에는 운영 문서를 추가하거나 수정하지 않았습니다.

지원 범위는 MYSC 그룹웨어의 양식 1211, 본인·현재 부서 카드 거래, 지정한 한 사용월의 현대카드 텍스트 인쇄 PDF입니다. 다른 카드사·스캔본·할부·달을 걸친 명세서는 자동 해석하지 않습니다. 그룹웨어 양식 또는 API가 바뀌면 검증에서 중단할 수 있습니다. 인증정보나 PDF 거래 내용은 소스에 고정되어 있지 않습니다.

## 고정된 연동 범위와 입력값

| 구분 | 처리 방식 |
| --- | --- |
| 사원·부서·회사·인증정보 | 정상 로그인 세션에서 읽음 |
| 카드·기본 프로젝트·영구 분류 규칙 | 사용자가 만드는 로컬 개인 프로필에서 읽음 |
| 사용월·금액·가맹점 | 실행 인수, 원본 카드 거래, PDF에서 읽음 |
| 용도·프로젝트·지급수단 코드 | 현재 그룹웨어 조회값 사용. 지급수단 미조회·중복 시 임의 코드로 대체하지 않고 중단 |
| 그룹웨어 주소·요청 경로 | MYSC의 현재 연동에 한정한 고정값 |
| 양식 1211·전자결재 반복 표·상태 코드 | 검증한 연동 규격에 한정. 다른 양식으로 범용 전환하는 설정은 제공하지 않음 |
| PDF 열 구조 | 현대카드 텍스트 인쇄 서식에 한정. 행 개수와 청구액은 고정하지 않음 |
| 제안 기준·기본 날짜 | 최근 183일의 본인 승인 이력, 최소 2개 문서의 일치, 사용월 말일·한국 날짜 기준 작성일 |

테스트의 기본 입력은 가상 사용자·가상 카드입니다. 선택적인 실제 파일 검증은 사용자가 로컬에서 전달한 비공개 파일을 읽으며, 해당 파일을 저장소에 복사하지 않습니다. 모든 회사·카드사·양식에 대응하는 범용 연동 도구는 아닙니다.
