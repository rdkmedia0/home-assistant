# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

"""
The wire protocol of Mesh Chat file transfers.

A file is sent to one other node, as a direct message, in chunks small enough for a Meshtastic packet. Every packet
of the protocol travels on the private app port number (``PRIVATE_APP``) and starts with a magic byte so other
private apps on the same port are told apart. Integers are big-endian. See ``FILE_TRANSFER_DESIGN.md``:

* OFFER (sender -> receiver): what is about to be sent, with the file's name, size, chunking and SHA-256.
* ACCEPT / REJECT (receiver -> sender): the receiver's answer; ACCEPT carries the chunks it already holds (resume).
* DATA (sender -> receiver): one chunk.
* ACK (receiver -> sender): the lowest missing chunk and a bitmap of the chunks after it.
* DONE (receiver -> sender): the file is complete and verified (or not).
* CANCEL (either way): the transfer stops.

The firmware acknowledges every direct message it delivers to the other radio (``want_ack``); the ACK of this
protocol is what confirms that the Home Assistant behind that radio actually got the chunk.
"""

from __future__ import annotations

import hashlib
import math
import struct
import zlib
from dataclasses import dataclass, field
from enum import IntEnum

from .aiomeshtastic.protobuf import mesh_pb2, portnums_pb2

FILE_TRANSFER_PORT = portnums_pb2.PortNum.PRIVATE_APP
PROTOCOL_MAGIC = 0x4D  # 'M'
PROTOCOL_VERSION = 1

# the whole packet must fit a Meshtastic payload; the chunk header leaves this much for the file's bytes
MAX_PAYLOAD_BYTES = mesh_pb2.Constants.DATA_PAYLOAD_LEN  # 233
DATA_HEADER_BYTES = 8  # magic, type, transfer id, sequence number
DEFAULT_CHUNK_BYTES = 200
MAX_CHUNK_BYTES = MAX_PAYLOAD_BYTES - DATA_HEADER_BYTES  # 225
MAX_CHUNK_COUNT = 0xFFFF
MAX_WINDOW = 8  # the ACK bitmap is one byte
NAME_MAX_BYTES = 64
MIME_MAX_BYTES = 255
HASH_BYTES = 16  # the first 16 bytes of the SHA-256


class PacketType(IntEnum):
    OFFER = 0x01
    ACCEPT = 0x02
    REJECT = 0x03
    DATA = 0x04
    ACK = 0x05
    DONE = 0x06
    CANCEL = 0x07


class RejectReason(IntEnum):
    DISABLED = 1
    TOO_LARGE = 2
    STORAGE_FULL = 3
    BUSY = 4
    DECLINED = 5
    UNSUPPORTED_VERSION = 6


class CancelReason(IntEnum):
    USER = 1
    TIMEOUT = 2
    ERROR = 3


class DoneResult(IntEnum):
    OK = 0
    HASH_MISMATCH = 1


class OfferFlag(IntEnum):
    DEFLATE = 0x01  # the chunks carry the deflated file
    IMAGE = 0x02  # the receiver may show it inline


class ProtocolError(ValueError):
    """A packet that is not a valid file transfer packet."""


_OFFER_HEAD = struct.Struct(">BBBIIHBB16sB")  # magic, type, version, transfer id, size, chunks, chunk bytes, flags
_ACCEPT_HEAD = struct.Struct(">BBIB")  # magic, type, transfer id, window
_REJECT = struct.Struct(">BBIB")  # magic, type, transfer id, reason
_DATA_HEAD = struct.Struct(">BBIH")  # magic, type, transfer id, sequence number
_ACK = struct.Struct(">BBIHB")  # magic, type, transfer id, next sequence number, window bitmap
_DONE = struct.Struct(">BBIB")  # magic, type, transfer id, result
_CANCEL = struct.Struct(">BBIB")  # magic, type, transfer id, reason

assert _DATA_HEAD.size == DATA_HEADER_BYTES  # noqa: S101


