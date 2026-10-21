# Re-cat — Re + Code Analysis Trace

레거시 코드(Java/Spring, Python)의 **호출 흐름**을 정적 분석으로 확정하고, LLM은 그 위에서 **설명만** 하게 한 뒤,
설명 속 호출 관계 주장을 분석 결과와 **코드로 대조 검증**하는 에이전트. 제공된 Base AI Agent
(`all_total_vue_student`) 위에 확장했다.

## 처리 흐름

```
gate → route → ┬ chat_answer
               └ locate → explore → explain → verify ┬ finalize
                                       ▲             ├ (실패) explain 재시도 (최대 2회)
                                       └─────────────┤
                                                     └ fallback (LLM 없는 구조 출력) → finalize
```

| 노드 | 하는 일 | 누가 결정하나 |
|---|---|---|
| gate | 로컬/클라우드 경로, 트레이싱 허용 여부 | **코드**(policy.yaml) |
| route | 코드 흐름 질문인지 판단 | LLM(fast) |
| locate | 질문에서 시작 함수 선택 (검색은 여기에만 사용) | 코드(키워드 검색) |
| explore | `get_callees`/`get_source`로 호출을 따라감. 열어볼 함수는 LLM이 고르고 호출 횟수·깊이는 코드가 제한 | LLM + 코드 상한 |
| explain | 설명 + 호출 관계 주장 목록(claims) | LLM(quality) |
| verify | claims를 `get_chain` 결과와 대조 | **코드** |

## Base 대비 변경

| 구분 | 파일 |
|---|---|
| 그대로 사용 | `agent/mcp_client.py`(서버 구성만 변경) `agent/retry.py` `agent/session_store.py` `sh/setup_*.sh` |
| 내용 교체 | `server.py` `agent/graph.py` `agent/deps.py` `agent/*_llm.py` `mcp_server.py` `web_chat.html` `web_demo_server.py` `agent/supervisor_v1.py` |
| 신규 | `analyzer/` `security/` `retrieval/` `agent/verifier.py` `policy.yaml` `tests/` `store/schema.sql` `docker-compose.yml` |
| 삭제 | 인시던트 도메인 데이터(`data/runbooks`, `monitoring.json`), decoy MCP 서버 |

`doc/diff_from_base.patch`에 Base 대비 전체 diff가 있다.

## 실행

```bash
bash sh/setup_venv.sh              # .venv 생성 + 패키지 설치 (최초 1회)
bash sh/run_agent_fake.sh          # 모델 없이 결정론 백엔드로 기동 (http://127.0.0.1:8100)
bash sh/run_frontend.sh            # 브라우저 화면 http://localhost:8600
.venv/bin/python -m pytest tests   # 테스트 91개
```

로컬 모델: `bash sh/setup_ollama.sh` → `bash sh/run_agent_ollma.sh`.
클라우드 에스컬레이션: `policy.yaml`의 `cloud.enabled: true` + `.env`의 `OPENAI_API_KEY` + `bash sh/run_agent_openai.sh`.

```bash
curl -s -X POST localhost:8100/chat -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dev-key-change-me' \
  -d '{"message":"주문 생성은 어떻게 동작해?","repo":"sample_py"}'
curl -s -H 'Authorization: Bearer dev-key-change-me' \
  'localhost:8100/flows/sample_py?symbol=OrderService.create'      # LLM 없이 호출 체인만
```

분석 대상은 `data/repos/<이름>/`에 두고 `policy.yaml`의 `repos.allowed`에 추가한다.

## 문서-코드 대조 (문서 근거)

저장소 안의 문서(md·txt·rst — 기획서, 명세, README)를 절 단위로 색인하고, "기획서의 3단계 결제 요청에 해당하는 코드가 `PaymentClient.request` 맞아?"처럼 물으면 해당 절을 찾아 코드와 맞춰 본다.

