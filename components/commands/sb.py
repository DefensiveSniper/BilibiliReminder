"""命令组件：!sb，管理 B 站直播间开播提醒的订阅。"""

from __future__ import annotations

import logging
import re
from typing import Any, AsyncGenerator

from langbot_plugin.api.definition.components.command.command import Command
from langbot_plugin.api.entities.builtin.command.context import (
    CommandReturn,
    ExecuteContext,
)
from langbot_plugin.api.entities.builtin.platform.message import (
    At,
    MessageChain,
    Plain,
)
from langbot_plugin.api.entities.builtin.provider.session import LauncherTypes

from bilireminder.client import BilibiliLiveError
from bilireminder.sender import resolve_sender

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "B站开播提醒，真就一点都记不住呗：\n"
    "!sb sub <房间号>     订阅直播间\n"
    "!sb unsub <房间号>   取消订阅\n"
    "!sb rooms            看看你都订了哪些爹\n"
    "!sb status           看看你爹们现在播没播"
)

_ROOM_ID_PATTERN = re.compile(r"(\d+)")


class Sb(Command):
    """群聊和私聊都可用，订阅关系按「机器人 + 会话 + 订阅人」记录。"""

    async def initialize(self) -> None:
        await super().initialize()

        @self.subcommand(
            name="",
            help="查看 B 站开播提醒的用法",
            usage="!sb",
        )
        async def root(
            self, context: ExecuteContext
        ) -> AsyncGenerator[CommandReturn, None]:
            sender = resolve_sender(self.plugin, context)
            async for item in _emit(context, HELP_TEXT, sender=sender):
                yield item

        @self.subcommand(
            name="sub",
            help="订阅一个 B 站直播间",
            usage="!sb sub <房间号>",
        )
        async def sub(
            self, context: ExecuteContext
        ) -> AsyncGenerator[CommandReturn, None]:
            sender = resolve_sender(self.plugin, context)
            text = await _handle_sub(self.plugin, context, sender)
            async for item in _emit(context, text, sender=sender):
                yield item

        @self.subcommand(
            name="unsub",
            help="取消订阅一个 B 站直播间",
            usage="!sb unsub <房间号>",
        )
        async def unsub(
            self, context: ExecuteContext
        ) -> AsyncGenerator[CommandReturn, None]:
            sender = resolve_sender(self.plugin, context)
            text = await _handle_unsub(self.plugin, context, sender)
            async for item in _emit(context, text, sender=sender):
                yield item

        @self.subcommand(
            name="rooms",
            help="查看自己订阅了哪些直播间",
            usage="!sb rooms",
        )
        async def rooms(
            self, context: ExecuteContext
        ) -> AsyncGenerator[CommandReturn, None]:
            sender = resolve_sender(self.plugin, context)
            text = await _handle_rooms(self.plugin, context, sender)
            async for item in _emit(context, text, sender=sender):
                yield item

        @self.subcommand(
            name="status",
            help="查看自己订阅的直播间当前的开播状态",
            usage="!sb status",
        )
        async def status(
            self, context: ExecuteContext
        ) -> AsyncGenerator[CommandReturn, None]:
            sender = resolve_sender(self.plugin, context)
            text = await _handle_status(self.plugin, context, sender)
            async for item in _emit(context, text, sender=sender):
                yield item

        @self.subcommand(
            name="*",
            help="未知用法时给出帮助",
            usage="!sb",
        )
        async def unknown(
            self, context: ExecuteContext
        ) -> AsyncGenerator[CommandReturn, None]:
            # 通配子命令必须至少产出一条返回值：SDK 在通配处理器不产出任何内容时，
            # 会继续走到「未知命令」分支，而那里 raise 的并不是异常类。
            async for item in _emit(
                context, f"没有这个用法。\n{HELP_TEXT}", always_return=True
            ):
                yield item


# ----------------------------------------------------------------------
# 各子命令的处理逻辑
# ----------------------------------------------------------------------


async def _handle_sub(plugin: Any, context: ExecuteContext, sender: str) -> str:
    room_input = _room_id_param(context)
    if not room_input:
        return "房间号呢？你不给号我上哪儿给你订去？"

    try:
        info = await plugin.client.fetch_room(room_input)
    except BilibiliLiveError as e:
        logger.warning("查询直播间 %s 失败：%s", room_input, e)
        return f"抱歉,订阅直播间发生了一个错误：{e}，请联系管理员"

    if info is None:
        return "你没长眼睛吗？房间号对错不知道吗？"

    bot_uuid, target_type, target_id, subscriber_id = await _scope(context, sender)
    added = await plugin.store.subscribe(
        bot_uuid=bot_uuid,
        target_type=target_type,
        target_id=target_id,
        subscriber_id=subscriber_id,
        room_id=info.room_id,
        input_id=room_input,
        up_name=info.up_name,
        live_status=info.live_status,
    )
    if not added:
        return f"你已经注册过B站直播间号[{room_input}],你再注册试试？"

    return (
        f"成功订阅B站直播间号[{room_input}]，你爹是<{info.up_name}>，在开播时我会哈你"
    )


