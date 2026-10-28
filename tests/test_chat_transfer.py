# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

"""File transfers between two gateways in one Home Assistant, on a fake mesh that can drop and refuse packets."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.meshtastic import chat_files
from custom_components.meshtastic.aiomeshtastic.interface import MeshNode
from custom_components.meshtastic.chat import async_setup_chat, async_unload_chat
from custom_components.meshtastic.chat_files import Data, PacketType
from custom_components.meshtastic.chat_transfer import (
    FileStatus,
    Timing,
    estimate_seconds,
    safe_file_name,
)
from custom_components.meshtastic.const import (
    DOMAIN,
    EVENT_MESHTASTIC_CHAT_FILE_RECEIVED,
    EVENT_MESHTASTIC_CHAT_FILE_SENT,
)

from .test_chat import ANNA, GATEWAY, FakeClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from custom_components.meshtastic.chat import MeshtasticChat

NOTE = "".join(f'<trkpt lat="{47 + i / 1000:.6f}" lon="{8 + i / 997:.6f}"/>\n' for i in range(400)).encode()
PHOTO = os.urandom(3000)
A_TO_B = f"node:{ANNA}"
B_TO_A = f"node:{GATEWAY}"

FAST = Timing(
    offer_timeout=1.0,
    routing_ack_timeout=1.0,
    per_hop=0,
    app_ack_timeout=0.5,
    done_timeout=0.5,
    link_retry_delay=0.01,
    link_retries=3,
    ack_misses=3,
    duty_cycle_backoff=(0.01,),
    chunk_gap_factor=0,
    utilization_recheck=0.01,
    utilization_max_wait=0.05,
    receiver_ack_delay=0.05,
)

type Predicate = Callable[[int, int, bytes], bool]


class FakeMesh:
    """Two radios and the air between them."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.clients: dict[int, MeshFakeClient] = {}
        self.drop_once: list[Predicate] = []  # the radio acknowledges, the other Home Assistant never sees it
        self.hold: Predicate | None = None  # like drop_once, but for as long as it is set
        self.routing: list[str | None] = []  # routing results handed to the next sends, in order
        self.log: list[tuple[int, int, bytes]] = []

    async def deliver(self, from_id: int, to_id: int, payload: bytes) -> tuple[int | None, str | None]:
        self.log.append((from_id, to_id, payload))
        if self.routing:
            result = self.routing.pop(0)
            if result != "NONE":
                return (None, None) if result is None else (to_id, result)
        for predicate in list(self.drop_once):
            if predicate(from_id, to_id, payload):
                self.drop_once.remove(predicate)
                return to_id, "NONE"
        if self.hold is not None and self.hold(from_id, to_id, payload):
            return to_id, "NONE"
        client = self.clients.get(to_id)
        if client is None:
            return to_id, "NONE"  # a radio without Mesh Chat behind it still acknowledges
        packet = SimpleNamespace(from_id=from_id, to_id=to_id, app_payload=bytes(payload))
        for listener in list(client.listeners):
            self.hass.async_create_task(listener(MeshNode.stub_node(from_id), packet))
        return to_id, "NONE"


class MeshFakeClient(FakeClient):
    def __init__(self, node_id: int, mesh: FakeMesh) -> None:
        super().__init__()
        self.node_id = node_id
        self.mesh = mesh
        self.listeners: list[Any] = []
        self.data_sent: list[tuple[int, bytes]] = []
        mesh.clients[node_id] = self

    def add_data_listener(self, port_num: int, callback: Any) -> Callable[[], None]:  # noqa: ARG002
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    async def send_data_with_ack(  # noqa: PLR0913
        self,
        payload: bytes,
        destination_id: int,
        *,
        port_num: int = 256,  # noqa: ARG002
        channel_index: int | None = None,  # noqa: ARG002
        ack_timeout: float = 60,  # noqa: ARG002
        packet_id: int | None = None,  # noqa: ARG002
    ) -> tuple[int | None, str | None]:
        self.data_sent.append((destination_id, bytes(payload)))
        return await self.mesh.deliver(self.node_id, destination_id, payload)

    def chunks_sent(self) -> list[int]:
        return [
            Data.decode(payload).seq
            for _, payload in self.data_sent
            if chat_files.packet_type(payload) == PacketType.DATA
        ]


