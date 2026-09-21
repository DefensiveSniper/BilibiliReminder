"""订阅数据存储，基于 LangBot 提供的插件持久化存储 API。"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

STORAGE_KEY = "subscriptions"
DATA_VERSION = 2


def make_session_key(bot_uuid: str, target_type: str, target_id: str) -> str:
    """一个会话由 机器人 + 会话类型 + 会话 ID 唯一确定。"""
    return f"{bot_uuid}|{target_type}|{target_id}"


class SubscriptionStore:
    """订阅关系存储。

    数据结构::

        {
          "version": 2,
          "sessions": {
            "<bot_uuid>|<target_type>|<target_id>": {
              "bot_uuid": "...",
              "target_type": "group",      # group / person
              "target_id": "...",          # 群号或用户 ID
              "rooms": {
                "<真实房间号>": {
                  "up_name": "...",
                  "input_id": "...",       # 用户订阅时输入的房间号（可能是短号）
                  "live_status": 0,        # 上一次查询到的开播状态
                  "subscribers": ["..."]   # 订阅者 ID 列表
                }
              }
            }
          }
        }
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = _empty_data()

    async def load(self) -> None:
        """从插件存储中读取订阅数据，读取失败时以空数据启动。"""
        async with self._lock:
            raw: bytes | None = None
            try:
                keys = await self._plugin.get_plugin_storage_keys()
                if STORAGE_KEY in keys:
                    raw = await self._plugin.get_plugin_storage(STORAGE_KEY)
            except Exception:
                logger.exception("读取订阅数据失败，本次以空订阅数据启动")
                return

            if not raw:
                return

            try:
                self._data = _normalize(json.loads(raw.decode("utf-8")))
            except Exception:
                logger.exception("订阅数据解析失败，本次以空订阅数据启动")
                self._data = _empty_data()
                return

            logger.info(
                "已加载 %d 个会话的订阅数据", len(self._data["sessions"])
            )

    async def snapshot(self) -> list[dict[str, Any]]:
        """返回所有会话订阅数据的副本，供轮询任务使用。"""
        async with self._lock:
            return copy.deepcopy(list(self._data["sessions"].values()))

    async def subscribe(
        self,
        *,
        bot_uuid: str,
        target_type: str,
        target_id: str,
        subscriber_id: str,
        room_id: str,
        input_id: str,
        up_name: str,
        live_status: int,
    ) -> bool:
        """新增订阅，返回 False 表示该用户已订阅过这个直播间。"""
        async with self._lock:
            key = make_session_key(bot_uuid, target_type, target_id)
            session = self._data["sessions"].setdefault(
                key,
                {
                    "bot_uuid": bot_uuid,
                    "target_type": target_type,
                    "target_id": target_id,
                    "rooms": {},
                },
            )
            room = session["rooms"].get(room_id)
            if room is None:
                room = {
                    "up_name": up_name,
                    "input_id": input_id,
                    # 订阅时已在直播中的话，先记下当前状态，避免下一次轮询立刻推送
                    "live_status": live_status,
                    "subscribers": [],
                }
                session["rooms"][room_id] = room
            else:
                room["up_name"] = up_name

            if subscriber_id in room["subscribers"]:
                return False

            room["subscribers"].append(subscriber_id)
            await self._save()
            return True

    async def unsubscribe(
        self,
        *,
        bot_uuid: str,
        target_type: str,
        target_id: str,
        subscriber_id: str,
        room_id: str,
    ) -> dict[str, Any] | None:
        """取消订阅，返回被取消的房间信息；未订阅时返回 None。"""
        async with self._lock:
            key = make_session_key(bot_uuid, target_type, target_id)
            session = self._data["sessions"].get(key)
            if session is None:
                return None

            room = session["rooms"].get(room_id)
            if room is None or subscriber_id not in room["subscribers"]:
                return None

            removed = copy.deepcopy(room)
            room["subscribers"].remove(subscriber_id)

            # 逐层清理：没人订阅的房间、没有房间的会话都不再保留
            if not room["subscribers"]:
                del session["rooms"][room_id]
            if not session["rooms"]:
                del self._data["sessions"][key]

            await self._save()
            return removed

    async def find_room_id(
        self,
        *,
        bot_uuid: str,
        target_type: str,
        target_id: str,
        subscriber_id: str,
        candidates: list[str],
    ) -> str | None:
        """在该用户的订阅中查找房间号，支持用短号或真实房间号匹配。"""
        async with self._lock:
            session = self._data["sessions"].get(
                make_session_key(bot_uuid, target_type, target_id)
            )
            if session is None:
                return None

            for room_id, room in session["rooms"].items():
                if subscriber_id not in room["subscribers"]:
                    continue
                if room_id in candidates or str(room.get("input_id")) in candidates:
                    return room_id
            return None

    async def list_rooms(
        self,
        *,
        bot_uuid: str,
        target_type: str,
        target_id: str,
        subscriber_id: str,
    ) -> list[dict[str, Any]]:
        """列出某个用户在当前会话中订阅的所有直播间。"""
        async with self._lock:
            session = self._data["sessions"].get(
                make_session_key(bot_uuid, target_type, target_id)
            )
            if session is None:
                return []

            return [
                dict(room, room_id=room_id)
                for room_id, room in session["rooms"].items()
                if subscriber_id in room["subscribers"]
            ]

    async def update_room(
        self,
        *,
        session_key: str,
        room_id: str,
        live_status: int | None = None,
        up_name: str | None = None,
    ) -> None:
        """更新房间的缓存状态。房间已被取消订阅时静默跳过。"""
        async with self._lock:
            session = self._data["sessions"].get(session_key)
            if session is None:
                return
            room = session["rooms"].get(room_id)
            if room is None:
                return

            changed = False
            if live_status is not None and room.get("live_status") != live_status:
                room["live_status"] = live_status
                changed = True
            if up_name and room.get("up_name") != up_name:
                room["up_name"] = up_name
                changed = True

            if changed:
                await self._save()

    async def _save(self) -> None:
        """写回插件存储。调用方需持有 self._lock。"""
        try:
            await self._plugin.set_plugin_storage(
                STORAGE_KEY,
                json.dumps(self._data, ensure_ascii=False).encode("utf-8"),
            )
        except Exception:
            logger.exception("保存订阅数据失败，本次修改可能在重启后丢失")


def _empty_data() -> dict[str, Any]:
    return {"version": DATA_VERSION, "sessions": {}}


def _normalize(data: Any) -> dict[str, Any]:
    """容错地把存储中的数据规整成当前版本的结构。"""
    if not isinstance(data, dict):
        return _empty_data()

    sessions: dict[str, Any] = {}
    for key, session in (data.get("sessions") or {}).items():
        if not isinstance(session, dict):
            continue

        rooms: dict[str, Any] = {}
        for room_id, room in (session.get("rooms") or {}).items():
            if not isinstance(room, dict):
                continue
            subscribers = [
                str(subscriber) for subscriber in (room.get("subscribers") or [])
            ]
            if not subscribers:
                continue
            rooms[str(room_id)] = {
                "up_name": str(room.get("up_name") or "未知UP主"),
                "input_id": str(room.get("input_id") or room_id),
                "live_status": int(room.get("live_status") or 0),
                "subscribers": subscribers,
            }

        if not rooms:
            continue

        sessions[str(key)] = {
            "bot_uuid": str(session.get("bot_uuid") or ""),
            "target_type": str(session.get("target_type") or "group"),
            "target_id": str(session.get("target_id") or ""),
            "rooms": rooms,
        }

    return {"version": DATA_VERSION, "sessions": sessions}
