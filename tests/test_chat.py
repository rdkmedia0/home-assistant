# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshtastic.aiomeshtastic.interface import MeshNode
from custom_components.meshtastic.api import EVENT_MESHTASTIC_API_TEXT_MESSAGE, MeshtasticApiClientError
from custom_components.meshtastic.chat import (
    DATA_CHAT,
    MAX_TEXT_BYTES,
    MeshtasticChat,
    MessageStatus,
    async_setup_chat,
    async_unload_chat,
    short_user_name,
)
from custom_components.meshtastic.const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

GATEWAY = 0x4904
ANNA = 0xA1B2C3D4
BROADCAST = 0xFFFFFFFF


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.ack: tuple[int | None, str | None] | Exception = (GATEWAY, "NONE")

    def get_node_info(self, node_id: int) -> MeshNode | None:
        if node_id == ANNA:
            return MeshNode(id=ANNA, user_id="!a1b2c3d4", short_name="ANNA", long_name="Anna Hiking")
        if node_id == GATEWAY:
            return MeshNode(id=GATEWAY, user_id="!00004904", short_name="4904", long_name="Home Gateway")
        return None

    async def send_text_with_ack(
        self, text: str, destination_id: int, *, channel_index: int | None = None, ack_timeout: float = 60
    ) -> tuple[int | None, str | None]:
        self.sent.append({"text": text, "to": destination_id, "channel": channel_index, "timeout": ack_timeout})
        if isinstance(self.ack, Exception):
            raise self.ack
        return self.ack

    async def send_text(
        self,
        text: str,
        destination_id: int | str = BROADCAST,
        *,
        channel_index: int | None = None,
        want_ack: bool = False,
    ) -> None:
        self.sent.append({"text": text, "to": destination_id, "channel": channel_index, "ack": want_ack})

    async def async_get_channels(self) -> list[dict[str, Any]]:
        def channel(index: int, role: str, name: str, psk: str) -> dict[str, Any]:
            settings = {"name": name, "psk": psk, "uplinkEnabled": False, "downlinkEnabled": False}
            return {"index": index, "role": role, "settings": settings}

        return [
            channel(0, "PRIMARY", "", "AQ=="),
            channel(1, "SECONDARY", "SOSPrivate", "c2VjcmV0c2VjcmV0c2VjcmV0MTI="),
            channel(2, "DISABLED", "", ""),
        ]

    async def async_get_all_nodes(self) -> dict[int, dict[str, Any]]:
        return {
            GATEWAY: {"num": GATEWAY, "user": {"id": "!00004904", "longName": "Home Gateway", "shortName": "4904"}},
            ANNA: {
                "num": ANNA,
                "user": {"id": "!a1b2c3d4", "longName": "Anna Hiking", "shortName": "ANNA", "publicKey": "abc"},
                "lastHeard": 1700000000,
                "hopsAway": 1,
                "isFavorite": True,
            },
        }


def _make_entry(hass: HomeAssistant, client: FakeClient, **chat_options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home Gateway",
        data={},
        options={"chat": {"enable": True, **chat_options}},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        client=client,
        gateway_node={"num": GATEWAY, "user": {"id": "!00004904", "longName": "Home Gateway", "shortName": "4904"}},
    )
    return entry


def _fire_text(  # noqa: PLR0913
    hass: HomeAssistant, entry: MockConfigEntry, *, to_node=None, channel=None, message_id=1, text="hi"
) -> None:
    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE,
        {
            "config_entry_id": entry.entry_id,
            "node": ANNA,
            "data": {
                "from": ANNA,
                "to": {"node": to_node, "channel": channel},
                "gateway": GATEWAY,
                "message": text,
            },
            "message_id": message_id,
        },
    )


@pytest.fixture
async def chat_setup(hass: HomeAssistant):
    client = FakeClient()
    entry = _make_entry(hass, client)
    chat = await async_setup_chat(hass, entry)
    yield entry, chat, client
    await async_unload_chat(hass, entry)


def test_short_user_name() -> None:
    assert short_user_name("Steve Smith") == "Steve S."
    assert short_user_name("Anna Maria Muster") == "Anna M."
    assert short_user_name("Steve") == "Steve"
    assert short_user_name(None) is None


