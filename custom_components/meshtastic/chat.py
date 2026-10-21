# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

"""Chat: message history per gateway and a websocket API used by the chat panel."""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .aiomeshtastic import MeshInterface
from .api import (
    ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID,
    ATTR_EVENT_MESHTASTIC_API_DATA,
    EVENT_MESHTASTIC_API_TEXT_MESSAGE,
    MeshtasticApiClientError,
)
from .const import (
    CONF_OPTION_CHAT,
    CONF_OPTION_CHAT_ENABLE,
    CONF_OPTION_CHAT_ENABLE_DEFAULT,
    CONF_OPTION_CHAT_HISTORY_SIZE,
    CONF_OPTION_CHAT_HISTORY_SIZE_DEFAULT,
    CONF_OPTION_CHAT_PREFIX_SENDER,
    CONF_OPTION_CHAT_PREFIX_SENDER_DEFAULT,
    CONF_OPTION_CHAT_REQUIRE_ADMIN,
    CONF_OPTION_CHAT_REQUIRE_ADMIN_DEFAULT,
    DOMAIN,
    EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI,
    LOGGER,
)
from .entity import GatewayChannelEntity, GatewayDirectMessageEntity

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.auth.models import User
    from homeassistant.config_entries import ConfigEntry

    from .data import MeshtasticConfigEntry

_LOGGER = LOGGER.getChild(__name__)

STORAGE_VERSION = 1
STORAGE_SAVE_DELAY = 10
MAX_TEXT_BYTES = 200
ACK_TIMEOUT = 60
DEDUPLICATE_WINDOW = 25

SIGNAL_CHAT_UPDATE = f"{DOMAIN}_chat_update"

DATA_CHAT: HassKey[dict[str, MeshtasticChat]] = HassKey(f"{DOMAIN}_chat")
DATA_CHAT_WEBSOCKET_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_chat_websocket")

CONVERSATION_CHANNEL = "channel"
CONVERSATION_NODE = "node"


class MessageStatus(StrEnum):
    RECEIVED = "received"
    SENDING = "sending"
    SENT = "sent"  # acknowledged by the mesh (e.g. rebroadcast heard), not necessarily by the recipient
    DELIVERED = "delivered"  # acknowledged by the recipient node
    NO_ACK = "no_ack"  # transmitted, but no acknowledgement arrived in time
    FAILED = "failed"


def chat_options(entry: ConfigEntry) -> Mapping[str, Any]:
    return entry.options.get(CONF_OPTION_CHAT, {})


def chat_enabled(entry: ConfigEntry) -> bool:
    return chat_options(entry).get(CONF_OPTION_CHAT_ENABLE, CONF_OPTION_CHAT_ENABLE_DEFAULT)


def chat_requires_admin(entry: ConfigEntry) -> bool:
    return chat_options(entry).get(CONF_OPTION_CHAT_REQUIRE_ADMIN, CONF_OPTION_CHAT_REQUIRE_ADMIN_DEFAULT)


def channel_conversation(index: int) -> str:
    return f"{CONVERSATION_CHANNEL}:{index}"


def node_conversation(node_id: int) -> str:
    return f"{CONVERSATION_NODE}:{node_id}"


def parse_conversation(conversation: str) -> tuple[str, int]:
    kind, _, value = conversation.partition(":")
    if kind not in (CONVERSATION_CHANNEL, CONVERSATION_NODE):
        msg = f"Invalid conversation {conversation}"
        raise vol.Invalid(msg)
    try:
        return kind, int(value)
    except ValueError as e:
        msg = f"Invalid conversation {conversation}"
        raise vol.Invalid(msg) from e


def short_user_name(name: str | None) -> str | None:
    """Format a Home Assistant user name as 'First L.'."""
    if not name:
        return None
    parts = name.split()
    if len(parts) >= 2:  # noqa: PLR2004
        return f"{parts[0]} {parts[-1][0]}."
    return parts[0]


