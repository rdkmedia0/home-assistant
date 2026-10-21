# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshtastic.chat import DATA_CHAT
from custom_components.meshtastic.const import DOMAIN
from custom_components.meshtastic.frontend import CHAT_PANEL_URL_PATH

from .test_chat import ANNA, BROADCAST, GATEWAY, FakeClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class FakeApiClient(FakeClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        super().__init__()
        self.metadata = {}

    async def connect(self) -> None:
        return

    async def disconnect(self) -> None:
        # yield like a real connection does
        await asyncio.sleep(0)

    def get_own_node(self) -> dict[str, Any]:
        return {"num": GATEWAY, "user": {"id": "!00004904", "longName": "Home Gateway", "shortName": "4904"}}

    async def async_get_own_node(self) -> dict[str, Any]:
        return self.get_own_node()

    async def async_get_node_local_config(self) -> dict[str, Any]:
        return {}

    async def async_get_node_module_config(self) -> dict[str, Any]:
        return {}


def _entry(options: dict[str, Any]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Home Gateway",
        version=1,
        minor_version=2,
        data={"connection_type": "tcp", "tcp_host": "gateway.local", "tcp_port": 4403},
        options={
            "nodes": [],
            "notify_platform": {"channels": False, "nodes": "none"},
            "web_client": {"enable": False},
            "tcp_proxy": {"enable": False},
            **options,
        },
    )


@pytest.fixture
def fake_api():
    with (
        patch("custom_components.meshtastic.MeshtasticApiClient", FakeApiClient),
        patch("custom_components.meshtastic.fetch_meshtastic_hardware_names", AsyncMock(return_value={})),
        patch("custom_components.meshtastic.frontend._async_register_static_path", AsyncMock()),
    ):
        yield


@pytest.mark.usefixtures("fake_api")
async def test_chat_panel_follows_options(hass: HomeAssistant) -> None:
    entry = _entry({"chat": {"enable": True, "require_admin": False}})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert entry.entry_id in hass.data[DATA_CHAT]
    panel = hass.data["frontend_panels"][CHAT_PANEL_URL_PATH]
    assert panel.require_admin is False

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data.get(DATA_CHAT, {})
    assert CHAT_PANEL_URL_PATH not in hass.data.get("frontend_panels", {})


@pytest.mark.usefixtures("fake_api")
async def test_chat_panel_admin_only_while_any_gateway_requires_it(hass: HomeAssistant) -> None:
    open_entry = _entry({"chat": {"enable": True, "require_admin": False}})
    admin_entry = _entry({"chat": {"enable": True, "require_admin": True}})
    open_entry.add_to_hass(hass)
    admin_entry.add_to_hass(hass)

    # setting up the domain sets up both gateways
    assert await hass.config_entries.async_setup(open_entry.entry_id)
    await hass.async_block_till_done()
    assert set(hass.data[DATA_CHAT]) == {open_entry.entry_id, admin_entry.entry_id}
    # the panel is visible to non admins as long as one gateway allows it; the API filters per gateway
    assert hass.data["frontend_panels"][CHAT_PANEL_URL_PATH].require_admin is False

    assert await hass.config_entries.async_unload(open_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data["frontend_panels"][CHAT_PANEL_URL_PATH].require_admin is True

    assert await hass.config_entries.async_unload(admin_entry.entry_id)
    await hass.async_block_till_done()
    assert CHAT_PANEL_URL_PATH not in hass.data.get("frontend_panels", {})


@pytest.mark.usefixtures("fake_api")
async def test_chat_disabled_by_default(hass: HomeAssistant) -> None:
    entry = _entry({})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.data.get(DATA_CHAT)
    assert CHAT_PANEL_URL_PATH not in hass.data.get("frontend_panels", {})

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("fake_api")
async def test_options_flow_enables_chat(hass: HomeAssistant) -> None:
    entry = _entry({})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    assert "chat" in result["data_schema"].schema

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "nodes": [],
            "notify_platform": {"channels": False, "nodes": "none"},
            "web_client": {"enable": False},
            "tcp_proxy": {"enable": False, "port": 4403},
            "chat": {"enable": True, "require_admin": True, "prefix_sender": True, "history_size": 50},
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    assert entry.options["chat"] == {"enable": True, "require_admin": True, "prefix_sender": True, "history_size": 50}
    assert entry.entry_id in hass.data[DATA_CHAT]
    assert CHAT_PANEL_URL_PATH in hass.data["frontend_panels"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("fake_api")
async def test_send_text_action_is_kept_in_chat_history(hass: HomeAssistant) -> None:
    entry = _entry({"chat": {"enable": True}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = entry.runtime_data.client
    chat = hass.data[DATA_CHAT][entry.entry_id]

    await hass.services.async_call(
        DOMAIN, "send_text", {"text": "hello", "channel": "1", "sent_by": "Telegram: Alice"}, blocking=True
    )
    await hass.async_block_till_done()
    message = chat.history("channel:1")[-1]
    assert message["direction"] == "out"
    assert message["text"] == "hello"
    assert message["sent_by"] == "Telegram: Alice"
    assert client.sent[-1] == {"text": "hello", "to": BROADCAST, "channel": 1, "timeout": 60}

    await hass.services.async_call(DOMAIN, "send_text", {"text": "direct", "to": str(ANNA)}, blocking=True)
    await hass.async_block_till_done()
    assert chat.history(f"node:{ANNA}")[-1]["sent_by"] is None
    assert client.sent[-1]["to"] == ANNA
    assert client.sent[-1]["channel"] is None

    # a direct message on an explicit channel keeps that channel
    await hass.services.async_call(
        DOMAIN, "send_text", {"text": "direct on 2", "to": str(ANNA), "channel": "2"}, blocking=True
    )
    await hass.async_block_till_done()
    assert client.sent[-1] == {"text": "direct on 2", "to": ANNA, "channel": 2, "timeout": 60}
    assert chat.history(f"node:{ANNA}")[-1]["text"] == "direct on 2"

    # without a channel the primary channel is used
    await hass.services.async_call(DOMAIN, "send_text", {"text": "primary"}, blocking=True)
    await hass.async_block_till_done()
    assert chat.history("channel:0")[-1]["text"] == "primary"

    with pytest.raises(ServiceValidationError, match="longer than"):
        await hass.services.async_call(DOMAIN, "send_text", {"text": "x" * 201}, blocking=True)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("fake_api")
async def test_send_text_action_without_chat(hass: HomeAssistant) -> None:
    entry = _entry({})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = entry.runtime_data.client

    await hass.services.async_call(DOMAIN, "send_text", {"text": "hello", "channel": "1", "ack": True}, blocking=True)
    assert client.sent[-1] == {"text": "hello", "to": "^all", "channel": 1, "ack": True}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
