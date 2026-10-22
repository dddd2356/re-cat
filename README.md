# Re-cat

Re-cat은 Python·Java 레거시 코드의 호출 관계를 정적 분석으로 확인하고, 로컬 Ollama가 그 근거를 설명하는 온프레미스 코드 인수인계 도구입니다.

LLM이 만든 설명은 신뢰의 출발점이 아닙니다. 답변에 포함된 호출 관계는 분석 그래프와 다시 대조하며, 검증하지 못한 내용은 구조적 사실과 분리해 표시합니다.

> 현재 버전은 **단일 조직·단일 서버·단일 프로세스 PoC**입니다. 사용자별·부서별 접근 제어와 다중 인스턴스 운영은 아직 지원하지 않습니다. 자세한 범위는 [운영 가이드](docs/OPERATIONS.md#현재-mvp-범위)를 확인하세요.

## 주요 기능

- Python AST와 Java tree-sitter 기반 호출 그래프 분석
- Ollama 기반 한국어 코드 흐름 설명과 결정적 호출 관계 검증
- 문서(md/txt/rst)와 코드의 호출 관계 대조
- PostgreSQL + pgvector 기반 저장소 등록, 대화 메타데이터, 감사 로그, 문서 검색
- 관리자 저장소 등록·zip/폴더 업로드·문서 업로드·재분석·삭제
- SSE 진행 이벤트, 정적 분석 fallback, MCP 하위 프로세스 자동 복구
- 민감 저장소의 외부 추적/클라우드 경로 차단 정책

## 동작 원리

```text
Browser → API → 정적 분석 MCP → 호출 그래프
                 ↓
            Ollama 설명 생성 → 호출 주장 검증 → 응답
                 ↓
       PostgreSQL(등록부·대화·감사·문서 벡터)
```

코드 흐름 질문은 `route → locate → explore → explain → verify → finalize` 경로를 거칩니다. 설명의 호출 주장이 그래프와 맞지 않으면 재설명을 시도하고, 끝내 검증되지 않으면 LLM 설명을 버린 뒤 정적 분석 결과만 반환합니다. 전체 설계는 [아키텍처 문서](docs/ARCHITECTURE.md)를 참고하세요.

## 요구 사항

- Python 3.11 이상
- Docker Desktop 또는 PostgreSQL 16 + pgvector
- Ollama 및 모델 `qwen3:0.6b`, `qwen3:8b`, `bge-m3`
- macOS/Linux: Bash
- Windows: PowerShell 7 또는 Windows PowerShell

## 빠른 시작

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

`RECAT_ENV=production`에서는 사용자·관리자 키가 모두 필요하고 서로 달라야 합니다. 키가 없는 개발 모드는 127.0.0.1 로컬 개발에만 사용하세요.

## 검증

자동 회귀 테스트는 외부 모델 대신 테스트 전용 mock을 사용합니다.

```bash
.venv/bin/python -m pytest -q
```

```powershell
.\.venv-win\Scripts\python.exe -m pytest -q
```

실제 통합 스모크는 PostgreSQL, MCP, Ollama, API/SSE를 함께 확인합니다.

```bash
.venv/bin/python -m tools.eval.smoke_ollama --stream
```

실제 모델 비교 평가는 다음과 같습니다.

```bash
.venv/bin/python -m tools.eval.run_eval --backend ollama
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

이 저장소는 코드 분석용 PoC입니다. 실제 조직의 코드를 올리기 전에는 조직의 보안 정책, 보존 정책, 접근 권한 정책을 검토하세요.