def _is_public_channel(psk: str | None) -> bool:
    # no key, or a one byte "well known" key (e.g. the default AQ==) means anyone can read the channel
    if not psk:
        return True
    try:
        return len(base64.b64decode(psk)) <= 1
    except (binascii.Error, ValueError):
        return False


class MeshtasticChat:
    """Keeps the message history of one gateway and sends messages on behalf of the chat panel."""

    def __init__(self, hass: HomeAssistant, entry: MeshtasticConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.chat.{entry.entry_id}", private=True
        )
        self._conversations: dict[str, list[dict[str, Any]]] = {}
        self._history_size = int(
            chat_options(entry).get(CONF_OPTION_CHAT_HISTORY_SIZE, CONF_OPTION_CHAT_HISTORY_SIZE_DEFAULT)
        )
        self._unsubscribe = None
        self._pending: set[asyncio.Task] = set()

    @property
    def requires_admin(self) -> bool:
        return chat_requires_admin(self.entry)

    @property
    def prefix_sender(self) -> bool:
        return chat_options(self.entry).get(CONF_OPTION_CHAT_PREFIX_SENDER, CONF_OPTION_CHAT_PREFIX_SENDER_DEFAULT)

    @property
    def gateway_node(self) -> Mapping[str, Any]:
        return self.entry.runtime_data.gateway_node or {}

    @property
    def gateway_id(self) -> int | None:
        return self.gateway_node.get("num")

    async def async_setup(self) -> None:
        data = await self._store.async_load()
        if data:
            self._conversations = {k: list(v) for k, v in data.get("conversations", {}).items()}
            # messages that were still in flight when Home Assistant stopped will never be acknowledged
            for messages in self._conversations.values():
                for message in messages:
                    if message.get("status") == MessageStatus.SENDING:
                        message["status"] = MessageStatus.NO_ACK
        self._unsubscribe = self.hass.bus.async_listen(EVENT_MESHTASTIC_API_TEXT_MESSAGE, self._on_text_message)

    async def async_unload(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        for task in list(self._pending):
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        await self._store.async_save(self._data_to_save())

    @staticmethod
    async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORAGE_VERSION, f"{DOMAIN}.chat.{entry_id}", private=True).async_remove()

    def _data_to_save(self) -> dict[str, Any]:
        return {"conversations": self._conversations}

    def _node_names(self, node_id: int) -> tuple[str, str, str]:
        node = self.entry.runtime_data.client.get_node_info(node_id)
        user_id = f"!{node_id:08x}"
        if node is None:
            return f"Meshtastic {user_id[-4:]}", user_id[-4:], user_id
        return node.long_name or user_id, node.short_name or user_id[-4:], node.user_id or user_id

    def _publish(self, kind: str, conversation: str, message: dict[str, Any]) -> None:
        self._store.async_delay_save(self._data_to_save, STORAGE_SAVE_DELAY)
        async_dispatcher_send(
            self.hass,
            SIGNAL_CHAT_UPDATE,
            {"entry_id": self.entry.entry_id, "kind": kind, "conversation": conversation, "message": dict(message)},
        )

    def _append(self, conversation: str, message: dict[str, Any]) -> None:
        messages = self._conversations.setdefault(conversation, [])
        messages.append(message)
        if len(messages) > self._history_size:
            del messages[: len(messages) - self._history_size]
        self._publish("message", conversation, message)

    @callback
    def _on_text_message(self, event: Event) -> None:
        if event.data.get(ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID) != self.entry.entry_id:
            return
        data = event.data.get(ATTR_EVENT_MESHTASTIC_API_DATA)
        if not data:
            return

        from_id = data.get("from")
        to = data.get("to") or {}
        if from_id is None:
            return

        if to.get("node") is None and to.get("channel") is not None:
            conversation = channel_conversation(to["channel"])
        elif to.get("node") is not None:
            if self.gateway_id is not None and to["node"] != self.gateway_id:
                return
            conversation = node_conversation(from_id)
        else:
            return

        packet_id = event.data.get("message_id")
        if packet_id is not None:
            recent = self._conversations.get(conversation, [])[-DEDUPLICATE_WINDOW:]
            if any(m.get("packet_id") == packet_id and m.get("from") == from_id for m in recent):
                return

        long_name, short_name, user_id = self._node_names(from_id)
        self._append(
            conversation,
            {
                "id": uuid.uuid4().hex,
                "packet_id": packet_id,
                "ts": time.time(),
                "direction": "in",
                "from": from_id,
                "from_user_id": user_id,
                "from_name": long_name,
                "from_short": short_name,
                "text": data.get("message", ""),
                "status": MessageStatus.RECEIVED,
            },
        )

    def history(self, conversation: str, before: float | None = None, limit: int = 100) -> list[dict[str, Any]]:
        messages = self._conversations.get(conversation, [])
        if before is not None:
            messages = [m for m in messages if m["ts"] < before]
        return [dict(m) for m in messages[-limit:]]

    def clear(self, conversation: str) -> None:
        if self._conversations.pop(conversation, None) is not None:
            self._store.async_delay_save(self._data_to_save, STORAGE_SAVE_DELAY)
            async_dispatcher_send(
                self.hass,
                SIGNAL_CHAT_UPDATE,
                {"entry_id": self.entry.entry_id, "kind": "cleared", "conversation": conversation, "message": None},
            )

    def summaries(self) -> dict[str, dict[str, Any]]:
        result = {}
        for conversation, messages in self._conversations.items():
            if not messages:
                continue
            last = messages[-1]
            result[conversation] = {
                "ts": last["ts"],
                "text": last["text"],
                "direction": last["direction"],
                "from_short": last.get("from_short"),
                "count": len(messages),
            }
        return result

    async def async_targets(self) -> dict[str, list[dict[str, Any]]]:
        client = self.entry.runtime_data.client
        channels = []
        for channel in await client.async_get_channels():
            role = channel.get("role")
            if role == "DISABLED":
                continue
            settings = channel.get("settings", {})
            index = int(channel.get("index", 0))
            name = settings.get("name") or ("Primary" if role == "PRIMARY" else f"Channel {index}")
            channels.append(
                {
                    "index": index,
                    "name": name,
                    "primary": role == "PRIMARY",
                    "public": _is_public_channel(settings.get("psk")),
                    "conversation": channel_conversation(index),
                }
            )

        nodes = []
        for node_id, node in (await client.async_get_all_nodes()).items():
            if node_id == self.gateway_id:
                continue
            user = node.get("user", {})
            user_id = user.get("id") or f"!{node_id:08x}"
            nodes.append(
                {
                    "num": node_id,
                    "user_id": user_id,
                    "long_name": user.get("longName") or user_id,
                    "short_name": user.get("shortName") or user_id[-4:],
                    "last_heard": node.get("lastHeard"),
                    "hops_away": node.get("hopsAway"),
                    "snr": node.get("snr"),
                    "favorite": bool(node.get("isFavorite", False)),
                    "has_public_key": bool(user.get("publicKey")),
                    "conversation": node_conversation(node_id),
                }
            )
        return {"channels": channels, "nodes": nodes}

    def prefix_for(self, user: User | None) -> str | None:
        if not self.prefix_sender or user is None:
            return None
        return short_user_name(user.name)

    def compose(self, text: str, user: User | None) -> str:
        text = text.strip()
        if prefix := self.prefix_for(user):
            text = f"{prefix}: {text}"
        return text

    async def async_send(self, conversation: str, text: str, user: User | None = None) -> dict[str, Any]:
        kind, target = parse_conversation(conversation)
        full_text = self.compose(text, user)
        if not full_text:
            msg = "Message is empty"
            raise vol.Invalid(msg)
        if len(full_text.encode("utf-8")) > MAX_TEXT_BYTES:
            msg = f"Message is longer than {MAX_TEXT_BYTES} bytes"
            raise vol.Invalid(msg)

        if kind == CONVERSATION_CHANNEL:
            destination = MeshInterface.BROADCAST_NUM
            channel_index = target
        else:
            destination = target
            channel_index = None

        gateway_id = self.gateway_id or 0
        long_name, short_name, user_id = self._node_names(gateway_id)
        message = {
            "id": uuid.uuid4().hex,
            "packet_id": None,
            "ts": time.time(),
            "direction": "out",
            "from": gateway_id,
            "from_user_id": user_id,
            "from_name": long_name,
            "from_short": short_name,
            "to": target if kind == CONVERSATION_NODE else None,
            "text": full_text,
            "status": MessageStatus.SENDING,
            "sent_by": user.name if user else None,
        }
        self._append(conversation, message)

        task = self.hass.async_create_background_task(
            self._async_deliver(conversation, message, destination, channel_index),
            name=f"{DOMAIN}_chat_send_{message['id']}",
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return dict(message)

    async def _async_deliver(
        self, conversation: str, message: dict[str, Any], destination: int, channel_index: int | None
    ) -> None:
        client = self.entry.runtime_data.client
        try:
            ack_from, error = await client.send_text_with_ack(
                message["text"], destination, channel_index=channel_index, ack_timeout=ACK_TIMEOUT
            )
        except asyncio.CancelledError:
            message["status"] = MessageStatus.NO_ACK
            self._publish("update", conversation, message)
            raise
        except (MeshtasticApiClientError, ValueError) as e:
            _LOGGER.debug("Failed to send chat message", exc_info=True)
            message["status"] = MessageStatus.FAILED
            message["error"] = str(e) or type(e).__name__
            self._publish("update", conversation, message)
            return

        if error is None:
            message["status"] = MessageStatus.NO_ACK
        elif error != "NONE":
            message["status"] = MessageStatus.FAILED
            message["error"] = error
        elif channel_index is None and ack_from == destination:
            message["status"] = MessageStatus.DELIVERED
        else:
            message["status"] = MessageStatus.SENT
        self._publish("update", conversation, message)

        if message["status"] != MessageStatus.FAILED:
            self._log_sent_message(message, destination, channel_index)

    def _log_sent_message(self, message: dict[str, Any], destination: int, channel_index: int | None) -> None:
        """Record messages sent from the chat in the logbook, next to the received ones."""
        gateway_id = self.gateway_id
        if gateway_id is None:
            return
        entity_registry = er.async_get(self.hass)
        if channel_index is not None:
            unique_id = GatewayChannelEntity.build_unique_id(self.entry.entry_id, gateway_id, channel_index)
            from_name = f"{message['from_name']} ({message['from_user_id']})"
        else:
            unique_id = GatewayDirectMessageEntity.build_unique_id(self.entry.entry_id, gateway_id)
            to_long, _, to_user_id = self._node_names(destination)
            from_name = f"{message['from_name']} ({message['from_user_id']}) → {to_long} ({to_user_id})"
        entity_id = entity_registry.async_get_entity_id(DOMAIN, DOMAIN, unique_id)
        if entity_id is None:
            return
        device = dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, str(gateway_id))})
        self.hass.bus.async_fire(
            EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG,
            {
                ATTR_ENTITY_ID: entity_id,
                ATTR_DEVICE_ID: device.id if device else None,
                EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME: from_name,
                EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI: channel_index is None,
                EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE: message["text"],
            },
        )