- 경로: `route`(doc) → `doc_locate`(MCP `search_docs`) → `doc_check`(`get_doc_section`, `check_doc_section` — 결정적) → `doc_explain`(LLM) → `doc_verify`(설명 속 호출 주장 검증, 실패하면 설명을 버림) → `finalize`
- 문서에 적힌 호출 관계(`` `A`가 `B`를 호출 ``, `A → B`)를 호출 그래프와 대조해 5가지로 판정한다: ✓ 일치(직접), ✓ 일치(간접, 경로 표시), ✗ 불일치, ✗ 코드에 없는 이름(낡은 문서 후보), ? 확인 안 됨(동적·모호 호출이 있어 단정 불가).
- 질문에 함수 이름이 있으면 그 함수와 절의 코드상 연결(직접 언급 / 호출로 이어짐 / 근거 없음)을 확인한다. "이 단계가 이 함수다"라는 대응 자체는 LLM의 추정으로 표시한다.
- 답변 구역: 문서 근거(검증 대상 아님) · 코드 대조 · 질문한 함수와의 연결 · 해석(추정).
- 샘플: `data/repos/sample_py/docs/order_spec.md`(5가지 판정이 모두 나오도록 낡은 내용을 일부 포함), `data/repos/sample_java/docs/README.md`.
- 한계: 문서 이름 추출은 규칙 기반이라 코드 이름을 백틱이나 화살표로 적은 경우만 잡는다("결제 모듈을 부른다" 같은 자연어는 못 잡음). 검색은 키워드이고 임베딩은 없다. 업무 규칙("10% 할인")은 대조할 수 없다. 실제 LLM으로는 실행하지 않았다.

## 배포 방식 — 회사별 온프레미스 설치형

코드를 외부로 보낼 수 없는 곳에서 쓰는 도구이므로, 회사마다 자기 서버에 Re-cat과 로컬 모델(sLLM)을 따로 설치한다. 회사 간 분리는 로그인이 아니라 설치 자체로 해결되고, 회사별 차이는 설정(`policy.yaml`, `.env`, 관리자 화면의 저장소 등록)으로 둔다.
중앙 서버를 여러 회사가 공유하는 방식은 다른 회사의 코드가 한곳에 모이므로 채택하지 않는다.

**역할 분리** — 사용자(`RECAT_API_KEY`)와 관리자(`RECAT_ADMIN_KEY`)는 서로 다른 키이고, 권한은 서버 API에서 강제한다(화면 숨김은 보안으로 보지 않는다). 사용자 키로는 관리자 API가 열리지 않는다(401), 관리자 키가 설정되지 않았는데 사용자 키만 있으면 403(닫힘).
**LangSmith 추적 정책** — 끄는 대신 추적 데이터가 저장되는 곳으로 판정한다(`security/gate.py` `tracing_decision`). 추적 서버(`LANGSMITH_ENDPOINT`)가 `policy.yaml`의 `tracing.internal_endpoints`(자체 호스팅·BYOC)에 있으면 민감 저장소도 추적하고, 클라우드면 민감 저장소는 끄고 일반 저장소는 입출력 숨김 상태(메타데이터만)로만 보낸다. 주소는 정확히 일치할 때만 사내로 보고, 정책 스위치가 꺼져 있으면 항상 끈다. 관리자 화면에 추적 서버와 저장소 종류별 판정이 표시된다.

