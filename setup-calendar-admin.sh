#!/usr/bin/env bash
# 캘린더 자동 등록 — 관리자 1회 설정.
# 조직 값은 인자로 받는다. 이 파일에는 어떤 조직·개인 값도 넣지 않는다.
# 서비스 계정 키는 이 저장소가 아니라 ~/.config/mysc-expense/state/ 에 권한 600으로 만든다.
set -euo pipefail

SCOPE="https://www.googleapis.com/auth/calendar.events"
SA_NAME="${SA_NAME:-leave-calendar}"
STATE_DIR="$HOME/.config/mysc-expense/state"
KEY_PATH="$STATE_DIR/calendar-sa.json"

die() { echo "  ✗ $*" >&2; exit 1; }
step() { echo; echo "── $* ──"; }

[ $# -ge 1 ] || die "사용법: $0 <GCP_PROJECT_ID>
  예: $0 mysc-leave-calendar
  프로젝트가 없으면 먼저 만드세요: gcloud projects create <ID>"
PROJECT="$1"

step "0. 준비 확인"
command -v gcloud >/dev/null || die "gcloud가 없습니다."
gcloud auth print-access-token >/dev/null 2>&1 \
  || die "gcloud 로그인이 만료됐습니다. 먼저 실행하세요: gcloud auth login"
ACCOUNT=$(gcloud config get-value account 2>/dev/null)
echo "  계정: $ACCOUNT"
echo "  프로젝트: $PROJECT"
gcloud projects describe "$PROJECT" >/dev/null 2>&1 \
  || die "프로젝트를 찾을 수 없거나 권한이 없습니다: $PROJECT"

step "1. Calendar API 사용 설정"
gcloud services enable calendar-json.googleapis.com --project "$PROJECT"
echo "  ✓ 완료"

step "2. 서비스 계정"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
if gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1; then
  echo "  이미 있음: $SA_EMAIL"
else
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" \
    --display-name "Leave calendar writer" \
    --description "Creates leave events on the shared attendance calendar"
  echo "  ✓ 생성: $SA_EMAIL"
fi

step "3. 키 발급"
mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
if [ -f "$KEY_PATH" ]; then
  echo "  이미 있음: $KEY_PATH"
  echo "  새로 발급하려면 기존 키를 먼저 폐기하고 이 파일을 지우세요."
else
  ( umask 077; gcloud iam service-accounts keys create "$KEY_PATH" \
      --iam-account "$SA_EMAIL" --project "$PROJECT" )
  chmod 600 "$KEY_PATH"
  echo "  ✓ 저장: $KEY_PATH (권한 600)"
fi
echo "  ⚠ 이 파일을 저장소·메신저·메일로 옮기지 마세요. 서버 시크릿으로만 주입합니다."

step "4. 다음은 화면에서 한 번 (자동화 불가)"
CLIENT_ID=$(gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" --format='value(uniqueId)')
cat <<EOF

  Google 관리 콘솔 → 보안 → 액세스 및 데이터 제어 → API 제어
  → 도메인 전체 위임 → 새로 추가

  클라이언트 ID   $CLIENT_ID
  OAuth 범위      $SCOPE

  범위는 이 하나만 넣으세요. Gmail·Drive·Sheets를 함께 넣지 마세요.

EOF

step "5. 대상 캘린더 권한"
cat <<EOF
  근태관리 캘린더 → 설정 및 공유 → 특정 사용자와 공유
  → 아래 주소를 추가하고 "변경 권한" 부여

  $SA_EMAIL

EOF

step "6. 서버 실행 (먼저 로컬에서 확인)"
cat <<EOF
  uvx workspace-mcp --tools calendar --transport streamable-http

  서비스 계정 사용을 위한 환경변수 이름은 프로젝트 배포 문서를 따르세요:
  https://workspacemcp.com/docs/deployment

  로컬에서 일정 생성·재조회가 확인되면 그때 사내 호스팅으로 올리고,
  구성원에게는 그 주소만 공지하세요. 키 파일은 공지에 넣지 않습니다.

EOF
echo "완료. 1~3은 다시 실행해도 안전합니다."
