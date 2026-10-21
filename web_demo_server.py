"""호환용 웹 실행 진입점. 웹 프록시는 apps.web.server에 있다."""
import sys
from frontend import server as _web

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(_web.app, host="127.0.0.1", port=8600)
else:
    sys.modules[__name__] = _web