**LangSmith 품질 지표** (`observability/langsmith_feedback.py`, `eval/langsmith_upload.py`)
- 추적이 허용된 실행마다 검증 결과를 숫자 피드백으로 붙인다: `recat.verified`, `recat.hallucinated`, `recat.wrong`, `recat.unverifiable`, `recat.caught_errors`, `recat.explain_attempts`, `recat.fallback`, `recat.verified_ratio`. 정해진 숫자 키만 보내므로 클라우드여도 본문은 나가지 않는다. 실패해도 응답에는 영향이 없다.
- `python -m eval.run_eval --backend ollama --langsmith`: 결과를 데이터셋 `re-cat-eval`과 실험 `re-cat-{A|B|C}-{backend}`로 올리고, 평가자 `error_rate`·`verified_ratio`를 붙인다. 파이프라인을 다시 돌리지 않고 계산된 결과를 올린다. 민감 저장소 질문은 빼고, fake 결과는 거부한다(`--include-fake`로만 허용).
- 관리자 감사 로그의 "추적" 열: `LANGSMITH_RUN_URL`(예: `https://smith.langchain.com/o/ORG_ID/projects/p/PROJECT_ID/r/{run_id}`)을 설정하면 링크, 아니면 run id 앞자리.
- 한계: 키가 없는 환경이라 가짜 클라이언트로만 테스트했다. 실제 LangSmith 화면과 전송은 키가 있는 환경에서 확인해야 한다.

한계: 키는 사람이 아니라 역할을 구분한다. 사용자 개인 식별, 사용자별 저장소 권한, 감사 로그의 "누가"는 없다. 확장 방향은 사내 SSO(OIDC)와 역할 클레임이다.

## 구현 상태 (정직하게)

| 항목 | 상태 |
|---|---|
| Python(ast)·Java(tree-sitter) 분석기, 미해석 호출 표시 | 구현, 샘플 저장소로 테스트 |
| MCP 도구 4종·Resource·Prompt, 경로 이탈 방지, 마스킹, 길이 제한 | 구현, 테스트 |
| 결정적 검증 + 재설명 + LLM 없는 대체 출력 | 구현, 테스트 |
| 보안 게이트(`decide`), 민감 저장소의 트레이싱 차단, 감사 로그(메타데이터만), Bearer 인증, 저장소 허용 목록 | 구현, 테스트 |
| 탐색 상한, `agent`/`fixed` 탐색 모드 전환(실험 7용) | 구현, 테스트 |
| 관리자 저장소 관리: 목록·상태, 등록(`data/repos/` 바로 아래 폴더만, 기본 민감), 재분석, 감사 기록. policy.yaml은 완화 불가 | 구현, 테스트 13개(권한·경로·정책 우회·재분석) |
| 문서-코드 대조: 절 색인·키워드 검색, 문서 속 호출 관계 5가지 판정, 질문한 함수와의 연결, 에이전트 doc 경로 | 구현, 테스트 14개(fake 백엔드) |
| LangSmith 연동: 추적 서버 위치 판정, 검증 결과 피드백, eval 실험 업로드, 관리자 링크 | 구현, 테스트 8개(가짜 클라이언트). 실제 전송 미확인 |
| Vue 화면: 저장소 선택, 단계 표시, 검증 배지, 미해석 호출 표시 | 구현, 헤드리스 브라우저로 확인 |
| Ollama·OpenAI 어댑터 | 모의 HTTP 서버로 요청·응답 처리만 확인. **실제 모델/API로는 미실행** |
| PostgreSQL + pgvector | `store/schema.sql`, `docker-compose.yml`만 작성. **서버에 연결되지 않음**(분석 결과는 메모리 캐시) |
| 임베딩 기반 진입점 검색 | 미구현(키워드 검색만) |
| 실험 1~7 측정, 카나리 유출 실험 | 미실행 |
| fake 백엔드의 "첫 설명에 틀린 주장 섞기" | 검증 노드 동작을 보이기 위한 의도된 결함(`FAKE_FLAWED_FIRST=0`으로 끔) |

## 장애 대응 (트러블슈팅)

모의 Ollama로 장애를 일부러 일으켜 확인하고 고친 8건 — 모델 호출이 서버를 멈춤, MCP 서브프로세스 멈춤·죽음 자동 복구,
모델 503 재시도와 정적 분석 fallback, 스트리밍 오류 보고, 깨진 JSON·본문 속 가짜 함수 검증, 동시 업로드 덮어쓰기, 교체 실패 롤백.
수정 전·후 실제 출력과 재현 스크립트는 [TROUBLESHOOTING.md](TROUBLESHOOTING.md), 회귀 테스트는 `tests/test_resilience.py`.

