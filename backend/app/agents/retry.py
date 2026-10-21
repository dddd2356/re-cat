"""Wave 9 — Transient 에러만 exponential backoff로 재시도.

asyncio.TimeoutError와 TransientError(일시적)만 재시도하고, 그 외(ValueError 등 영구적)는 즉시 전파한다.
트러블슈팅 ②: 예전에는 MCP 호출에 시간 제한이 없어 TimeoutError가 날 일이 없었다 — 재시도가 시뮬레이션에서만 돌았다.
이제 MCPClient가 시간 제한·연결 끊김을 TransientError로 바꿔 올리고, 서브프로세스를 다시 띄운 뒤 여기서 재시도한다.
"""
import asyncio

from langsmith import traceable


class TransientError(RuntimeError):
    """다시 시도하면 성공할 수 있는 오류(시간 초과, 서브프로세스 재시작 직후 등)."""


@traceable(name="with_retry")
async def with_retry(fn, *args, retries: int = 3, base_delay: float = 0.3, **kwargs):
    attempt = 0
    while True:
        attempt += 1
        try:
            result = await fn(*args, **kwargs)
            return result, attempt - 1   # (결과, 재시도 횟수)
        except (asyncio.TimeoutError, TransientError):
            if attempt >= retries:
                raise
            delay = base_delay * (3 ** (attempt - 1))
            await asyncio.sleep(delay)