async def async_setup_chat(hass: HomeAssistant, entry: MeshtasticConfigEntry) -> MeshtasticChat:
    chats = hass.data.setdefault(DATA_CHAT, {})
    if (existing := chats.pop(entry.entry_id, None)) is not None:
        await existing.async_unload()
    chat = MeshtasticChat(hass, entry)
    await chat.async_setup()
    chats[entry.entry_id] = chat
    _async_register_websocket_commands(hass)
    return chat


async def async_unload_chat(hass: HomeAssistant, entry: ConfigEntry) -> None:
    chats = hass.data.get(DATA_CHAT, {})
    if (chat := chats.pop(entry.entry_id, None)) is not None:
        await chat.async_unload()


def any_chat_loaded(hass: HomeAssistant) -> bool:
    return bool(hass.data.get(DATA_CHAT))


def all_chats_require_admin(hass: HomeAssistant) -> bool:
    return all(chat.requires_admin for chat in hass.data.get(DATA_CHAT, {}).values())


def _chats_for_user(hass: HomeAssistant, user: User) -> dict[str, MeshtasticChat]:
    return {
        entry_id: chat
        for entry_id, chat in hass.data.get(DATA_CHAT, {}).items()
        if user.is_admin or not chat.requires_admin
    }


def _get_chat(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> MeshtasticChat | None:
    chat = _chats_for_user(hass, connection.user).get(msg["entry_id"])
    if chat is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Chat not available for this gateway")
    return chat


@callback
def _async_register_websocket_commands(hass: HomeAssistant) -> None:
    if hass.data.get(DATA_CHAT_WEBSOCKET_REGISTERED):
        return
    hass.data[DATA_CHAT_WEBSOCKET_REGISTERED] = True
    websocket_api.async_register_command(hass, websocket_chat_gateways)
    websocket_api.async_register_command(hass, websocket_chat_history)
    websocket_api.async_register_command(hass, websocket_chat_send)
    websocket_api.async_register_command(hass, websocket_chat_clear)
    websocket_api.async_register_command(hass, websocket_chat_subscribe)


@websocket_api.websocket_command({vol.Required("type"): "meshtastic/chat/gateways"})
@websocket_api.async_response
async def websocket_chat_gateways(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    result = []
    for entry_id, chat in _chats_for_user(hass, connection.user).items():
        gateway = chat.gateway_node
        user = gateway.get("user", {})
        try:
            targets = await chat.async_targets()
            available = True
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to load chat targets", exc_info=True)
            targets = {"channels": [], "nodes": []}
            available = False
        result.append(
            {
                "entry_id": entry_id,
                "title": chat.entry.title,
                "available": available,
                "gateway": {
                    "num": chat.gateway_id,
                    "user_id": user.get("id"),
                    "long_name": user.get("longName"),
                    "short_name": user.get("shortName"),
                },
                "max_bytes": MAX_TEXT_BYTES,
                "prefix": chat.prefix_for(connection.user),
                "can_clear": connection.user.is_admin,
                "conversations": chat.summaries(),
                **targets,
            }
        )
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshtastic/chat/history",
        vol.Required("entry_id"): str,
        vol.Required("conversation"): str,
        vol.Optional("before"): vol.Coerce(float),
        vol.Optional("limit", default=100): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
    }
)
@callback
def websocket_chat_history(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], chat.history(msg["conversation"], msg.get("before"), msg["limit"]))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshtastic/chat/send",
        vol.Required("entry_id"): str,
        vol.Required("conversation"): str,
        vol.Required("text"): str,
    }
)
@websocket_api.async_response
async def websocket_chat_send(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    try:
        message = await chat.async_send(msg["conversation"], msg["text"], connection.user)
    except vol.Invalid as e:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(e))
        return
    connection.send_result(msg["id"], message)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshtastic/chat/clear",
        vol.Required("entry_id"): str,
        vol.Required("conversation"): str,
    }
)
@websocket_api.require_admin
@callback
def websocket_chat_clear(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (chat := _get_chat(hass, connection, msg)) is None:
        return
    chat.clear(msg["conversation"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "meshtastic/chat/subscribe"})
@callback
def websocket_chat_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    user = connection.user

    @callback
    def _forward(update: dict[str, Any]) -> None:
        if update["entry_id"] not in _chats_for_user(hass, user):
            return
        connection.send_message(websocket_api.event_message(msg["id"], update))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_CHAT_UPDATE, _forward)
    connection.send_result(msg["id"])
