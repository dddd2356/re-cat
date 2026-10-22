<div align="center">

# Re-cat

**정적 분석으로 검증된 레거시 코드 호출 흐름 설명 도구**

Python/Java 코드베이스의 호출 관계를 정적 분석으로 확정하고, 로컬 LLM(Ollama)이 그 위에서 자연어 설명을 생성합니다.
LLM이 만든 설명은 그 자체로 신뢰하지 않고 **항상 정적 분석 결과와 대조해 검증**한 뒤 응답합니다.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.159-009688)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2-1C3C3C)](https://langchain-ai.github.io/langgraph/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-336791)](https://www.postgresql.org/)
[![Ollama](https://img.shields.io/badge/LLM-Ollama%20(local)-000000)](https://ollama.com/)
[![Status](https://img.shields.io/badge/status-internal%20PoC-yellow)]()

[기능](#기능) · [아키텍처](#아키텍처) · [빠른 시작](#빠른-시작) · [설정](#설정) · [테스트](#테스트) · [운영 문서](#문서)

</div>

---

## 개요

Re-cat은 담당자가 바뀐 레거시 코드를 인수인계할 때, "이 함수가 실제로 뭘 호출하는가"를 **추측이 아니라 사실**로 확인시켜주는 온프레미스 도구입니다.

- 호출 그래프는 Python AST / Java tree-sitter **정적 분석**으로만 확정합니다. LLM은 이 결과를 설명할 뿐 호출 관계를 판단하지 않습니다.
- 설명에 포함된 호출 주장(claim)은 매번 정적 분석 그래프와 재대조하며, 검증에 실패하면 재설명 → 그래도 실패하면 **LLM 설명 없이 정적 분석 결과만** 반환합니다.
- 코드와 함께 올린 설계 문서(README, 명세 등)도 같은 방식으로 코드와 대조해, 문서가 낡았는지(코드에 없는 이름을 가리키는지) 판별합니다.
- 코드와 질문 내용은 기본적으로 외부로 나가지 않습니다 — 로컬 Ollama만 사용하고, 클라우드 경로는 정책으로 기본 차단되어 있습니다.

> **범위**: 현재 버전은 단일 조직·단일 서버·단일 프로세스를 전제로 한 PoC입니다. 사용자/부서별 접근 제어, 다중 인스턴스 운영은 아직 지원하지 않습니다. 운영 전환 시 확인할 항목은 [운영 가이드](docs/OPERATIONS.md#현재-mvp-범위)를 참고하세요.

## 기능

| 영역 | 내용 |
|---|---|
| **정적 분석** | Python AST, Java tree-sitter 기반 호출 그래프. 모듈 import·상속·타입 힌트·생성자 체인까지 해석하고, 해석 못 한 호출은 후보(candidates)로 남겨 추측을 금지 |
| **검증된 설명** | LLM이 낸 호출 주장(claim)을 정적 분석 결과와 1:1 대조. hallucinated(없는 이름)/wrong(없는 관계)/unverifiable(판정 불가)을 구분해 표시 |
| **문서 대조** | 업로드한 md/txt/rst 문서의 "A가 B를 호출한다" 서술을 코드와 대조해 일치/불일치/코드에 없는 이름으로 판정. 여러 절을 묶는 "문서 전체 요약" 질문도 지원 |
| **장애 복원력** | 모델 호출 재시도·회로, MCP 서브프로세스 자동 재시작, 스트리밍 중 오류 이벤트 전달, 업로드 교체 실패 시 이전 버전 자동 복구 |
| **관리자 기능** | 저장소 zip/폴더 업로드, 문서 업로드, 재분석, 삭제, 민감 저장소 지정, 업로드 한도(zip/해제 크기/파일 수) 설정 |
| **거버넌스** | 민감 저장소는 외부 전송·LangSmith 추적을 정책으로 기본 차단, 사용자/관리자 API 키 분리, 감사 로그는 메타데이터만 기록 |

## 아키텍처

![Re-cat 아키텍처](docs/architecture.png)

```text
사용자/관리자 화면 → Frontend Proxy(:8600, SSE) → FastAPI Backend(:8100)
                                                        │
                                   ┌────────────────────┼────────────────────┐
                                   ▼                    ▼                    ▼
                             MCP Server            Ollama(로컬 LLM)     PostgreSQL + pgvector
                          (정적 분석 그래프)      (fast/quality/embed)   (등록부·대화·감사·문서벡터)
```

코드 질문은 `gate → route → locate → explore → explain → verify → finalize` 경로를, 문서 질문은 `doc_locate → doc_check → doc_explain → doc_verify` 경로를 LangGraph 상태 그래프로 거칩니다. 설계와 신뢰 경계의 전체 설명은 [아키텍처 문서](docs/ARCHITECTURE.md)를, 다이어그램 재생성은 `python docs/architecture_diagram.py`(matplotlib 필요)를 참고하세요.

### 에이전트 그래프 상세

실제 노드 구성(`backend/app/agents/graph.py`)과, 각 분기에서 실제로 어떤 질문이 그 경로를 타는지를 함께 표시한 흐름도입니다.

![Re-cat 에이전트 그래프](docs/agent_flow.png)

재생성: `python docs/agent_flow_diagram.py`(matplotlib 필요).

## 기술 스택

| 구분 | 사용 기술 |
|---|---|
| 백엔드 | FastAPI, LangGraph, LangChain-core, Pydantic |
| 정적 분석 | Python `ast` 표준 라이브러리, `tree-sitter` + `tree-sitter-java` |
| LLM | Ollama(`qwen3:0.6b` 분류/탐색, `qwen3:8b` 설명, `bge-m3` 임베딩), OpenAI 호환 엔드포인트(옵션) |
| 데이터 저장 | PostgreSQL 16 + pgvector(등록부·대화·감사·문서 임베딩) |
| MCP | FastMCP 기반 서브프로세스(정적 분석 그래프·문서 절을 읽기 전용 도구로 제공) |
| 프론트엔드 | Vue 3(CDN, 빌드리스), SSE 기반 실시간 진행 이벤트 |
| 관측 | LangSmith(옵션, 정책으로 입출력 숨김/민감 저장소 차단 강제) |

## 빠른 시작

### 요구 사항

- Python 3.11 이상
- Docker Desktop 또는 PostgreSQL 16 + pgvector
- Ollama 및 모델 `qwen3:0.6b`, `qwen3:8b`, `bge-m3`
- macOS/Linux: Bash · Windows: PowerShell 7 이상

### macOS / Linux

```bash
bash scripts/setup_venv.sh
docker compose up -d db
.venv/bin/python -m backend.app.repositories.migrate
bash scripts/setup_ollama.sh
bash scripts/run_agent_ollma.sh
```

새 터미널에서 웹 화면을 실행합니다.

```bash
bash scripts/run_frontend.sh
```

### Windows

```powershell
python -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -r requirements.txt
docker compose up -d db
.\.venv-win\Scripts\python.exe -m backend.app.repositories.migrate

# Ollama 설치 후 실행합니다. https://ollama.com/download/windows
ollama pull qwen3:0.6b
ollama pull qwen3:8b
ollama pull bge-m3
.\scripts\run_agent_ollama.ps1
```

새 PowerShell 창에서 웹 화면을 실행합니다.

```powershell
.\.venv-win\Scripts\python.exe -m frontend.server
```

### 접속 주소

| 주소 | 용도 |
| --- | --- |
| `http://127.0.0.1:8100/docs` | OpenAPI / Swagger UI |
| `http://127.0.0.1:8600/` | 사용자 화면 |
| `http://127.0.0.1:8600/admin` | 관리자 화면 |

## 설정

`.env.example`을 `.env`로 복사해 API 키와 데이터베이스 주소를 설정합니다.

```dotenv
RECAT_ENV=production
RECAT_API_KEY=replace-with-user-key
RECAT_ADMIN_KEY=replace-with-separate-admin-key
DATABASE_URL=postgresql://recat:password@127.0.0.1:5432/recat
LLM_BACKEND=ollama
OLLAMA_FAST_MODEL=qwen3:0.6b
OLLAMA_QUALITY_MODEL=qwen3:8b
OLLAMA_EMBED_MODEL=bge-m3
```

`RECAT_ENV=production`에서는 사용자·관리자 키가 모두 필요하고 서로 달라야 합니다. 키가 없는 개발 모드는 127.0.0.1 로컬 개발에만 사용하세요. 업로드 한도(`RECAT_UPLOAD_MAX_*_MB`) 등 전체 변수는 [`.env.example`](.env.example)을 참고하세요.

## 테스트

자동 회귀 테스트는 외부 모델 대신 테스트 전용 mock을 사용합니다.

```bash
.venv/bin/python -m pytest -q          # macOS/Linux
.\.venv-win\Scripts\python.exe -m pytest -q   # Windows
```

실제 통합 스모크는 PostgreSQL·MCP·Ollama·API/SSE를 함께 띄워 확인합니다.

```bash
.venv/bin/python -m tools.eval.smoke_ollama --stream
```

실제 모델 기반 비교 평가(코드만 준 LLM vs 구조 제공 vs Re-cat 전체 검증)는 다음과 같습니다.

```bash
.venv/bin/python -m tools.eval.run_eval --backend ollama
```

## 프로젝트 구조

```text
backend/app/
├── main.py              # FastAPI 앱, 라우트, 업로드/재분석/삭제
├── agents/              # LangGraph 에이전트: graph.py(노드 구성) · routing.py · verifier.py · *_llm.py(어댑터)
├── analyzers/           # Python AST / Java tree-sitter 정적 분석
├── retrieval/           # 문서 절 검색·대조, 진입점(entrypoint) 검색
├── security/            # 인증, 정책, 업로드 검증, 마스킹, 감사 로그
├── repositories/        # PostgreSQL 스키마·마이그레이션·세션/삭제 저장소
└── mcp/                 # 정적 분석 그래프·문서 절을 제공하는 MCP 서버

frontend/public/         # web_chat.html, web_admin.html, web_graph.js (Vue 3, 빌드리스)
docs/                    # 아키텍처·운영 문서, 아키텍처 다이어그램
tests/                   # pytest 회귀 테스트 (mock LLM 기반)
tools/eval/              # 실제 모델 비교 평가, 분석기 리포트 CLI
```

## 저장소와 문서 관리

- 분석 대상 코드는 `var/data/repos/<repo>/`에 배치하거나 관리자 화면에서 업로드합니다.
- 기본 정책 저장소는 `policy.yaml`에 정의합니다.
- 관리자 업로드 기본 한도는 zip 250MB, 해제 후 1GB, 파일당 100MB, 파일 5만 개이며 심볼릭 링크·경로 이탈·압축 폭탄을 거부합니다. `.env`의 `RECAT_UPLOAD_MAX_*_MB`로 낮출 수 있고, 앱의 최대 상한(zip 500MB, 해제 후 2GB, 파일 하나 250MB)은 넘길 수 없습니다.
- 문서는 md/txt/rst UTF-8만 허용하며, 업로드 문서는 코드 폴더와 분리해 저장합니다.
- 코드 원문은 DB에 복제하지 않습니다. DB에는 등록부·대화·감사 메타데이터·문서 벡터만 저장합니다.

## 보안과 운영 범위

- 사용자 API 키와 관리자 API 키는 서버에서 분리해 검사합니다.
- 민감 저장소는 기본적으로 외부 추적과 클라우드 경로를 차단합니다.
- 호출 관계 검증은 정적 분석 결과만 기준으로 합니다. LLM의 의도·이유 설명이나 문서의 업무 사실을 자동으로 증명하지는 않습니다.
- 운영 환경에서는 reverse proxy에서 업로드 요청 크기 제한을 추가해야 합니다.

SSO/RLS, 다중 워커, 작업 큐, 사용자·부서별 권한은 현업 확장 단계입니다. 도입 전 점검 항목과 백업·장애 대응은 [운영 가이드](docs/OPERATIONS.md)를 참고하세요.

## 문서

- [아키텍처와 신뢰 모델](docs/ARCHITECTURE.md)
- [설치·운영·점검 가이드](docs/OPERATIONS.md)
- API 명세: 서버 실행 후 `http://127.0.0.1:8100/docs`

## 라이선스와 데이터 취급

이 저장소는 사내 코드 분석용 PoC이며 별도 오픈소스 라이선스를 부여하지 않습니다. 실제 조직의 코드를 올리기 전에는 조직의 보안 정책, 보존 정책, 접근 권한 정책을 검토하세요.
