"""解析「当前这条命令是谁发的」。

群聊的 `Session` 对象在同一个群内是复用的（LangBot 按 机器人 + 会话类型 + 会话 ID
缓存会话），`session.sender_id` 记录的是**首次创建该会话的人**，并不是当前这条命令的
发送者。命令上下文里又没有发送者字段，所以这里借助命令事件：LangBot 在执行命令前会先
派发 `GroupCommandSent` / `PersonCommandSent`，事件里带着本次请求真实的 sender_id，
事件监听器把它按请求键记下来，命令处理时再取出来用。
"""

from __future__ import annotations

from typing import Any


def query_key(ctx: Any) -> str:
    """同一次请求在事件和命令之间的关联键。"""
    query_uuid = getattr(ctx, "query_uuid", None)
    if query_uuid:
        return f"uuid:{query_uuid}"
    return f"id:{getattr(ctx, 'query_id', '')}"


def resolve_sender(plugin: Any, context: Any) -> str:
    """取出本次命令的发送者 ID。

    事件里没拿到时退回 `session.sender_id`（私聊下它就是对方本人，群聊下只是个兜底）。
    """
    sender = plugin.pop_sender(query_key(context))
    if sender:
        return str(sender)

    session = context.session
    return str(session.sender_id or session.launcher_id)
