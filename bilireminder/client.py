"""B 站直播间信息查询客户端。"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

ROOM_BASE_INFO_API = (
    "https://api.live.bilibili.com/xlive/web-room/v1/index/getRoomBaseInfo"
)

LIVE_STATUS_OFFLINE = 0
"""未开播"""
LIVE_STATUS_LIVING = 1
"""直播中"""
LIVE_STATUS_ROUND = 2
"""轮播中"""

LIVE_STATUS_TEXT = {
    LIVE_STATUS_OFFLINE: "未开播",
    LIVE_STATUS_LIVING: "直播中",
    LIVE_STATUS_ROUND: "轮播中",
}

# 接口一次能稳定返回的房间数量有限，分片查询
_BATCH_SIZE = 20
_BATCH_INTERVAL = 0.5

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://live.bilibili.com/",
}


class BilibiliLiveError(Exception):
    """访问 B 站接口失败。"""


@dataclasses.dataclass(frozen=True)
class RoomInfo:
    """直播间基础信息。"""

    room_id: str
    """真实房间号"""
    short_id: str
    """短号，没有短号时为空字符串"""
    up_name: str
    title: str
    cover: str
    live_url: str
    live_status: int

    @property
    def is_living(self) -> bool:
        return self.live_status == LIVE_STATUS_LIVING

    @property
    def status_text(self) -> str:
        return LIVE_STATUS_TEXT.get(self.live_status, f"未知状态({self.live_status})")


class BilibiliLiveClient:
    """查询直播间状态，复用同一个 HTTP 连接池。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def fetch_rooms(self, room_ids: Iterable[Any]) -> dict[str, RoomInfo]:
        """批量查询房间信息。

        返回 ``{查询时使用的房间号: RoomInfo}``，不存在的房间号不会出现在结果中。
        某个分片请求失败时只记录日志并跳过，不影响其他分片。
        """
        wanted = list(dict.fromkeys(str(room_id) for room_id in room_ids))
        result: dict[str, RoomInfo] = {}
        for index in range(0, len(wanted), _BATCH_SIZE):
            chunk = wanted[index : index + _BATCH_SIZE]
            if index:
                await asyncio.sleep(_BATCH_INTERVAL)
            try:
                result.update(await self._fetch_chunk(chunk))
            except BilibiliLiveError as e:
                logger.warning("查询直播间 %s 失败：%s", ",".join(chunk), e)
        return result

    async def fetch_room(self, room_id: Any) -> RoomInfo | None:
        """查询单个房间，房间不存在时返回 None，接口异常时抛出 BilibiliLiveError。"""
        room_id = str(room_id)
        rooms = await self._fetch_chunk([room_id])
        return rooms.get(room_id)

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    timeout=self._timeout, headers=_HEADERS
                )
            return self._client

    async def _fetch_chunk(self, room_ids: list[str]) -> dict[str, RoomInfo]:
        client = await self._get_client()
        params: list[tuple[str, str]] = [("room_ids", room_id) for room_id in room_ids]
        params.append(("req_biz", "web"))

        try:
            response = await client.get(ROOM_BASE_INFO_API, params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            raise BilibiliLiveError(str(e)) from e

        code = payload.get("code")
        if code != 0:
            raise BilibiliLiveError(f"接口返回 code={code}，message={payload.get('message')}")

        by_room_ids = (payload.get("data") or {}).get("by_room_ids") or {}

        # 接口以真实房间号为 key 返回，这里把短号也索引上，
        # 这样用户用短号查询时同样能取回结果。
        index: dict[str, RoomInfo] = {}
        for raw in by_room_ids.values():
            try:
                info = _parse_room(raw)
            except Exception:
                logger.warning("解析直播间信息失败：%s", raw)
                continue
            index[info.room_id] = info
            if info.short_id and info.short_id != "0":
                index.setdefault(info.short_id, info)

        return {room_id: index[room_id] for room_id in room_ids if room_id in index}


def _parse_room(raw: dict[str, Any]) -> RoomInfo:
    room_id = str(raw.get("room_id") or "")
    return RoomInfo(
        room_id=room_id,
        short_id=str(raw.get("short_id") or ""),
        up_name=str(raw.get("uname") or "未知UP主"),
        title=str(raw.get("title") or ""),
        cover=str(raw.get("cover") or ""),
        live_url=str(raw.get("live_url") or f"https://live.bilibili.com/{room_id}"),
        live_status=int(raw.get("live_status") or 0),
    )