## 알려진 한계

- Python: `getattr`, 콜백, 데코레이터로 바뀌는 호출, 같은 이름의 여러 클래스는 해석하지 못하거나 `resolved=False`로 남긴다.
- Java: 인터페이스 구현이 2개 이상이면 `ambiguous`, 프록시·AOP·이벤트·`@Qualifier` 선택·람다 안 타입 추론은 미지원, 오버로드는 첫 선언만 기록.
- 검증은 "호출 관계가 사실인가"만 본다. 의도·이유 설명의 정확성은 검증하지 못한다(화면에서 '해석(검증 안 됨)'으로 분리).
- 마스킹은 정규식이라 base64·분할 문자열은 놓친다(`tests/test_security.py`에 명시).
- 저장소 관리: 코드는 운영자가 서버의 `data/repos/`에 배치한다고 가정한다(원격 Git clone·삭제는 없음). 등록 정보는 `data/registry.json`에 저장되며 policy.yaml 위에 더하기만 한다.
- 프론트가 CDN(unpkg, Google Fonts)에서 Vue와 글꼴을 불러온다. 폐쇄망 설치 시에는 Vue 파일을 로컬에 두도록 바꿔야 한다(미처리).
- 인증 키가 비어 있으면 개발 모드(인증 없음)로 동작한다. 127.0.0.1 바인딩을 유지할 것.


## 사용 가이드 (UI)

화면 첫 진입 시 4단계 가이드가 표시되고(저장소 선택 → 질문 입력 → 진행 단계 확인 → 검증 결과 읽기), 질문을 보내면 접힌다. 오른쪽 위 "사용 가이드" 버튼으로 다시 열 수 있다. 예시 질문 버튼을 누르면 입력창에 채워진다.

## LangGraph 선언 분석 (Base 코드 분석용)

`add_node` / `add_edge` / `add_conditional_edges` / `set_entry_point`로 선언된 실행 순서를 노드 함수 사이의 간선(`kind="graph"`)으로 추가한다. Base 에이전트(`data/repos/base_agent`)에서 선언된 간선 12개가 모두 잡힌다. 조건 분기의 매핑이 dict 리터럴이 아니면 "확인 안 됨(dynamic)"으로 남긴다. 한계: `add_node`에 람다·클로저를 넘기거나 노드 이름을 변수로 만들면 못 잡는다.

## 비교 실험 (`eval/`)

같은 질문 12개로 호출 관계 주장의 오류율을 세 방식으로 비교한다. A: 코드만 준 LLM(검증 없음), B: 정적 분석 구조를 줬지만 검증 없음(Re-cat 첫 시도), C: Re-cat 전체(검증·재설명·폴백).

    python -m eval.run_eval --backend fake      # 파이프라인 동작 확인용 (A 생략, 수치 인용 금지)
    LLM_BACKEND=ollama python -m eval.run_eval  # 실제 비교 (A 포함)

결과는 `eval/results/*.json`에 저장되고, 오류율·진입점 적중률·폴백 비율·평균 설명 시도를 출력한다. 폴백한 질문은 오류율에서 빼는 대신 폴백 비율로 따로 보고한다. 질문 목록은 `eval/questions.json`. **실제 LLM으로는 아직 실행하지 않았다.** fake 실행에서 확인된 것은 파이프라인 동작과 진입점 적중률(12개 중 8개)뿐이다.

## 화면 (사용자 / 관리자)

실행: 서버(`sh/run_agent_fake.sh`, 8100) → 프록시(`sh/run_frontend.sh`, 8600). 브라우저에서 `/`(사용자), `/admin`(관리자).