def _entry(
    hass: HomeAssistant, client: MeshFakeClient, node_id: int, title: str, **file_options: Any
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={},
        options={"chat": {"enable": True, "files_enable": True, **file_options}},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        client=client, gateway_node={"num": node_id, "user": {"id": f"!{node_id:08x}", "longName": title}}
    )
    return entry


class Gateway:
    def __init__(self, entry: MockConfigEntry, chat: MeshtasticChat, client: MeshFakeClient) -> None:
        self.entry = entry
        self.chat = chat
        self.client = client

    @property
    def files(self) -> Any:
        assert self.chat.files is not None
        return self.chat.files

    def message(self, conversation: str, message_id: str) -> dict[str, Any]:
        message = self.chat.find_message(conversation, message_id=message_id)
        assert message is not None
        return message

    def file_message(self, conversation: str) -> dict[str, Any] | None:
        return next((m for m in self.chat.history(conversation) if m.get("kind") == "file"), None)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, client: MeshFakeClient) -> Gateway:
    chat = await async_setup_chat(hass, entry)
    assert chat.files is not None
    chat.files.timing = FAST
    return Gateway(entry, chat, client)


@pytest.fixture
async def mesh(hass: HomeAssistant, tmp_path: Path):
    hass.config.config_dir = str(tmp_path)
    fake = FakeMesh(hass)
    yield fake
    for client in list(fake.clients.values()):
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.runtime_data.client is client:
                await async_unload_chat(hass, entry)


async def _two_gateways(hass: HomeAssistant, mesh: FakeMesh, **b_options: Any) -> tuple[Gateway, Gateway]:
    a = await _setup(hass, _entry(hass, MeshFakeClient(GATEWAY, mesh), GATEWAY, "Home Gateway"), mesh.clients[GATEWAY])
    b = await _setup(
        hass, _entry(hass, MeshFakeClient(ANNA, mesh), ANNA, "Anna Hiking", **b_options), mesh.clients[ANNA]
    )
    return a, b


async def until(predicate: Callable[[], bool], patience: float = 5) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + patience
    while not predicate():
        if loop.time() > deadline:
            msg = "Timed out waiting"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


async def _wait_status(gateway: Gateway, conversation: str, message_id: str, *statuses: str) -> dict[str, Any]:
    await until(lambda: gateway.message(conversation, message_id)["status"] in statuses)
    await asyncio.sleep(0.05)  # let the other side finish its bookkeeping
    return gateway.message(conversation, message_id)


def _is_data(seq: int) -> Predicate:
    def predicate(_from: int, _to: int, payload: bytes) -> bool:
        return chat_files.packet_type(payload) == PacketType.DATA and Data.decode(payload).seq == seq

    return predicate


def _is_type(kind: PacketType) -> Predicate:
    return lambda _from, _to, payload: chat_files.packet_type(payload) == kind


# --- happy path ----------------------------------------------------------------------------------------------------


