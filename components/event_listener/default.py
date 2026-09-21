"""事件监听器：记录每次命令请求的真实发送者。

只做一件事，原因见 bilireminder/sender.py 的说明。
"""

from __future__ import annotations

import logging

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events

from bilireminder.sender import query_key

logger = logging.getLogger(__name__)


class DefaultEventListener(EventListener):
    async def initialize(self) -> None:
        await super().initialize()

        @self.handler(events.GroupCommandSent)
        async def on_group_command(event_context: context.EventContext) -> None:
            _remember(self.plugin, event_context)

        @self.handler(events.PersonCommandSent)
        async def on_person_command(event_context: context.EventContext) -> None:
            _remember(self.plugin, event_context)


def _remember(plugin, event_context: context.EventContext) -> None:
    sender_id = getattr(event_context.event, "sender_id", None)
    if sender_id is None:
        return
    plugin.remember_sender(query_key(event_context), str(sender_id))