async def test_receive_channel_and_direct_messages(hass: HomeAssistant, chat_setup) -> None:
    entry, chat, _ = chat_setup

    _fire_text(hass, entry, channel=1, message_id=10, text="on channel")
    _fire_text(hass, entry, channel=1, message_id=10, text="on channel")  # duplicate
    _fire_text(hass, entry, to_node=GATEWAY, message_id=11, text="direct")
    _fire_text(hass, entry, to_node=0x12345678, message_id=12, text="not for us")
    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE,
        {"config_entry_id": "other", "data": {"from": ANNA, "to": {"channel": 1}, "message": "x"}},
    )
    await hass.async_block_till_done()

    channel = chat.history("channel:1")
    assert [m["text"] for m in channel] == ["on channel"]
    assert channel[0]["from_name"] == "Anna Hiking"
    assert channel[0]["from_short"] == "ANNA"
    assert channel[0]["direction"] == "in"

    direct = chat.history(f"node:{ANNA}")
    assert [m["text"] for m in direct] == ["direct"]
    assert set(chat.summaries()) == {"channel:1", f"node:{ANNA}"}


@pytest.mark.parametrize(
    ("conversation", "ack", "expected_status", "expected_to", "expected_channel"),
    [
        ("channel:1", (GATEWAY, "NONE"), MessageStatus.SENT, BROADCAST, 1),
        (f"node:{ANNA}", (ANNA, "NONE"), MessageStatus.DELIVERED, ANNA, None),
        (f"node:{ANNA}", (GATEWAY, "NONE"), MessageStatus.SENT, ANNA, None),
        (f"node:{ANNA}", (GATEWAY, "MAX_RETRANSMIT"), MessageStatus.FAILED, ANNA, None),
        (f"node:{ANNA}", (None, None), MessageStatus.NO_ACK, ANNA, None),
        ("channel:1", MeshtasticApiClientError("boom"), MessageStatus.FAILED, BROADCAST, 1),
    ],
)
async def test_send_status(  # noqa: PLR0913
    hass: HomeAssistant, chat_setup, conversation, ack, expected_status, expected_to, expected_channel
) -> None:
    _, chat, client = chat_setup
    client.ack = ack

    message = await chat.async_send(conversation, "  hello  ")
    assert message["direction"] == "out"
    await hass.async_block_till_done()

    stored = chat.history(conversation)[-1]
    assert stored["id"] == message["id"]
    assert stored["status"] == expected_status
    assert client.sent[-1]["text"] == "hello"
    assert client.sent[-1]["to"] == expected_to
    assert client.sent[-1]["channel"] == expected_channel


async def test_send_on_behalf_of_someone_else(hass: HomeAssistant, chat_setup) -> None:
    _, chat, client = chat_setup

    await chat.async_send("channel:1", "hello from telegram", sent_by="Telegram: Alice")
    await hass.async_block_till_done()
    assert client.sent[-1]["text"] == "hello from telegram"
    assert chat.history("channel:1")[-1]["sent_by"] == "Telegram: Alice"


async def test_send_prefix_and_limits(hass: HomeAssistant) -> None:
    client = FakeClient()
    entry = _make_entry(hass, client, prefix_sender=True)
    chat = await async_setup_chat(hass, entry)
    user = SimpleNamespace(name="Steve Smith", is_admin=True)

    await chat.async_send("channel:1", "on my way", user)
    await hass.async_block_till_done()
    assert client.sent[-1]["text"] == "Steve S.: on my way"
    assert chat.history("channel:1")[-1]["sent_by"] == "Steve Smith"

    with pytest.raises(vol.Invalid, match="longer than"):
        await chat.async_send("channel:1", "x" * MAX_TEXT_BYTES, user)
    with pytest.raises(vol.Invalid, match="empty"):
        await chat.async_send("channel:1", "   ")
    with pytest.raises(vol.Invalid, match="Invalid conversation"):
        await chat.async_send("bogus:1", "hello")

    await async_unload_chat(hass, entry)


async def test_history_is_persisted_and_trimmed(hass: HomeAssistant, hass_storage) -> None:
    client = FakeClient()
    entry = _make_entry(hass, client, history_size=10)
    await async_setup_chat(hass, entry)
    for i in range(15):
        _fire_text(hass, entry, channel=1, message_id=100 + i, text=f"m{i}")
    await hass.async_block_till_done()
    await async_unload_chat(hass, entry)

    stored = hass_storage[f"{DOMAIN}.chat.{entry.entry_id}"]["data"]["conversations"]["channel:1"]
    assert [m["text"] for m in stored] == [f"m{i}" for i in range(5, 15)]

    chat = await async_setup_chat(hass, entry)
    assert [m["text"] for m in chat.history("channel:1", limit=3)] == ["m12", "m13", "m14"]
    await async_unload_chat(hass, entry)

    await MeshtasticChat.async_remove_store(hass, entry.entry_id)
    assert f"{DOMAIN}.chat.{entry.entry_id}" not in hass_storage


