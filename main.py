"""BilibiliReminder：订阅 B 站 UP 主的开播状态，开播时主动推送提醒。

在 https://github.com/Hanschase/BreminderPlugin 的基础上进行的修改。
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any

from langbot_plugin.api.definition.plugin import BasePlugin
from langbot_plugin.api.entities.builtin.platform.message import (
    At,
    Image,
    MessageChain,
    Plain,
)

from bilireminder.client import LIVE_STATUS_LIVING, BilibiliLiveClient, RoomInfo
from bilireminder.store import SubscriptionStore, make_session_key

logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL = 60
MIN_CHECK_INTERVAL = 15
FIRST_CHECK_DELAY = 5
SENDER_CACHE_SIZE = 256


class BilibiliReminder(BasePlugin):
    """插件入口：持有订阅数据、B 站客户端和后台轮询任务。"""

    store: SubscriptionStore
    client: BilibiliLiveClient

    def __init__(self) -> None:
        super().__init__()
        # 请求键 -> 该次命令的真实发送者，由事件监听器写入、命令处理时取走
        self._senders: OrderedDict[str, str] = OrderedDict()

    async def initialize(self) -> None:
        self.store = SubscriptionStore(self)
        await self.store.load()
        self.client = BilibiliLiveClient()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "BilibiliReminder 已启动，开播查询间隔 %d 秒", self.check_interval
        )

    def __del__(self) -> None:
        task = getattr(self, "_poll_task", None)
        if task is not None and not task.done():
            task.cancel()

    # ------------------------------------------------------------------
    # 命令发送者
    # ------------------------------------------------------------------

    def remember_sender(self, query_key: str, sender_id: str) -> None:
        """记录某次请求的真实发送者，见 bilireminder/sender.py。"""
        if not query_key or not sender_id:
            return
        self._senders[query_key] = str(sender_id)
        self._senders.move_to_end(query_key)
        # 命令若被其他插件拦截就不会有人来取，这里按容量淘汰，避免无限增长
        while len(self._senders) > SENDER_CACHE_SIZE:
            self._senders.popitem(last=False)

    def pop_sender(self, query_key: str) -> str | None:
        return self._senders.pop(query_key, None)

    # ------------------------------------------------------------------
    # 配置项
    # ------------------------------------------------------------------

    @property
    def _config(self) -> dict[str, Any]:
        try:
            return self.get_config() or {}
        except Exception:
            return {}

    @property
    def check_interval(self) -> int:
        """查询频率，单位为秒。过小的间隔容易被 B 站限流，因此有下限。"""
        try:
            interval = int(self._config.get("check_interval") or DEFAULT_CHECK_INTERVAL)
        except (TypeError, ValueError):
            interval = DEFAULT_CHECK_INTERVAL
        return max(interval, MIN_CHECK_INTERVAL)

    @property
    def default_cover(self) -> str:
        """UP 主没设置封面时使用的兜底图片，留空表示不附图片。"""
        return str(self._config.get("default_cover") or "").strip()

    @property
    def notify_admin(self) -> bool:
        """发生问题时是否通知管理员。"""
        return bool(self._config.get("notify_admin"))

    @property
    def admin_id(self) -> str:
        return str(self._config.get("admin_id") or "").strip()

    # ------------------------------------------------------------------
    # 后台轮询
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        await asyncio.sleep(FIRST_CHECK_DELAY)
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("轮询直播间状态时发生未预期的错误")
            await asyncio.sleep(self.check_interval)

    async def check_once(self) -> None:
        """查询一轮所有被订阅的直播间，并对新开播的直播间推送提醒。"""
        sessions = await self.store.snapshot()
        room_ids = {room_id for session in sessions for room_id in session["rooms"]}
        if not room_ids:
            return

        infos = await self.client.fetch_rooms(sorted(room_ids))
        if not infos:
            logger.warning("本轮未能查询到任何直播间信息，跳过本次检查")
            return

        for session in sessions:
            session_key = make_session_key(
                session["bot_uuid"], session["target_type"], session["target_id"]
            )
            for room_id, room in session["rooms"].items():
                info = infos.get(room_id)
                if info is None:
                    logger.warning("未查询到直播间 %s 的信息，本轮跳过", room_id)
                    continue

                previous_status = int(room.get("live_status") or 0)
                await self.store.update_room(
                    session_key=session_key,
                    room_id=room_id,
                    live_status=info.live_status,
                    up_name=info.up_name,
                )

                # 只在「此前不在直播中」变为「直播中」时提醒，轮播结束后再开播同样会提醒
                if previous_status != LIVE_STATUS_LIVING and info.is_living:
                    await self._notify(session, room, info)

    async def _notify(
        self, session: dict[str, Any], room: dict[str, Any], info: RoomInfo
    ) -> None:
        """向订阅了该直播间的会话推送开播提醒。"""
        target_type = session["target_type"]
        target_id = session["target_id"]
        bot_uuid = session["bot_uuid"]

        components: list[Any] = []
        if target_type == "group":
            # 私聊消息本来就只发给订阅者本人，只有群聊需要 @ 出来
            components.extend(
                At(target=subscriber) for subscriber in room["subscribers"]
            )
        components.append(Plain(text="\n您订阅的直播间开播啦！"))

        # UP 主没设封面、用户也没配兜底图时就只发文字，不附一张必定加载失败的图
        cover = info.cover or self.default_cover
        if cover:
            components.append(Image(url=cover))

        components.append(
            Plain(
                text=(
                    f"直播间标题：{info.title}"
                    f"\nUP主：{info.up_name}"
                    f"\n直播间地址：{info.live_url}"
                )
            )
        )

        try:
            await self.send_message(
                bot_uuid=bot_uuid,
                target_type=target_type,
                target_id=str(target_id),
                message_chain=MessageChain(components),
            )
        except Exception as e:
            logger.exception(
                "向 %s %s 推送直播间 %s 的开播提醒失败", target_type, target_id, info.room_id
            )
            await self.notify_admin_if_needed(
                bot_uuid,
                f"直播间通知插件出了点问题，去看看后台，"
                f"房间号：{info.room_id}，会话：{target_type}_{target_id}，错误：{e}",
            )
            return

        logger.info(
            "已向 %s %s 推送 %s（房间号 %s）的开播提醒",
            target_type,
            target_id,
            info.up_name,
            info.room_id,
        )

    async def notify_admin_if_needed(self, bot_uuid: str, text: str) -> None:
        """在配置开启时，把异常信息私聊发给管理员。"""
        if not self.notify_admin or not self.admin_id or not bot_uuid:
            return
        try:
            await self.send_message(
                bot_uuid=bot_uuid,
                target_type="person",
                target_id=self.admin_id,
                message_chain=MessageChain([Plain(text=text)]),
            )
        except Exception:
            logger.exception("通知管理员 %s 失败", self.admin_id)
