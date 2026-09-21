# BilibiliReminder

A LangBot plugin that subscribes to Bilibili live rooms and pushes a reminder to the chat when a streamer goes live.

Built for the LangBot 4.x plugin system. Based on [Hanschase/BreminderPlugin](https://github.com/Hanschase/BreminderPlugin).

## Features

- Subscribe to any Bilibili live room by room ID (short IDs and room URLs work too).
- Works in both group chats and direct messages. In group chats the reminder `@`s everyone who subscribed to that room.
- Per-user subscriptions: each member of a group keeps their own list.
- One batched request per polling round, no matter how many rooms are subscribed.
- Subscriptions are kept in LangBot plugin storage, scoped to the current installation and workspace.

## Installation

Install it from the LangBot plugin marketplace, or see the [plugin installation guide](https://langbot.app/docs/zh/plugin/plugin-intro).

## Usage

All commands live under the `sb` command (`!` is the default command prefix):

| Command | Description |
| --- | --- |
| `!sb` | Show usage |
| `!sb sub <room_id>` | Subscribe to a live room |
| `!sb unsub <room_id>` | Unsubscribe from a live room |
| `!sb rooms` | List the rooms you subscribed to in this chat |
| `!sb status` | Show the current live status of the rooms you subscribed to |

Example: `!sb sub 21452505`

The plugin starts polling automatically when it loads — no command is needed to start it.

## Configuration

Configure these in the LangBot plugin management page:

| Option | Default | Description |
| --- | --- | --- |
| Check interval (seconds) | `60` | How often live status is polled. Values below 15 are clamped to 15. |
| Fallback cover image | empty | Used when the streamer has not set a room cover. Leave empty to send the reminder without an image. |
| Notify admin on failure | `false` | Send a DM to the admin when pushing a reminder fails. |
| Admin user ID | empty | Receiver of those failure notifications. |

## Upgrading from 0.1.x

Version 0.2.0 targets the LangBot 4.x plugin system and is not compatible with the old data file:

- Commands changed: `!apply` / `!cancel` / `!rooms` / `!startrem` are replaced by `!sb sub` / `!sb unsub` / `!sb rooms`; polling now starts by itself.
- Subscriptions are no longer stored in `subscription.json`; the old file is not imported, so subscriptions have to be created again.

## Notes

- All reply texts are in Chinese, and deliberately rude — that is the original plugin's voice.
- The plugin only reads public Bilibili live room information and does not require a Bilibili account.
