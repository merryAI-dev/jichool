# MYSC 그룹웨어(Amaranth 10) API 지도

`amaranth.py`의 근거 문서다. 2026-10-20에 REA로 번들을 정적 분석하고, 디버그 Chrome에서 실제 화면 호출을 캡처해 확인했다.

## 구조

- 셸: `https://gw.mysc.co.kr/static/js/` (메뉴는 `/gw/gw999A04`로 서버에서 받음)
- 마이크로 모듈: `/modules/<code>/asset-manifest.json`
  - `eap` 전자결재(문서함·뷰어·작성), `personal` 지출결의(APB1020New·APB1040 등), `nonprofit` 비영리 예산·소득자
- 모든 API는 `POST` + JSON, 헤더 서명: `Authorization: Bearer <auth_a_token>`, `wehago-sign = base64(HMAC-SHA256(hash_key, token + transaction-id + timestamp + path))`, `timestamp`, `transaction-id` (`expense.signed_headers`)
- 응답: `{resultCode: 0, resultData: ...}`. 0이 아니면 실패다(HTTP 200이어도).

## 조회 — `amaranth.py`에 들어간 것 (실호출 확인)

| 작업 | 경로 | 요청 | 비고 |
| --- | --- | --- | --- |
| `document_boxes` | `/eap/eap110A84` | `{}` | 상신·미결·기결·반려·수신참조·이전문서 등 menuId |
| `document_search` | `/eap/eap110A46` | `page, pageSize, sfrDt, stoDt(YYYY-MM-DD), sDocTp, sTitle, sContents, sUserNm, sDocNo, menuId, docId:null` | 참조문서 선택 화면의 검색. 열람 권한 있는 타인 문서 포함 |
| `document_view` | `/eap/eap111A23` | `{docId, docAuth:0, approkey:""}` | 문서함 미리보기. `doc_contents`(본문 HTML), `inter_contents`(구 시스템 ERP 표) |
| `expense_list` | `/personal/APB1020New/0ap00001` | `appDateFrom/To(YYYYMMDD), dateFg:"1"` | **본인 기안만**. 2025-08 이전 문서 없음 |
| `expense_document` | `…/0ap00002`, `…/0ap00003` | `{linkKey}` | 본인 문서의 ERP 원본 머리·행 |
| `purpose_codes` | `/personal/APCodePicker/ApAcashcdTypeCode` | `multiCode:[formId]` | 1210: 0290 사업소득·0300 기타소득지급수수료(`deductFg 5`=원천징수), 0350 기타지급수수료 |
| `payment_methods` | `…/0ap00041` | `{}` | paymethodFg 01→8, 02→8A, 03→1(세금계산서), 04→2(계산서), 05→9A |
| `expense_forms` | `/eap/eap096A45` | 회사·사원 정보 | 1210·1211·1212·1213·1214·1216 |

## 일부러 넣지 않은 것

| 경로 | 이유 |
| --- | --- |
| `/eap/eap111A05` | 문서 열람 시 화면이 부르는 **읽음 처리**(쓰기). 조회 도구는 부르지 않는다 |
| `/eap/eap110A03` | 작성기 초기화. 상신된 문서는 `2208 이미 상신된 문서` |
| `/eap/getDocBind` | 개발용, 운영 서버 404 |
| `…/0ap01001`, `…/0ap01002`, `/eap/eap110A06`, `/ecm/ecm001A01` | 저장 경로. `expense.py`·`taxinvoice.py`의 작업 기록·재조회 흐름으로만 쓴다 |

전체 후보는 전자결재 번들에서 경로 문자열 643개(`/eap` 403, `/eapmt` 69, `/ecm` 56 …)를 뽑았다. 새 조회를 카탈로그에 넣을 때는 실제 화면 호출을 캡처해 요청 모양과 `resultCode`를 확인한 뒤 `OPS`에 추가하고 `test_amaranth.py`에 가상 응답 검사를 붙인다.

## 뷰어 URL

- 진행 중 문서(작성·결재): `/#/popup?callComp=UBAP001&docID=&formId=&docAuth=`
- 종결 문서 보기: `/#/popup?callComp=UBAP002&docID=&formId=&docAuth=0`

## 전문가 활용비(외부인건비) — 확인된 것과 남은 것

확인된 처리 형식(MYSC-2025-4940/4939 사업소득, 4938 기타소득, 1509 기타소득 4인)은 [SKILL.md 7절](SKILL.md) 표 참조. 1건은 **1210 지출결의 + 1212 원천세 자금이체** 한 쌍이다.

ERP 행의 소득자 필드(개인 모듈 `1206` 청크의 `setIncomeInfo` 기준):

| 필드 | 뜻 |
| --- | --- |
| `trCd` / `trNm` | 소득자 코드·이름 (인사 모듈 소득자등록, `incmperNo`) |
| `trFg` | `4` 기타·사업소득자, `9` 사업(업종), `8` 사원 |
| `etcdataCd` | `G` 기타소득, `F` 사업소득, `BI` 비거주 |
| `etcdummy1` | 소득구분(기타)·업종구분(사업) 코드 |
| `etcrvrsYm` / `etcdivCd` | 귀속연월 / 원천세 신고 사업장 |
| `unitAm` | 지급총액 |
| `ndepAm` / `inadAm` / `etcrate` | 필요경비 / 소득금액 / 필요경비율(기타) |
| `intxAm` / `rstxAm` | 소득세 / 지방소득세 (10원 미만 절사, 과세최저한 이하 0) |
| `supAm` / `vatAm` | 실지급액(= unitAm − 세금·공제) / 공제 합계 |

**남은 것:** 위 매핑은 비영리 예산 화면의 것이다. 지출결의 1210(APB1020New) 저장 본문에서 같은 이름으로 들어가는지는 **실제 임시보관본 1건을 `expense_document`로 읽어** 확인해야 한다. 확인 전에는 외부인건비 저장을 열지 않는다.
