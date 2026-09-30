"""로스트아크 공식 Open API 클라이언트 (https://developer-lostark.game.onstove.com).

쓰는 엔드포인트
- GET /characters/{name}/siblings          : 원정대(계정) 전체 캐릭터의 서버/직업/아이템 레벨
- GET /armories/characters/{name}/profiles : 캐릭터 한 명의 프로필

분당 100회 제한이 있어 요청 사이에 간격을 두고, 429 를 받으면 리셋 시각까지 기다렸다가 한 번 재시도한다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

BASE_URL = "https://developer-lostark.game.onstove.com"
MIN_INTERVAL = 0.7  # 초. 분당 약 85회로 제한 안쪽 유지


class ApiError(Exception):
    """사용자에게 그대로 보여줄 수 있는 API 오류.

    fatal=True 는 점검/키 오류처럼 다른 요청도 실패할 게 뻔한 경우 (일괄 갱신을 중단한다).
    """

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


@dataclass(frozen=True)
class ApiCharacter:
    name: str
    server: str
    cls: str
    item_level: float


def parse_item_level(text: str | None) -> float:
    """'1,720.83' → 1720.83"""
    if not text:
        return 0.0
    return float(str(text).replace(",", ""))


def _character(data: dict) -> ApiCharacter:
    return ApiCharacter(
        name=data["CharacterName"],
        server=data.get("ServerName") or "",
        cls=data.get("CharacterClassName") or "",
        item_level=parse_item_level(data.get("ItemAvgLevel")),
    )


class LostArkClient:
    def __init__(self, api_key: str, session: aiohttp.ClientSession | None = None):
        self._headers = {"accept": "application/json", "authorization": f"bearer {api_key}"}
        self._session = session
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        async with self._lock:  # 요청을 한 줄로 세워 간격 유지
            for attempt in range(2):
                wait = self._last_request + MIN_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request = time.monotonic()
                try:
                    async with self._session.get(BASE_URL + path, headers=self._headers) as resp:
                        if resp.status == 429 and attempt == 0:
                            reset = int(resp.headers.get("X-RateLimit-Reset", "0") or 0)
                            delay = min(max(reset - time.time(), 1), 60)
                            log.warning("Lost Ark API 요청 한도 초과, %.0f초 대기", delay)
                            await asyncio.sleep(delay)
                            continue
                        if resp.status == 401:
                            raise ApiError("API 키가 올바르지 않아요. .env 의 LOSTARK_API_KEY 를 확인해 주세요.", fatal=True)
                        if resp.status == 429:
                            raise ApiError("API 요청 한도를 넘었어요. 1분 뒤 다시 시도해 주세요.", fatal=True)
                        if resp.status == 503:
                            raise ApiError("로스트아크 서버 점검 중이라 정보를 가져올 수 없어요.", fatal=True)
                        if resp.status != 200:
                            raise ApiError(f"API 응답 오류 (HTTP {resp.status})")
                        return await resp.json(content_type=None)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:  # ValueError: 점검 중 HTML 응답 등
                    raise ApiError(f"API 에 연결하지 못했어요: {type(e).__name__}") from e
        raise ApiError("API 요청 한도를 넘었어요. 1분 뒤 다시 시도해 주세요.")

    async def siblings(self, name: str) -> list[ApiCharacter]:
        """원정대 전체 캐릭터. 없는 캐릭터면 빈 목록."""
        data = await self._get(f"/characters/{quote(name)}/siblings")
        return [_character(d) for d in data or []]

    async def profile(self, name: str) -> ApiCharacter | None:
        data = await self._get(f"/armories/characters/{quote(name)}/profiles")
        return _character(data) if data else None