async def test_a_file_travels_between_two_gateways(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a, b = await _two_gateways(hass, mesh)
    sent_events = async_capture_events(hass, EVENT_MESHTASTIC_CHAT_FILE_SENT)
    received_events = async_capture_events(hass, EVENT_MESHTASTIC_CHAT_FILE_RECEIVED)

    message = await a.files.async_send(A_TO_B, NOTE, name="trail.gpx", mime="application/gpx+xml")
    assert message["kind"] == "file"
    assert message["status"] == FileStatus.OFFERING
    assert message["text"] == "\U0001f4ce trail.gpx"
    file = message["file"]
    assert (file["name"], file["size"], file["compressed"], file["image"]) == ("trail.gpx", len(NOTE), True, False)
    assert file["chunk_count"] == 13
    assert file["url"] == f"/api/meshtastic/chat/files/{a.entry.entry_id}/{file['transfer_id']}"

    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert done["status"] == FileStatus.DELIVERED, done["file"]
    assert done["file"]["chunks_done"] == 13
    assert done["file"]["error"] is None
    assert done["file"]["estimate_seconds"] == estimate_seconds(13, "LONG_FAST", 0)

    # stop-and-wait: every chunk once, in order, each acknowledged before the next
    assert a.client.chunks_sent() == list(range(13))
    acks = [p for _, p in b.client.data_sent if chat_files.packet_type(p) == PacketType.ACK]
    assert len(acks) == 13

    received = b.file_message(B_TO_A)
    assert received is not None
    assert received["status"] == FileStatus.RECEIVED
    assert received["direction"] == "in"
    assert received["from"] == GATEWAY
    assert received["file"]["size"] == len(NOTE)
    assert received["file"]["url"] == f"/api/meshtastic/chat/files/{b.entry.entry_id}/{file['transfer_id']}"
    assert Path(received["file"]["path"]).read_bytes() == NOTE
    assert (
        Path(received["file"]["path"]).parent
        == Path(hass.config.config_dir) / ".storage/meshtastic.files" / b.entry.entry_id
    )
    assert Path(done["file"]["path"]).read_bytes() == NOTE

    assert [(e.data["to"], e.data["status"], e.data["name"]) for e in sent_events] == [(ANNA, "delivered", "trail.gpx")]
    assert [(e.data["from"], e.data["size"], e.data["url"]) for e in received_events] == [
        (GATEWAY, len(NOTE), received["file"]["url"])
    ]
    assert not a.files._outgoing  # noqa: SLF001
    assert not b.files._incoming  # noqa: SLF001


async def test_dropped_chunk_and_lost_ack_are_repaired(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a, b = await _two_gateways(hass, mesh)
    mesh.drop_once.append(_is_data(3))  # the radio got it, Anna's Home Assistant did not
    mesh.drop_once.append(_is_type(PacketType.ACK))  # and one acknowledgement vanishes

    message = await a.files.async_send(A_TO_B, NOTE, name="trail.gpx")
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert done["status"] == FileStatus.DELIVERED, done["file"]

    sent = a.client.chunks_sent()
    assert sent.count(3) == 2
    assert sorted(set(sent)) == list(range(13))
    assert Path(b.file_message(B_TO_A)["file"]["path"]).read_bytes() == NOTE


async def test_window_of_four_resends_only_the_missing_chunk(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a, b = await _two_gateways(hass, mesh, files_window=4)
    mesh.drop_once.append(_is_data(5))

    message = await a.files.async_send(A_TO_B, NOTE, name="trail.gpx")
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert done["status"] == FileStatus.DELIVERED, done["file"]

    sent = a.client.chunks_sent()
    assert sent[:8] == [0, 1, 2, 3, 4, 5, 6, 7]
    assert sent.count(5) == 2
    assert len(sent) == 14
    assert Path(b.file_message(B_TO_A)["file"]["path"]).read_bytes() == NOTE
    acks = [p for _, p in b.client.data_sent if chat_files.packet_type(p) == PacketType.ACK]
    assert len(acks) < 13


# --- what the radio reports ------------------------------------------------------------------------------------


async def test_duty_cycle_and_link_errors_are_retried(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a, b = await _two_gateways(hass, mesh)
    mesh.routing = ["DUTY_CYCLE_LIMIT", "MAX_RETRANSMIT", None, "NONE"]

    message = await a.files.async_send(A_TO_B, PHOTO, name="photo.jpg", mime="image/jpeg")
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert done["status"] == FileStatus.DELIVERED, done["file"]
    offers = [p for _, p in a.client.data_sent if chat_files.packet_type(p) == PacketType.OFFER]
    assert len(offers) == 4
    assert b.file_message(B_TO_A)["file"]["image"] is True


async def test_persistent_link_error_fails_the_transfer(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a, _ = await _two_gateways(hass, mesh)
    mesh.routing = ["MAX_RETRANSMIT", "MAX_RETRANSMIT", "MAX_RETRANSMIT"]

    message = await a.files.async_send(A_TO_B, PHOTO, name="photo.jpg")
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert done["status"] == FileStatus.FAILED
    assert done["file"]["error"].startswith("MAX_RETRANSMIT (no acknowledgement after")


async def test_configuration_error_fails_at_once(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a, _ = await _two_gateways(hass, mesh)
    mesh.routing = ["PKI_UNKNOWN_PUBKEY"]

    message = await a.files.async_send(A_TO_B, PHOTO, name="photo.jpg")
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert done["status"] == FileStatus.FAILED
    assert "PKI_UNKNOWN_PUBKEY" in done["file"]["error"]
    assert len(a.client.data_sent) == 1


async def test_a_node_without_mesh_chat_never_answers(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a = await _setup(hass, _entry(hass, MeshFakeClient(GATEWAY, mesh), GATEWAY, "Home Gateway"), mesh.clients[GATEWAY])

    message = await a.files.async_send(A_TO_B, PHOTO, name="photo.jpg")
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert done["status"] == FileStatus.FAILED
    assert done["file"]["error"] == "No Mesh Chat with file transfer answered at this node"


async def test_receiver_that_stops_answering(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a, b = await _two_gateways(hass, mesh)
    mesh.hold = _is_type(PacketType.DATA)  # Anna's radio acknowledges every chunk, her Home Assistant sees none

    message = await a.files.async_send(A_TO_B, PHOTO, name="photo.jpg")
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert b.file_message(B_TO_A)["status"] == FileStatus.RECEIVING
    assert done["status"] == FileStatus.FAILED
    assert done["file"]["error"] == "The Mesh Chat at this node stopped answering"
    assert a.client.chunks_sent() == [0, 0, 0]


# --- the receiver's say ----------------------------------------------------------------------------------------


async def test_receiver_asks_before_accepting(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a, b = await _two_gateways(hass, mesh, files_accept="ask")
    message = await a.files.async_send(A_TO_B, PHOTO, name="photo.jpg")
    await until(lambda: b.file_message(B_TO_A) is not None)
    offered = b.file_message(B_TO_A)
    assert offered["status"] == FileStatus.OFFERED
    assert a.message(A_TO_B, message["id"])["status"] == FileStatus.OFFERING

    await b.files.async_accept(offered["id"], accept=True)
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert done["status"] == FileStatus.DELIVERED, done["file"]
    with pytest.raises(Exception, match="No file is waiting"):
        await b.files.async_accept(offered["id"], accept=True)


async def test_receiver_declines(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a, b = await _two_gateways(hass, mesh, files_accept="ask")
    message = await a.files.async_send(A_TO_B, PHOTO, name="photo.jpg")
    await until(lambda: b.file_message(B_TO_A) is not None)

    await b.files.async_accept(b.file_message(B_TO_A)["id"], accept=False)
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.REJECTED, FileStatus.FAILED)
    assert (done["status"], done["file"]["error"]) == (FileStatus.REJECTED, "the file was declined")
    assert b.file_message(B_TO_A)["status"] == FileStatus.REJECTED


async def test_receiver_rejects_what_it_cannot_take(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a, b = await _two_gateways(hass, mesh, files_max_kb=1)

    message = await a.files.async_send(A_TO_B, PHOTO, name="photo.jpg")
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.REJECTED, FileStatus.FAILED)
    assert (done["status"], done["file"]["error"]) == (FileStatus.REJECTED, "the file is larger than the node accepts")
    assert b.file_message(B_TO_A) is None

    async def full() -> int:
        return b.files.storage_bytes

    b.files._async_usage = full  # noqa: SLF001
    message = await a.files.async_send(A_TO_B, b"tiny", name="note.txt")
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.REJECTED, FileStatus.FAILED)
    assert done["file"]["error"] == "the node has no storage left for files"

    # the sender's own limits
    with pytest.raises(Exception, match="direct message"):
        await a.files.async_send("channel:0", b"x", name="x")
    with pytest.raises(Exception, match="larger than 32 kB"):
        await a.files.async_send(A_TO_B, os.urandom(32 * 1024 + 1), name="big.bin")
    with pytest.raises(Exception, match="empty"):
        await a.files.async_send(A_TO_B, b"", name="empty")


# --- cancel and resume -----------------------------------------------------------------------------------------


async def test_cancel_from_either_side(hass: HomeAssistant, mesh: FakeMesh) -> None:
    a, b = await _two_gateways(hass, mesh)
    mesh.hold = _is_data(2)

    message = await a.files.async_send(A_TO_B, NOTE, name="trail.gpx")
    await until(lambda: b.file_message(B_TO_A) is not None and b.file_message(B_TO_A)["file"]["chunks_done"] == 2)
    await a.files.async_cancel(message["id"])
    assert a.message(A_TO_B, message["id"])["status"] == FileStatus.CANCELLED
    cancelled = await _wait_status(b, B_TO_A, b.file_message(B_TO_A)["id"], FileStatus.CANCELLED)
    assert cancelled["file"]["error"] == "cancelled by the other side"
    with pytest.raises(Exception, match="No transfer is running"):
        await a.files.async_cancel(message["id"])

    # the other way round
    mesh.hold = _is_data(2)
    message = await a.files.async_send(A_TO_B, NOTE, name="trail.gpx")
    await until(lambda: len(b.chat.history(B_TO_A)) == 2 and b.chat.history(B_TO_A)[-1]["file"]["chunks_done"] == 2)
    await b.files.async_cancel(b.chat.history(B_TO_A)[-1]["id"])
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.CANCELLED, FileStatus.FAILED)
    assert (done["status"], done["file"]["error"]) == (FileStatus.CANCELLED, "cancelled by the other side")


async def test_sender_resumes_after_a_restart(hass: HomeAssistant, mesh: FakeMesh, hass_storage: dict) -> None:
    a, b = await _two_gateways(hass, mesh)
    mesh.hold = lambda _f, _t, p: chat_files.packet_type(p) == PacketType.DATA and Data.decode(p).seq >= 5

    message = await a.files.async_send(A_TO_B, NOTE, name="trail.gpx")
    await until(lambda: b.file_message(B_TO_A) is not None and b.file_message(B_TO_A)["file"]["chunks_done"] == 5)
    await async_unload_chat(hass, a.entry)
    stored = hass_storage[f"{DOMAIN}.chat_files.{a.entry.entry_id}"]["data"]["outgoing"]
    assert [r["name"] for r in stored.values()] == ["trail.gpx"]
    assert a.message(A_TO_B, message["id"])["status"] == FileStatus.SENDING

    mesh.hold = None
    a.client.data_sent.clear()
    a = await _setup(hass, a.entry, a.client)
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert done["status"] == FileStatus.DELIVERED, done["file"]
    assert a.client.chunks_sent() == list(range(5, 13))  # the receiver said what it had
    assert Path(b.file_message(B_TO_A)["file"]["path"]).read_bytes() == NOTE


async def test_receiver_resumes_after_a_restart(hass: HomeAssistant, mesh: FakeMesh, hass_storage: dict) -> None:
    a, b = await _two_gateways(hass, mesh)
    mesh.hold = lambda _f, _t, p: chat_files.packet_type(p) == PacketType.DATA and Data.decode(p).seq >= 4

    message = await a.files.async_send(A_TO_B, NOTE, name="trail.gpx")
    await until(lambda: b.file_message(B_TO_A) is not None and b.file_message(B_TO_A)["file"]["chunks_done"] == 4)
    await async_unload_chat(hass, b.entry)
    stored = hass_storage[f"{DOMAIN}.chat_files.{b.entry.entry_id}"]["data"]["incoming"]
    assert [sorted(int(s) for s in r["chunks"]) for r in stored.values()] == [[0, 1, 2, 3]]

    mesh.hold = None
    b = await _setup(hass, b.entry, b.client)
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert done["status"] == FileStatus.DELIVERED, done["file"]
    assert Path(b.file_message(B_TO_A)["file"]["path"]).read_bytes() == NOTE


# --- storage, view, websocket ----------------------------------------------------------------------------------


async def test_files_are_served_and_removed_with_their_messages(
    hass: HomeAssistant, mesh: FakeMesh, hass_client, hass_read_only_access_token
) -> None:
    assert await async_setup_component(hass, "http", {})
    a, b = await _two_gateways(hass, mesh)
    photo = await a.files.async_send(A_TO_B, PHOTO, name="../../evil name.jpg", mime="image/jpeg")
    assert photo["file"]["name"] == "evil_name.jpg"
    await _wait_status(a, A_TO_B, photo["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    note = await a.files.async_send(A_TO_B, b"<html>hi</html>", name="page.html", mime="text/html")
    await _wait_status(a, A_TO_B, note["id"], FileStatus.DELIVERED, FileStatus.FAILED)

    client = await hass_client()
    response = await client.get(b.file_message(B_TO_A)["file"]["url"])
    assert response.status == 200
    assert await response.read() == PHOTO
    assert response.headers["Content-Type"] == "image/jpeg"
    assert response.headers["Content-Disposition"] == 'inline; filename="evil_name.jpg"'
    assert response.headers["X-Content-Type-Options"] == "nosniff"

    # the sender keeps its copy too; anything but an image downloads as a plain file
    response = await client.get(note["file"]["url"])
    assert response.status == 200
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert response.headers["Content-Disposition"] == 'attachment; filename="page.html"'

    assert (await client.get(f"/api/meshtastic/chat/files/{a.entry.entry_id}/12345")).status == 404
    assert (await client.get(f"/api/meshtastic/chat/files/nope/{photo['file']['transfer_id']}")).status == 404
    assert (await client.get(f"/api/meshtastic/chat/files/{a.entry.entry_id}/x")).status == 404
    read_only = await hass_client(hass_read_only_access_token)
    assert (await read_only.get(note["file"]["url"])).status == 403

    photo_path = Path(b.file_message(B_TO_A)["file"]["path"])
    assert photo_path.is_file()
    b.chat.clear(B_TO_A)
    await until(lambda: not photo_path.is_file())
    assert (await client.get(photo["file"]["url"].replace(a.entry.entry_id, b.entry.entry_id))).status == 404


async def test_websocket_file_commands(hass: HomeAssistant, mesh: FakeMesh, hass_ws_client) -> None:
    a, b = await _two_gateways(hass, mesh, files_accept="ask")
    ws = await hass_ws_client(hass)

    await ws.send_json({"id": 1, "type": "meshtastic/chat/gateways"})
    gateways = {g["entry_id"]: g for g in (await ws.receive_json())["result"]}
    assert gateways[a.entry.entry_id]["files"] == {
        "enabled": True,
        "max_bytes": 32 * 1024,
        "ask": False,
        "window": 1,
        "image_max_px": 320,
        "chunk_bytes": 200,
        "preset": "LONG_FAST",
        "airtime": 2.2,
        "overhead": 1.0,
        "url": f"/api/meshtastic/chat/files/{a.entry.entry_id}",
    }
    assert gateways[b.entry.entry_id]["files"]["ask"] is True

    await ws.send_json(
        {
            "id": 2,
            "type": "meshtastic/chat/file/send",
            "entry_id": a.entry.entry_id,
            "conversation": A_TO_B,
            "name": "photo.jpg",
            "mime": "image/jpeg",
            "data": base64.b64encode(PHOTO).decode(),
        }
    )
    response = await ws.receive_json()
    assert response["success"], response
    message = response["result"]
    assert message["file"]["name"] == "photo.jpg"
    await until(lambda: b.file_message(B_TO_A) is not None)

    await ws.send_json(
        {
            "id": 3,
            "type": "meshtastic/chat/file/accept",
            "entry_id": b.entry.entry_id,
            "message_id": b.file_message(B_TO_A)["id"],
            "accept": True,
        }
    )
    assert (await ws.receive_json())["success"]
    done = await _wait_status(a, A_TO_B, message["id"], FileStatus.DELIVERED, FileStatus.FAILED)
    assert done["status"] == FileStatus.DELIVERED

    await ws.send_json(
        {"id": 4, "type": "meshtastic/chat/file/cancel", "entry_id": a.entry.entry_id, "message_id": message["id"]}
    )
    response = await ws.receive_json()
    assert response["error"]["code"] == "invalid_format"

    await ws.send_json(
        {
            "id": 5,
            "type": "meshtastic/chat/file/send",
            "entry_id": a.entry.entry_id,
            "conversation": A_TO_B,
            "name": "x",
            "data": "not base64!",
        }
    )
    assert (await ws.receive_json())["error"]["message"] == "The file data is not valid base64"

    plain = MockConfigEntry(domain=DOMAIN, title="Plain", data={}, options={"chat": {"enable": True}})
    plain.add_to_hass(hass)
    plain.runtime_data = SimpleNamespace(client=FakeClient(), gateway_node={"num": 7, "user": {}})
    await async_setup_chat(hass, plain)
    await ws.send_json({"id": 6, "type": "meshtastic/chat/file/cancel", "entry_id": plain.entry_id, "message_id": "m"})
    assert (await ws.receive_json())["error"]["code"] == "not_supported"
    await async_unload_chat(hass, plain)


def test_helpers() -> None:
    assert safe_file_name("C:\\Users\\me\\My Photo (1).JPG") == "My_Photo_1_.JPG"
    assert safe_file_name("../../.hidden") == "hidden"
    assert safe_file_name("???") == "file"
    assert len(safe_file_name("x" * 100)) == 64
    assert estimate_seconds(160, "LONG_FAST", 0) == 512
    assert estimate_seconds(160, "SHORT_FAST", 2) == 256
    assert estimate_seconds(10, None, None) == estimate_seconds(10, "LONG_FAST", 0)