**사용자 화면** — 왼쪽 그래프, 오른쪽 대화.
- 구조 개요: 클래스·파일 단위 의존 그래프 (`GET /graph/{repo}`, MCP `get_overview`). 노란 배지는 분석이 못 따라간 호출 수.
- 호출 흐름: 질문하면 시작 함수 기준 호출 체인 그래프가 그려진다. 점선 노드는 "확인 안 됨".
- 영향 범위: 함수 옆 버튼 → 이 함수를 직접·간접으로 호출하는 곳 (`GET /callers/{repo}`, MCP `get_callers`).
- 답변은 세 구역(구조·검증됨 / 확인 안 됨 / 해석·미검증)으로 나뉘어 표시된다.
- 그래프는 외부 라이브러리 없이 `web_graph.js`가 SVG로 그린다.

**구조 그래프 드릴다운** — 구조 개요에서 클래스·모듈을 더블클릭하면 그 안의 함수들과 호출이 펼쳐지고(`GET /group/{repo}`, MCP `get_group`), 함수를 더블클릭하면 그 함수의 호출 흐름(`GET /flows/{repo}`)으로 들어간다. 클릭하면 코드가 아래에 열리고, 위쪽 경로(전체 구조 › 클래스 › 함수)로 되돌아간다. 모두 LLM 없이 정적 분석 결과다. 다른 묶음의 함수는 점선 상자로 표시한다.

**코드 보기** — 답변 아래 "코드 보기"에서 함수 버튼을 누르면 그 함수의 코드(파일·줄 번호)가 답변 바로 아래 펼쳐진다. 그래프에서 함수를 눌러도 왼쪽 아래에 코드가 열린다(`GET /source/{repo}`, MCP `get_source` 경유: 길이 제한·마스킹, 감사 로그에는 함수 이름만).

**zip 업로드** — 관리자 화면 → 저장소 관리 → "zip으로 올리기". 프로젝트 폴더를 통째로 압축해 올리면 `data/repos/<이름>/`에 풀리고 등록(기본 민감)된다. 분석은 저장소 전체를 대상으로 하므로 파일을 넘나드는 호출도 하나의 그래프로 이어진다. 경로 이탈(`../`)·심볼릭 링크·압축 폭탄은 거부하고(zip 50MB, 해제 300MB, 파일 2만 개, 파일당 20MB), `.git`·`node_modules`·빌드 산출물은 건너뛴다. 같은 이름 교체는 업로드로 등록한 저장소만 가능하다(`policy.yaml` 저장소는 불가). 코드는 실행하지 않는다(`POST /admin/repos/upload`, `security/repo_upload.py`). 폴더를 바로 고를 수도 있다("폴더 선택", `POST /admin/repos/upload-folder`, zip과 같은 검사).

**문서 등록** — 저장소 안의 md·txt·rst는 자동으로 색인된다. 저장소에 없는 문서(기획서, API 명세)는 관리자 화면 저장소 표의 "문서"에서 따로 올린다(`/admin/repos/{repo}/docs`, md·txt·rst UTF-8, 파일 2MB·합계 20MB). `data/docs/<repo>/`에 저장되어 코드 폴더는 바뀌지 않고, 답변에는 `uploaded/<파일>`로 출처가 표시된다. 업로드한 문서만 삭제할 수 있다.

**관리자 화면** — `RECAT_ADMIN_KEY`로 로그인(`GET /admin/summary`). 탭은 현황과 저장소 관리(`GET/POST /admin/repos`, `POST /admin/repos/{repo}/reanalyze`)이다. 저장소 관리는 분석 상태 확인, 등록 후보 선택, 재분석을 한다. 임의 경로 입력과 삭제는 없다. 요청·재설명·폴백·검증이 잡은 오류 통계, 처리 경로, 저장소별 요청, 보안 정책, LangSmith 추적 상태, 비교 실험 결과, 최근 감사 로그(메타데이터만). 사용자 키로는 열리지 않고, 사용자 키만 있고 관리자 키가 없으면 403(닫힘)이다. 프록시는 관리자 키를 붙이지 않고 브라우저가 입력한 키를 그대로 전달한다.
한계: LangSmith 평가·대시보드 연동은 없다(추적 제어만). 감사 통계의 "오류 주장"은 이 버전부터 기록된 요청만 센다.