async def _handle_unsub(plugin: Any, context: ExecuteContext, sender: str) -> str:
    room_input = _room_id_param(context)
    if not room_input:
        return "号都不给一个，你取消个寂寞？"

    bot_uuid, target_type, target_id, subscriber_id = await _scope(context, sender)

    # 用户可能用短号订阅、用真实房间号取消（或者反过来），两个号都拿来匹配
    candidates = [room_input]
    up_name: str | None = None
    try:
        info = await plugin.client.fetch_room(room_input)
    except BilibiliLiveError as e:
        logger.warning("查询直播间 %s 失败：%s", room_input, e)
        info = None
    if info is not None:
        candidates.append(info.room_id)
        up_name = info.up_name

    room_id = await plugin.store.find_room_id(
        bot_uuid=bot_uuid,
        target_type=target_type,
        target_id=target_id,
        subscriber_id=subscriber_id,
        candidates=candidates,
    )
    if room_id is None:
        return f"你™订阅你爹<{up_name or room_input}>了吗？你就取消，再发给你卤煮扬了！"

    removed = await plugin.store.unsubscribe(
        bot_uuid=bot_uuid,
        target_type=target_type,
        target_id=target_id,
        subscriber_id=subscriber_id,
        room_id=room_id,
    )
    name = up_name or (removed or {}).get("up_name") or room_input
    return f"不看你爹<{name}>就滚"


async def _handle_rooms(plugin: Any, context: ExecuteContext, sender: str) -> str:
    rooms = await _list_rooms(plugin, context, sender)
    if not rooms:
        return "你订阅了个蛋？没订阅你瞎发什么？"

    listed = ", ".join(f"{room['up_name']}({_display_id(room)})" for room in rooms)
    return f"傻呗吗你？这你都能忘？给大伙看看你的爹爹们：<{listed}>"


async def _handle_status(plugin: Any, context: ExecuteContext, sender: str) -> str:
    rooms = await _list_rooms(plugin, context, sender)
    if not rooms:
        return "你订阅了个蛋？没订阅你瞎发什么？"

    infos = await plugin.client.fetch_rooms(room["room_id"] for room in rooms)

    lines = ["你爹们的近况："]
    for room in rooms:
        display_id = _display_id(room)
        info = infos.get(room["room_id"])
        if info is None:
            lines.append(f"{room['up_name']}({display_id})：查不到，B站又抽风了")
        elif info.is_living:
            lines.append(
                f"{info.up_name}({display_id})：{info.status_text}"
                f"《{info.title}》 {info.live_url}"
            )
        else:
            lines.append(f"{info.up_name}({display_id})：{info.status_text}")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------


async def _emit(
    context: ExecuteContext,
    text: str,
    *,
    sender: str | None = None,
    always_return: bool = False,
) -> AsyncGenerator[CommandReturn, None]:
    """回复命令。群聊里 @ 一下发起人，私聊直接回文本。

    always_return 为真时强制走 CommandReturn，不使用消息链（见通配子命令处的说明）。
    """
    if not always_return and context.session.launcher_type == LauncherTypes.GROUP:
        try:
            await context.reply(
                MessageChain(
                    [
                        At(target=sender or context.session.sender_id),
                        Plain(text=f" {text}"),
                    ]
                )
            )
            return
        except Exception:
            logger.exception("以消息链回复失败，改为纯文本回复")

    yield CommandReturn(text=text)


async def _scope(context: ExecuteContext, sender: str) -> tuple[str, str, str, str]:
    """返回 (机器人 UUID, 会话类型, 会话 ID, 订阅人 ID)。"""
    bot_uuid = str(context.session.bot_uuid or "")
    if not bot_uuid:
        bot_uuid = str(await context.get_bot_uuid())

    target_type = context.session.launcher_type.value
    target_id = str(context.session.launcher_id)
    return bot_uuid, target_type, target_id, sender


async def _list_rooms(
    plugin: Any, context: ExecuteContext, sender: str
) -> list[dict[str, Any]]:
    bot_uuid, target_type, target_id, subscriber_id = await _scope(context, sender)
    return await plugin.store.list_rooms(
        bot_uuid=bot_uuid,
        target_type=target_type,
        target_id=target_id,
        subscriber_id=subscriber_id,
    )


def _room_id_param(context: ExecuteContext) -> str:
    """取出命令中的房间号，支持直接粘贴直播间链接。"""
    if not context.crt_params:
        return ""
    matched = _ROOM_ID_PATTERN.findall(context.crt_params[0].strip())
    return matched[-1] if matched else ""


def _display_id(room: dict[str, Any]) -> str:
    """展示用户当初输入的房间号，没有则展示真实房间号。"""
    return str(room.get("input_id") or room.get("room_id") or "")