async def test_pending_send_is_settled_on_unload(hass: HomeAssistant) -> None:
    client = FakeClient()
    entry = _make_entry(hass, client)
    chat = await async_setup_chat(hass, entry)
    started = asyncio.Event()

    async def never_acked(*args: Any, **kwargs: Any) -> tuple[int | None, str | None]:  # noqa: ARG001
        started.set()
        await asyncio.sleep(3600)
        return None, None

    client.send_text_with_ack = never_acked
    await chat.async_send("channel:1", "hello")
    await started.wait()
    await async_unload_chat(hass, entry)
    assert chat.history("channel:1")[-1]["status"] == MessageStatus.NO_ACK


async def test_websocket_api(hass: HomeAssistant, hass_ws_client, chat_setup) -> None:
    entry, _, client = chat_setup
    ws = await hass_ws_client(hass)

    await ws.send_json({"id": 1, "type": "meshtastic/chat/subscribe"})
    assert (await ws.receive_json())["success"]

    await ws.send_json({"id": 2, "type": "meshtastic/chat/gateways"})
    response = await ws.receive_json()
    assert response["success"]
    gateway = response["result"][0]
    assert gateway["entry_id"] == entry.entry_id
    assert gateway["gateway"]["long_name"] == "Home Gateway"
    assert [(c["index"], c["name"], c["public"]) for c in gateway["channels"]] == [
        (0, "Primary", True),
        (1, "SOSPrivate", False),
    ]
    assert [n["num"] for n in gateway["nodes"]] == [ANNA]
    assert gateway["nodes"][0]["favorite"] is True
    assert gateway["max_bytes"] == MAX_TEXT_BYTES

    _fire_text(hass, entry, channel=1, message_id=5, text="hello from anna")
    event = await ws.receive_json()
    assert event["id"] == 1
    assert event["event"]["kind"] == "message"
    assert event["event"]["conversation"] == "channel:1"
    assert event["event"]["message"]["text"] == "hello from anna"

    await ws.send_json(
        {"id": 3, "type": "meshtastic/chat/send", "entry_id": entry.entry_id, "conversation": "channel:1", "text": "hi"}
    )
    messages = [await ws.receive_json() for _ in range(3)]
    result = next(m for m in messages if m.get("id") == 3)
    assert result["success"]
    kinds = [m["event"]["kind"] for m in messages if m.get("type") == "event"]
    assert kinds == ["message", "update"]
    assert client.sent[-1]["text"] == "hi"

    await ws.send_json(
        {"id": 4, "type": "meshtastic/chat/history", "entry_id": entry.entry_id, "conversation": "channel:1"}
    )
    response = await ws.receive_json()
    assert [m["text"] for m in response["result"]] == ["hello from anna", "hi"]

    await ws.send_json(
        {"id": 5, "type": "meshtastic/chat/send", "entry_id": entry.entry_id, "conversation": "channel:1", "text": ""}
    )
    response = await ws.receive_json()
    assert not response["success"]

    await ws.send_json(
        {"id": 6, "type": "meshtastic/chat/clear", "entry_id": entry.entry_id, "conversation": "channel:1"}
    )
    messages = [await ws.receive_json() for _ in range(2)]
    assert next(m for m in messages if m.get("id") == 6)["success"]
    assert hass.data[DATA_CHAT][entry.entry_id].history("channel:1") == []


async def test_websocket_requires_admin(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token, chat_setup
) -> None:
    entry, _, _ = chat_setup
    ws = await hass_ws_client(hass, hass_read_only_access_token)

    await ws.send_json({"id": 1, "type": "meshtastic/chat/gateways"})
    assert (await ws.receive_json())["result"] == []

    await ws.send_json(
        {"id": 2, "type": "meshtastic/chat/send", "entry_id": entry.entry_id, "conversation": "channel:1", "text": "x"}
    )
    response = await ws.receive_json()
    assert not response["success"]
    assert response["error"]["code"] == "not_found"


async def test_websocket_non_admin_allowed(hass: HomeAssistant, hass_ws_client, hass_read_only_access_token) -> None:
    client = FakeClient()
    entry = _make_entry(hass, client, require_admin=False)
    await async_setup_chat(hass, entry)
    ws = await hass_ws_client(hass, hass_read_only_access_token)

    await ws.send_json({"id": 1, "type": "meshtastic/chat/gateways"})
    result = (await ws.receive_json())["result"]
    assert [g["entry_id"] for g in result] == [entry.entry_id]
    assert result[0]["can_clear"] is False

    await ws.send_json(
        {"id": 2, "type": "meshtastic/chat/clear", "entry_id": entry.entry_id, "conversation": "channel:1"}
    )
    response = await ws.receive_json()
    assert response["error"]["code"] == "unauthorized"
    await async_unload_chat(hass, entry)
