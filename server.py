"""호환용 API 실행 진입점. 애플리케이션은 apps.api.app.main에 있다."""
import sys
from backend.app import main as _main

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(_main.app, host="127.0.0.1", port=8100, log_level="warning")
else:
    # eval·기존 자동 테스트의 import server를 유지한다. 밑줄 속성까지 같은 모듈을 가리킨다.
    sys.modules[__name__] = _main