def file_hash(data: bytes) -> bytes:
    """Return the part of the file's SHA-256 that travels in the offer."""
    return hashlib.sha256(data).digest()[:HASH_BYTES]


def bitmap_bytes(count: int) -> int:
    return math.ceil(count / 8)


def bitmap_from_seqs(seqs: set[int] | frozenset[int], count: int) -> bytes:
    """Build a bitmap of ``count`` bits, bit ``seq`` set for every sequence number in ``seqs`` (bit 0 is the MSB)."""
    bits = bytearray(bitmap_bytes(count))
    for seq in seqs:
        if 0 <= seq < count:
            bits[seq // 8] |= 0x80 >> (seq % 8)
    return bytes(bits)


def seqs_from_bitmap(bitmap: bytes, count: int) -> set[int]:
    return {seq for seq in range(count) if seq // 8 < len(bitmap) and bitmap[seq // 8] & (0x80 >> (seq % 8))}


@dataclass(frozen=True)
class Offer:
    transfer_id: int
    size: int  # bytes on the wire (after deflate when the flag is set)
    chunk_count: int
    chunk_bytes: int
    sha256_16: bytes  # of the original file
    name: str
    mime: str = ""
    deflate: bool = False
    image: bool = False
    version: int = PROTOCOL_VERSION

    def encode(self) -> bytes:
        name = self.name.encode("utf-8")
        mime = self.mime.encode("ascii")
        if len(name) > NAME_MAX_BYTES:
            msg = f"The file name is longer than {NAME_MAX_BYTES} bytes"
            raise ProtocolError(msg)
        if len(mime) > MIME_MAX_BYTES:
            msg = f"The MIME type is longer than {MIME_MAX_BYTES} bytes"
            raise ProtocolError(msg)
        if not 0 < self.chunk_bytes <= MAX_CHUNK_BYTES:
            msg = f"Chunks must be 1 to {MAX_CHUNK_BYTES} bytes"
            raise ProtocolError(msg)
        if not 0 < self.chunk_count <= MAX_CHUNK_COUNT:
            msg = f"A transfer has 1 to {MAX_CHUNK_COUNT} chunks"
            raise ProtocolError(msg)
        if len(self.sha256_16) != HASH_BYTES:
            msg = f"The hash must be {HASH_BYTES} bytes"
            raise ProtocolError(msg)
        flags = (OfferFlag.DEFLATE if self.deflate else 0) | (OfferFlag.IMAGE if self.image else 0)
        head = _OFFER_HEAD.pack(
            PROTOCOL_MAGIC,
            PacketType.OFFER,
            self.version,
            self.transfer_id,
            self.size,
            self.chunk_count,
            self.chunk_bytes,
            flags,
            self.sha256_16,
            len(mime),
        )
        packet = head + mime + name
        if len(packet) > MAX_PAYLOAD_BYTES:
            msg = "The offer does not fit a packet"
            raise ProtocolError(msg)
        return packet

    @classmethod
    def decode(cls, packet: bytes) -> Offer:
        if len(packet) < _OFFER_HEAD.size:
            msg = "Offer is truncated"
            raise ProtocolError(msg)
        _, _, version, transfer_id, size, chunk_count, chunk_bytes, flags, sha256_16, mime_len = _OFFER_HEAD.unpack(
            packet[: _OFFER_HEAD.size]
        )
        rest = packet[_OFFER_HEAD.size :]
        if len(rest) < mime_len:
            msg = "Offer is truncated"
            raise ProtocolError(msg)
        try:
            mime = rest[:mime_len].decode("ascii")
            name = rest[mime_len:].decode("utf-8")
        except UnicodeDecodeError as e:
            msg = "Offer carries an invalid name or MIME type"
            raise ProtocolError(msg) from e
        if not name:
            msg = "Offer has no file name"
            raise ProtocolError(msg)
        if chunk_count == 0 or chunk_bytes == 0:
            msg = "Offer has no chunks"
            raise ProtocolError(msg)
        if not (chunk_count - 1) * chunk_bytes < size <= chunk_count * chunk_bytes:
            msg = "Offer's size does not match its chunks"
            raise ProtocolError(msg)
        return cls(
            transfer_id=transfer_id,
            size=size,
            chunk_count=chunk_count,
            chunk_bytes=chunk_bytes,
            sha256_16=sha256_16,
            name=name,
            mime=mime,
            deflate=bool(flags & OfferFlag.DEFLATE),
            image=bool(flags & OfferFlag.IMAGE),
            version=version,
        )


@dataclass(frozen=True)
class Accept:
    transfer_id: int
    window: int = 1
    have: frozenset[int] = frozenset()  # chunks the receiver already holds (resume)
    chunk_count: int = 0  # needed to size the bitmap; the receiver knows it from the offer

    def encode(self) -> bytes:
        if not 1 <= self.window <= MAX_WINDOW:
            msg = f"The window is 1 to {MAX_WINDOW} chunks"
            raise ProtocolError(msg)
        return _ACCEPT_HEAD.pack(PROTOCOL_MAGIC, PacketType.ACCEPT, self.transfer_id, self.window) + bitmap_from_seqs(
            self.have, self.chunk_count
        )

    @classmethod
    def decode(cls, packet: bytes, chunk_count: int) -> Accept:
        """Decode against the chunk count of the offer this answers."""
        if len(packet) < _ACCEPT_HEAD.size:
            msg = "Accept is truncated"
            raise ProtocolError(msg)
        _, _, transfer_id, window = _ACCEPT_HEAD.unpack(packet[: _ACCEPT_HEAD.size])
        if not 1 <= window <= MAX_WINDOW:
            msg = f"The window is 1 to {MAX_WINDOW} chunks"
            raise ProtocolError(msg)
        have = seqs_from_bitmap(packet[_ACCEPT_HEAD.size :], chunk_count)
        return cls(transfer_id=transfer_id, window=window, have=frozenset(have), chunk_count=chunk_count)


@dataclass(frozen=True)
class Reject:
    transfer_id: int
    reason: int

    def encode(self) -> bytes:
        return _REJECT.pack(PROTOCOL_MAGIC, PacketType.REJECT, self.transfer_id, self.reason)

    @classmethod
    def decode(cls, packet: bytes) -> Reject:
        return cls(*_unpack_fixed(_REJECT, packet, "Reject")[2:])


@dataclass(frozen=True)
class Data:
    transfer_id: int
    seq: int
    payload: bytes

    def encode(self) -> bytes:
        if len(self.payload) > MAX_CHUNK_BYTES:
            msg = f"A chunk carries at most {MAX_CHUNK_BYTES} bytes"
            raise ProtocolError(msg)
        return _DATA_HEAD.pack(PROTOCOL_MAGIC, PacketType.DATA, self.transfer_id, self.seq) + self.payload

    @classmethod
    def decode(cls, packet: bytes) -> Data:
        if len(packet) < _DATA_HEAD.size:
            msg = "Data is truncated"
            raise ProtocolError(msg)
        _, _, transfer_id, seq = _DATA_HEAD.unpack(packet[: _DATA_HEAD.size])
        return cls(transfer_id=transfer_id, seq=seq, payload=packet[_DATA_HEAD.size :])


@dataclass(frozen=True)
class Ack:
    transfer_id: int
    next_seq: int  # the lowest sequence number still missing; the chunk count once everything arrived
    received: frozenset[int] = frozenset()  # chunks held among next_seq + 1 .. next_seq + 8

    def encode(self) -> bytes:
        bitmap = 0
        for seq in self.received:
            offset = seq - self.next_seq - 1
            if 0 <= offset < MAX_WINDOW:
                bitmap |= 0x80 >> offset
        return _ACK.pack(PROTOCOL_MAGIC, PacketType.ACK, self.transfer_id, self.next_seq, bitmap)

    @classmethod
    def decode(cls, packet: bytes) -> Ack:
        _, _, transfer_id, next_seq, bitmap = _unpack_fixed(_ACK, packet, "Ack")
        received = {next_seq + 1 + offset for offset in range(MAX_WINDOW) if bitmap & (0x80 >> offset)}
        return cls(transfer_id=transfer_id, next_seq=next_seq, received=frozenset(received))


@dataclass(frozen=True)
class Done:
    transfer_id: int
    result: int = DoneResult.OK

    def encode(self) -> bytes:
        return _DONE.pack(PROTOCOL_MAGIC, PacketType.DONE, self.transfer_id, self.result)

    @classmethod
    def decode(cls, packet: bytes) -> Done:
        return cls(*_unpack_fixed(_DONE, packet, "Done")[2:])


@dataclass(frozen=True)
class Cancel:
    transfer_id: int
    reason: int = CancelReason.USER

    def encode(self) -> bytes:
        return _CANCEL.pack(PROTOCOL_MAGIC, PacketType.CANCEL, self.transfer_id, self.reason)

    @classmethod
    def decode(cls, packet: bytes) -> Cancel:
        return cls(*_unpack_fixed(_CANCEL, packet, "Cancel")[2:])


type Message = Offer | Accept | Reject | Data | Ack | Done | Cancel

_PACKET_TYPES = {int(kind) for kind in PacketType}

_DECODERS = {
    PacketType.OFFER: Offer.decode,
    PacketType.REJECT: Reject.decode,
    PacketType.DATA: Data.decode,
    PacketType.ACK: Ack.decode,
    PacketType.DONE: Done.decode,
    PacketType.CANCEL: Cancel.decode,
}


def _unpack_fixed(layout: struct.Struct, packet: bytes, what: str) -> tuple:
    if len(packet) != layout.size:
        msg = f"{what} has the wrong length"
        raise ProtocolError(msg)
    return layout.unpack(packet)


def is_file_transfer_packet(packet: bytes) -> bool:
    """Whether a payload on the private app port belongs to this protocol."""
    return len(packet) >= 2 and packet[0] == PROTOCOL_MAGIC and packet[1] in _PACKET_TYPES  # noqa: PLR2004


def packet_type(packet: bytes) -> PacketType:
    if not is_file_transfer_packet(packet):
        msg = "Not a file transfer packet"
        raise ProtocolError(msg)
    return PacketType(packet[1])


def transfer_id_of(packet: bytes) -> int:
    """Read the transfer id of any packet of the protocol (after the magic, type and, for offers, version)."""
    kind = packet_type(packet)
    offset = 3 if kind == PacketType.OFFER else 2
    if len(packet) < offset + 4:
        msg = "Packet is truncated"
        raise ProtocolError(msg)
    return struct.unpack(">I", packet[offset : offset + 4])[0]


def decode(packet: bytes, *, chunk_count: int | None = None) -> Message:
    """
    Decode a packet of the protocol.

    An ACCEPT needs the chunk count of the offer it answers (``chunk_count``) to read its resume bitmap.
    """
    kind = packet_type(packet)
    if kind == PacketType.ACCEPT:
        if chunk_count is None:
            msg = "An accept can only be decoded against its offer"
            raise ProtocolError(msg)
        return Accept.decode(packet, chunk_count)
    return _DECODERS[kind](packet)


# --- chunking ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    """A file ready to be offered: its wire bytes (deflated when that is smaller) and the matching offer."""

    offer: Offer
    body: bytes
    original_size: int

    def chunk(self, seq: int) -> Data:
        if not 0 <= seq < self.offer.chunk_count:
            msg = f"Chunk {seq} is out of range"
            raise ProtocolError(msg)
        start = seq * self.offer.chunk_bytes
        return Data(self.offer.transfer_id, seq, self.body[start : start + self.offer.chunk_bytes])


def prepare(  # noqa: PLR0913
    data: bytes,
    *,
    transfer_id: int,
    name: str,
    mime: str = "",
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    compress: bool = True,
) -> Prepared:
    """Compute the offer for ``data``: deflate it when that saves at least one chunk, and split it."""
    if not data:
        msg = "The file is empty"
        raise ProtocolError(msg)
    if not 0 < chunk_bytes <= MAX_CHUNK_BYTES:
        msg = f"Chunks must be 1 to {MAX_CHUNK_BYTES} bytes"
        raise ProtocolError(msg)
    body = data
    deflate = False
    if compress:
        deflated = zlib.compress(data, 9)
        if math.ceil(len(deflated) / chunk_bytes) < math.ceil(len(data) / chunk_bytes):
            body = deflated
            deflate = True
    chunk_count = math.ceil(len(body) / chunk_bytes)
    if chunk_count > MAX_CHUNK_COUNT:
        msg = f"The file needs more than {MAX_CHUNK_COUNT} chunks"
        raise ProtocolError(msg)
    offer = Offer(
        transfer_id=transfer_id,
        size=len(body),
        chunk_count=chunk_count,
        chunk_bytes=chunk_bytes,
        sha256_16=file_hash(data),
        name=name,
        mime=mime,
        deflate=deflate,
        image=mime.lower().startswith("image/"),
    )
    return Prepared(offer=offer, body=body, original_size=len(data))


@dataclass
class Reassembly:
    """The receiver's side of one transfer: collects chunks until the file described by the offer is complete."""

    offer: Offer
    chunks: dict[int, bytes] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return len(self.chunks) == self.offer.chunk_count

    @property
    def have(self) -> frozenset[int]:
        return frozenset(self.chunks)

    @property
    def next_seq(self) -> int:
        """The lowest sequence number still missing, the chunk count once everything arrived."""
        seq = 0
        while seq in self.chunks:
            seq += 1
        return seq

    def add(self, chunk: Data) -> bool:
        """Keep a chunk; returns False for a duplicate. Raises for a chunk that cannot belong to the offer."""
        if chunk.transfer_id != self.offer.transfer_id:
            msg = "Chunk belongs to another transfer"
            raise ProtocolError(msg)
        if not 0 <= chunk.seq < self.offer.chunk_count:
            msg = f"Chunk {chunk.seq} is out of range"
            raise ProtocolError(msg)
        last = chunk.seq == self.offer.chunk_count - 1
        expected = self.offer.size - chunk.seq * self.offer.chunk_bytes if last else self.offer.chunk_bytes
        if len(chunk.payload) != expected:
            msg = f"Chunk {chunk.seq} has {len(chunk.payload)} bytes instead of {expected}"
            raise ProtocolError(msg)
        if chunk.seq in self.chunks:
            return False
        self.chunks[chunk.seq] = chunk.payload
        return True

    def ack(self) -> Ack:
        next_seq = self.next_seq
        window = {seq for seq in self.chunks if next_seq < seq <= next_seq + MAX_WINDOW}
        return Ack(self.offer.transfer_id, next_seq, frozenset(window))

    def assemble(self) -> bytes:
        """Return the original file; raises when chunks are missing or the hash does not match the offer."""
        if not self.complete:
            msg = "The transfer is not complete"
            raise ProtocolError(msg)
        body = b"".join(self.chunks[seq] for seq in range(self.offer.chunk_count))
        if self.offer.deflate:
            try:
                body = zlib.decompress(body)
            except zlib.error as e:
                msg = "The file could not be inflated"
                raise ProtocolError(msg) from e
        if file_hash(body) != self.offer.sha256_16:
            msg = "The file's hash does not match the offer"
            raise ProtocolError(msg)
        return body


def missing_after(ack: Ack, chunk_count: int, window: int) -> list[int]:
    """List what the sender sends after an ACK: the missing chunks of the window starting at ``ack.next_seq``."""
    return [seq for seq in range(ack.next_seq, min(ack.next_seq + window, chunk_count)) if seq not in ack.received]
