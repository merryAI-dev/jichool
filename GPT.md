# GPT(함수 호출)에서 쓰기

Claude는 `SKILL.md`를 따라 `amaranth.py`를 직접 실행한다. GPT나 다른 모델은 같은 조회 카탈로그를 함수 호출 도구로 쓴다.

1. 도구 정의: [`tools/openai-tools.json`](tools/openai-tools.json) (Claude API용은 `tools/anthropic-tools.json`). 카탈로그를 바꾸면 다시 생성한다.

   ```sh
   uv run amaranth.py tools --format openai > tools/openai-tools.json
   uv run amaranth.py tools --format anthropic > tools/anthropic-tools.json
   ```

2. 실행기: 모델이 부른 함수 이름·인자를 그대로 넘긴다. 결과는 주민번호·계좌번호가 가려진 JSON이다.

   ```python
   import json, subprocess
   def run_tool(name, arguments):
       out = subprocess.run(["uv", "run", "amaranth.py", "call", name, json.dumps(arguments)],
                            capture_output=True, text=True)
       return out.stdout if out.returncode == 0 else json.dumps({"error": out.stderr.strip()})
   ```

3. 연결: 같은 컴퓨터에서 `uv run amaranth.py browser` → 열린 창에서 사용자가 로그인 → `uv run amaranth.py login`.

## 시스템 지시에 넣을 것

- 이 도구들은 조회 전용이다. 상신·결재·삭제·저장을 했다고 말하지 않는다.
- 가려진 주민번호·계좌번호를 추측하거나 풀어 쓰지 않는다. 다른 사람 문서의 개인정보를 새 결의로 옮기지 않는다.
- 과거 문서는 형식(계정·적요 패턴·증빙구분)의 근거일 뿐, 이번 지출 목적의 증거가 아니다. 목적·용도·프로젝트는 사용자에게 확인한다.
- 저장이 필요한 지출결의는 Claude 스킬(`SKILL.md`)의 확인·검증 흐름으로 처리한다.
