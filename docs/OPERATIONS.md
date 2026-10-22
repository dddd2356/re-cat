# 설치·운영·점검 가이드

## 현재 MVP 범위

Re-cat은 단일 조직의 내부망 단일 서버 PoC를 대상으로 합니다. API 키로 사용자와 관리자를 나누고 저장소 단위로 격리하지만, 사용자·부서별 권한과 SSO는 아직 없습니다. 채팅·업로드·재분석은 안전한 파일 교체를 위해 한 프로세스에서 직렬 처리되므로 동시 사용자 서비스 용도가 아닙니다. 인터넷에 직접 공개하지 말고 127.0.0.1 또는 사내 reverse proxy 뒤에서 운영하세요.

## 시작 전 점검

1. PostgreSQL과 pgvector가 실행 중인지 확인합니다.
2. `.env`에 서로 다른 `RECAT_API_KEY`, `RECAT_ADMIN_KEY`를 설정합니다.
3. 운영이면 `RECAT_ENV=production`을 설정합니다.
4. Ollama와 `qwen3:0.6b`, `qwen3:8b`, `bge-m3` 모델을 준비합니다.
5. `policy.yaml`에서 허용 저장소와 민감 저장소 정책을 검토합니다.
6. LangSmith를 켤 때는 `hide_inputs_outputs: true`를 유지합니다. 애플리케이션은 이를 강제하지만, 민감 저장소는 trace를 켜지 않습니다.

## 기동 순서

```bash
docker compose up -d db
.venv/bin/python -m backend.app.repositories.migrate
bash scripts/run_agent_ollma.sh
```

별도 터미널에서 프록시를 실행합니다.

```bash
bash scripts/run_frontend.sh
```

Windows는 [README](../README.md#windows)의 PowerShell 절차를 사용합니다.

## 릴리스 전 점검

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m tools.eval.smoke_ollama --stream
```

첫 명령은 회귀 테스트, 두 번째는 실제 PostgreSQL·Ollama·MCP·API/SSE 통합 스모크입니다. Ollama 모델이 없거나 서비스가 꺼져 있으면 두 번째 명령은 실패합니다.

## 업로드 운영

- 관리자만 저장소와 외부 문서를 등록·삭제할 수 있습니다.
- 업로드 기본 제한은 zip 250MB, 해제 후 1GB, 파일당 100MB, 파일 5만 개입니다. `.env`의 `RECAT_UPLOAD_MAX_ZIP_MB`, `RECAT_UPLOAD_MAX_TOTAL_MB`, `RECAT_UPLOAD_MAX_FILE_MB`로 조직 정책에 맞게 낮출 수 있으며, 앱의 절대 상한은 각각 500MB, 2GB, 250MB입니다.
- 서비스 앞 reverse proxy에서도 요청 본문 크기를 제한하세요. 애플리케이션의 제한만으로는 chunked 대용량 multipart 요청을 가장 이른 지점에서 차단할 수 없습니다.
- 업로드·재분석·삭제는 단일 프로세스 기준으로 직렬화됩니다.

## 데이터 보존과 백업

- PostgreSQL 볼륨은 정기 백업합니다. 대화·감사 메타데이터·등록부·문서 벡터는 DB에 있습니다.
- `var/data/repos`와 `var/data/docs`는 코드·업로드 문서 원문이므로 DB 백업과 별도로 백업합니다.
- 저장소 삭제는 되돌리기 어렵습니다. 관리자 화면의 이름 재입력 확인은 오작동 방지 장치일 뿐 백업을 대체하지 않습니다.

## 장애 대응

| 증상 | 우선 확인 |
| --- | --- |
| API가 기동하지 않음 | PostgreSQL 연결, migration, `.env`, production 키 설정 |
| 문서 검색이 keyword fallback | Ollama 실행 여부와 `bge-m3` 설치 여부 |
| 설명 없이 구조만 표시 | Ollama 상태와 API 로그; 정적 fallback은 의도된 동작 |
| 업로드 실패 | 관리자 키, 파일 형식·크기·경로·압축 제한 |
| 응답이 느림 | Ollama 모델 로딩, MCP 재분석, 동시 요청 수 |

## 현업 전환 전 필수 과제

다음 기능은 현 MVP의 범위 밖입니다. 여러 부서나 다중 서버 운영 전에 구현·검증해야 합니다.

- SSO/OIDC와 사용자·부서·역할 기반 인가
- PostgreSQL RLS 및 저장소 존재 숨김 정책
- 작업 큐와 격리된 분석·임베딩 워커
- 분산 락 또는 DB advisory lock
- reverse proxy 업로드 제한, TLS, 보안 헤더
- 사용자 API 키를 대신하는 세션 기반 인증
