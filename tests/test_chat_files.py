# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

"""The file transfer protocol and the plumbing it rides on (raw private app payloads, data packets with acks)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import zlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.meshtastic import chat_files
from custom_components.meshtastic.aiomeshtastic.errors import MeshtasticError
from custom_components.meshtastic.aiomeshtastic.interface import MeshInterface
from custom_components.meshtastic.aiomeshtastic.packet import Packet
from custom_components.meshtastic.aiomeshtastic.protobuf import mesh_pb2, portnums_pb2
from custom_components.meshtastic.api import MeshtasticApiClient, MeshtasticApiClientError
from custom_components.meshtastic.chat_files import (
    DEFAULT_CHUNK_BYTES,
    MAX_CHUNK_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_WINDOW,
    NAME_MAX_BYTES,
    Accept,
    Ack,
    Cancel,
    CancelReason,
    Data,
    Done,
    DoneResult,
    Offer,
    PacketType,
    ProtocolError,
    Reassembly,
    Reject,
    RejectReason,
    missing_after,
    prepare,
)

GATEWAY = 0x4904
ANNA = 0xA1B2C3D4
TRANSFER = 0xDEADBEEF

PHOTO = os.urandom(5000)  # does not compress
# a track log: compresses well, but not down to one chunk
NOTE = "".join(f'<trkpt lat="{47 + i / 1000:.6f}" lon="{8 + i / 997:.6f}"/>\n' for i in range(400)).encode()


def offer(**overrides: Any) -> Offer:
    fields = {
        "transfer_id": TRANSFER,
        "size": 450,
        "chunk_count": 3,
        "chunk_bytes": 200,
        "sha256_16": hashlib.sha256(b"x").digest()[:16],
        "name": "trail.gpx",
        "mime": "application/gpx+xml",
    }
    return Offer(**{**fields, **overrides})


# --- packets -------------------------------------------------------------------------------------------------------


def test_offer_round_trip_and_size_limit() -> None:
    o = offer(deflate=True, image=True)
    packet = o.encode()
    assert packet[:2] == bytes([chat_files.PROTOCOL_MAGIC, PacketType.OFFER])
    assert chat_files.packet_type(packet) == PacketType.OFFER
    assert chat_files.transfer_id_of(packet) == TRANSFER
    assert chat_files.decode(packet) == o

    # the longest allowed name and a long MIME type still fit one packet
    long = offer(name="n" * NAME_MAX_BYTES, mime="application/vnd.something.very-long+json")
    assert len(long.encode()) <= MAX_PAYLOAD_BYTES
    with pytest.raises(ProtocolError, match="file name"):
        offer(name="n" * (NAME_MAX_BYTES + 1)).encode()
    with pytest.raises(ProtocolError, match="Chunks"):
        offer(chunk_bytes=MAX_CHUNK_BYTES + 1).encode()
    with pytest.raises(ProtocolError, match="hash"):
        offer(sha256_16=b"short").encode()


def test_offer_decode_rejects_nonsense() -> None:
    packet = offer().encode()
    with pytest.raises(ProtocolError, match="truncated"):
        Offer.decode(packet[:10])
    with pytest.raises(ProtocolError, match="no file name"):
        Offer.decode(offer(name="").encode())
    # the size must match the chunks: 3 chunks of 200 bytes carry 401 to 600 bytes
    with pytest.raises(ProtocolError, match="size does not match"):
        Offer.decode(offer(size=400).encode())
    with pytest.raises(ProtocolError, match="size does not match"):
        Offer.decode(offer(size=601).encode())
    # a mime length pointing past the end
    broken = bytearray(packet)
    broken[chat_files._OFFER_HEAD.size - 1] = 200  # noqa: SLF001
    with pytest.raises(ProtocolError, match="truncated"):
        Offer.decode(bytes(broken))


def test_accept_carries_the_resume_bitmap() -> None:
    a = Accept(TRANSFER, window=4, have=frozenset({0, 1, 9, 17}), chunk_count=18)
    packet = a.encode()
    assert len(packet) == chat_files._ACCEPT_HEAD.size + 3  # noqa: SLF001
    assert chat_files.decode(packet, chunk_count=18) == a
    # chunks beyond the count are dropped from the bitmap
    assert Accept.decode(Accept(TRANSFER, have=frozenset({5, 99}), chunk_count=6).encode(), 6).have == {5}
    with pytest.raises(ProtocolError, match="only be decoded against"):
        chat_files.decode(packet)
    with pytest.raises(ProtocolError, match="window"):
        Accept(TRANSFER, window=MAX_WINDOW + 1).encode()
    with pytest.raises(ProtocolError, match="window"):
        Accept.decode(bytes([chat_files.PROTOCOL_MAGIC, PacketType.ACCEPT]) + TRANSFER.to_bytes(4, "big") + b"\x00", 1)


def test_fixed_packets_round_trip() -> None:
    for message in (
        Reject(TRANSFER, RejectReason.STORAGE_FULL),
        Done(TRANSFER, DoneResult.HASH_MISMATCH),
        Cancel(TRANSFER, CancelReason.TIMEOUT),
        Ack(TRANSFER, 7, frozenset({8, 10, 15})),
        Data(TRANSFER, 65535, b"\x00" * MAX_CHUNK_BYTES),
    ):
        packet = message.encode()
        assert len(packet) <= MAX_PAYLOAD_BYTES
        assert chat_files.transfer_id_of(packet) == TRANSFER
        assert chat_files.decode(packet) == message

    with pytest.raises(ProtocolError, match="wrong length"):
        Reject.decode(Reject(TRANSFER, 1).encode() + b"\x00")
    with pytest.raises(ProtocolError, match="truncated"):
        Data.decode(b"\x4d\x04\x00")
    with pytest.raises(ProtocolError, match="at most"):
        Data(TRANSFER, 0, b"\x00" * (MAX_CHUNK_BYTES + 1)).encode()


def test_ack_bitmap_covers_the_eight_chunks_after_the_missing_one() -> None:
    ack = Ack(TRANSFER, 10, frozenset({11, 18, 19, 30}))  # 19 and 30 are outside the window
    decoded = Ack.decode(ack.encode())
    assert (decoded.next_seq, decoded.received) == (10, frozenset({11, 18}))


def test_foreign_packets_are_told_apart() -> None:
    assert not chat_files.is_file_transfer_packet(b"")
    assert not chat_files.is_file_transfer_packet(b"\x4d")
    assert not chat_files.is_file_transfer_packet(b"hello on the private port")
    assert not chat_files.is_file_transfer_packet(bytes([chat_files.PROTOCOL_MAGIC, 0x7F]))
    assert chat_files.is_file_transfer_packet(Cancel(TRANSFER).encode())
    with pytest.raises(ProtocolError, match="Not a file transfer packet"):
        chat_files.decode(b"nope")
    with pytest.raises(ProtocolError, match="truncated"):
        chat_files.transfer_id_of(b"\x4d\x07\x00")


# --- chunking ------------------------------------------------------------------------------------------------------


def test_prepare_splits_and_hashes() -> None:
    prepared = prepare(PHOTO, transfer_id=TRANSFER, name="photo.jpg", mime="image/jpeg")
    o = prepared.offer
    assert (o.size, o.chunk_count, o.chunk_bytes, o.deflate, o.image) == (5000, 25, DEFAULT_CHUNK_BYTES, False, True)
    assert o.sha256_16 == hashlib.sha256(PHOTO).digest()[:16]
    assert prepared.original_size == 5000
    chunks = [prepared.chunk(seq) for seq in range(o.chunk_count)]
    assert all(len(c.encode()) <= MAX_PAYLOAD_BYTES for c in chunks)
    assert [len(c.payload) for c in chunks] == [200] * 25
    assert b"".join(c.payload for c in chunks) == PHOTO
    with pytest.raises(ProtocolError, match="out of range"):
        prepared.chunk(25)

    # an uneven file ends with a short chunk
    short = prepare(PHOTO[:4321], transfer_id=TRANSFER, name="part", mime="", compress=False)
    assert (short.offer.chunk_count, len(short.chunk(21).payload)) == (22, 121)
    with pytest.raises(ProtocolError, match="empty"):
        prepare(b"", transfer_id=TRANSFER, name="empty")


def test_prepare_deflates_when_it_saves_chunks() -> None:
    prepared = prepare(NOTE, transfer_id=TRANSFER, name="note.txt", mime="text/plain")
    assert prepared.offer.deflate
    assert prepared.offer.chunk_count < len(NOTE) // DEFAULT_CHUNK_BYTES
    assert prepared.offer.size == len(prepared.body) < len(NOTE)
    assert zlib.decompress(prepared.body) == NOTE
    # the hash is of the original file, so the receiver can check the inflated result
    assert prepared.offer.sha256_16 == hashlib.sha256(NOTE).digest()[:16]

    plain = prepare(NOTE, transfer_id=TRANSFER, name="note.txt", compress=False)
    assert not plain.offer.deflate
    assert plain.body == NOTE


def test_reassembly_out_of_order_duplicates_and_resume() -> None:
    prepared = prepare(NOTE, transfer_id=TRANSFER, name="note.txt", mime="text/plain")
    receiving = Reassembly(prepared.offer)
    count = prepared.offer.chunk_count
    assert count >= 3

    assert receiving.add(prepared.chunk(1))
    assert not receiving.add(prepared.chunk(1))  # the radio reported it twice
    assert receiving.next_seq == 0
    assert receiving.ack() == Ack(TRANSFER, 0, frozenset({1}))
    assert not receiving.complete
    with pytest.raises(ProtocolError, match="not complete"):
        receiving.assemble()

    # what the receiver already has is what a resumed sender skips
    resumed = Accept.decode(Accept(TRANSFER, have=receiving.have, chunk_count=count).encode(), count)
    assert resumed.have == {1}
    assert missing_after(Ack(TRANSFER, 0, frozenset({1})), count, 4) == [0, 2, 3]
    assert missing_after(Ack(TRANSFER, count - 1), count, 8) == [count - 1]
    assert missing_after(Ack(TRANSFER, count), count, 8) == []

    for seq in reversed(range(count)):
        receiving.add(prepared.chunk(seq))
    assert receiving.complete
    assert receiving.next_seq == count
    assert receiving.ack() == Ack(TRANSFER, count)
    assert receiving.assemble() == NOTE


def test_reassembly_rejects_chunks_that_cannot_belong() -> None:
    prepared = prepare(PHOTO, transfer_id=TRANSFER, name="photo.jpg", mime="image/jpeg")
    receiving = Reassembly(prepared.offer)
    with pytest.raises(ProtocolError, match="another transfer"):
        receiving.add(Data(TRANSFER + 1, 0, b"\x00" * 200))
    with pytest.raises(ProtocolError, match="out of range"):
        receiving.add(Data(TRANSFER, 25, b"\x00" * 200))
    with pytest.raises(ProtocolError, match="199 bytes instead of 200"):
        receiving.add(Data(TRANSFER, 3, b"\x00" * 199))

    # a corrupted chunk of the right size is caught by the hash at the end
    for seq in range(25):
        receiving.add(prepared.chunk(seq) if seq != 7 else Data(TRANSFER, 7, bytes(200)))
    with pytest.raises(ProtocolError, match="hash does not match"):
        receiving.assemble()

    # a deflated body that does not inflate
    broken = Reassembly(offer(size=10, chunk_count=1, chunk_bytes=10, deflate=True))
    broken.add(Data(TRANSFER, 0, b"not zlib!!"))
    with pytest.raises(ProtocolError, match="inflated"):
        broken.assemble()


# --- plumbing ------------------------------------------------------------------------------------------------------


def private_app_packet(payload: bytes, port_num: int = portnums_pb2.PortNum.PRIVATE_APP) -> mesh_pb2.FromRadio:
    from_radio = mesh_pb2.FromRadio()
    from_radio.packet.id = 42
    setattr(from_radio.packet, "from", ANNA)
    from_radio.packet.to = GATEWAY
    from_radio.packet.decoded.portnum = port_num
    from_radio.packet.decoded.payload = payload
    return from_radio


def test_packet_hands_over_private_app_payloads_as_bytes() -> None:
    payload = Cancel(TRANSFER).encode()
    assert Packet(private_app_packet(payload)).app_payload == payload
    assert Packet(private_app_packet(payload, portnums_pb2.PortNum.MAX)).app_payload == payload
    # a port number this version does not know still yields nothing
    assert Packet(private_app_packet(payload, portnums_pb2.PortNum.REPLY_APP)).app_payload is None


async def test_interface_delivers_private_app_packets_to_listeners() -> None:
    interface = MeshInterface(connection=MagicMock())
    received: list[tuple[int, bytes]] = []

    async def on_packet(node: Any, packet: Packet) -> None:
        received.append((node.id, packet.app_payload))

    unsubscribe = interface.add_packet_app_listener(portnums_pb2.PortNum.PRIVATE_APP, on_packet, as_packet=True)
    payload = Done(TRANSFER).encode()
    await interface._process_packet_for_app_listener(private_app_packet(payload))  # noqa: SLF001
    await asyncio.gather(*interface._background_tasks)  # noqa: SLF001
    assert received == [(ANNA, payload)]

    unsubscribe()
    await interface._process_packet_for_app_listener(private_app_packet(payload))  # noqa: SLF001
    await asyncio.gather(*interface._background_tasks)  # noqa: SLF001
    assert len(received) == 1


async def test_interface_send_data_message() -> None:
    interface = MeshInterface(connection=MagicMock())
    interface._connected_node_info = SimpleNamespace(my_node_num=GATEWAY)  # noqa: SLF001
    interface._connection.send_mesh_packet = AsyncMock(return_value=None)  # noqa: SLF001

    payload = Data(TRANSFER, 0, b"\x00" * DEFAULT_CHUNK_BYTES).encode()
    await interface.send_data_message(payload, ANNA, want_ack=True, packet_id=555)
    kwargs = interface._connection.send_mesh_packet.call_args.kwargs  # noqa: SLF001
    assert kwargs["message"] == payload
    assert kwargs["port_num"] == portnums_pb2.PortNum.PRIVATE_APP
    assert (kwargs["to_node"], kwargs["from_node"], kwargs["channel_index"]) == (ANNA, GATEWAY, None)
    assert kwargs["priority"] == mesh_pb2.MeshPacket.Priority.RELIABLE
    assert (kwargs["ack"], kwargs["packet_id"]) == (True, 555)

    # text messages take the same road
    await interface.send_text_message("hi", ANNA)
    kwargs = interface._connection.send_mesh_packet.call_args.kwargs  # noqa: SLF001
    assert (kwargs["message"], kwargs["port_num"]) == (b"hi", portnums_pb2.PortNum.TEXT_MESSAGE_APP)
    assert (kwargs["ack"], kwargs["priority"]) == (False, mesh_pb2.MeshPacket.Priority.DEFAULT)

    # what the radio would NAK as TOO_LARGE is refused here
    with pytest.raises(ValueError, match="larger than 233 bytes"):
        await interface.send_data_message(bytes(MAX_PAYLOAD_BYTES + 1), ANNA)
    assert interface._connection.send_mesh_packet.await_count == 2  # noqa: SLF001


async def test_api_client_send_data_with_ack() -> None:
    client = MeshtasticApiClient.__new__(MeshtasticApiClient)
    routing = mesh_pb2.Routing()
    routing.error_reason = mesh_pb2.Routing.Error.NONE
    ack = SimpleNamespace(from_id=ANNA, app_payload=routing)
    client._interface = SimpleNamespace(send_data_message=AsyncMock(return_value=ack))  # noqa: SLF001

    payload = Offer.encode(offer())
    assert await client.send_data_with_ack(payload, ANNA, packet_id=777) == (ANNA, "NONE")
    kwargs = client._interface.send_data_message.call_args.kwargs  # noqa: SLF001
    assert client._interface.send_data_message.call_args.args == (payload, ANNA)  # noqa: SLF001
    assert (kwargs["port_num"], kwargs["want_ack"], kwargs["packet_id"]) == (
        portnums_pb2.PortNum.PRIVATE_APP,
        True,
        777,
    )

    routing.error_reason = mesh_pb2.Routing.Error.DUTY_CYCLE_LIMIT
    assert await client.send_data_with_ack(payload, ANNA) == (ANNA, "DUTY_CYCLE_LIMIT")

    client._interface.send_data_message = AsyncMock(return_value=None)  # noqa: SLF001
    assert await client.send_data_with_ack(payload, ANNA) == (None, None)

    async def never(*_: Any, **__: Any) -> None:
        await asyncio.sleep(10)

    client._interface.send_data_message = never  # noqa: SLF001
    assert await client.send_data_with_ack(payload, ANNA, ack_timeout=0.01) == (None, None)

    client._interface.send_data_message = AsyncMock(side_effect=MeshtasticError("Channel #2 is disabled"))  # noqa: SLF001
    with pytest.raises(MeshtasticApiClientError, match="Channel #2 is disabled"):
        await client.send_data_with_ack(payload, ANNA)


def test_api_client_data_listener_delegates_to_the_interface() -> None:
    client = MeshtasticApiClient.__new__(MeshtasticApiClient)
    client._interface = SimpleNamespace(add_packet_app_listener=MagicMock(return_value="unsubscribe"))  # noqa: SLF001

    async def on_packet(_node: Any, _packet: Packet) -> None:
        pass

    assert client.add_data_listener(portnums_pb2.PortNum.PRIVATE_APP, on_packet) == "unsubscribe"
    client._interface.add_packet_app_listener.assert_called_once_with(  # noqa: SLF001
        packet_type=portnums_pb2.PortNum.PRIVATE_APP, callback=on_packet, as_packet=True
    )
