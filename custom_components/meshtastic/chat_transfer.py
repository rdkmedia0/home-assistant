# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

"""
File transfers of the chat: the sender and receiver of ``chat_files`` packets, their storage and the HTTP view.

One ``MeshtasticFileTransfers`` per gateway. Outgoing transfers run as tasks that offer the file, then send the
chunks the receiver still misses and wait for its acknowledgement before the next window (section 5 of
``FILE_TRANSFER_DESIGN.md``). Incoming transfers collect chunks, acknowledge them, and write the finished file under
Home Assistant's storage directory, where an authenticated view serves it to the chat panel. Both sides persist their
state so a transfer survives a restart of either Home Assistant.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import STORAGE_DIR, Store
from homeassistant.util.hass_dict import HassKey

from . import chat_files
from .aiomeshtastic.errors import ROUTING_ERROR_HINTS
from .api import MeshtasticApiClientError
from .chat_files import (
    FILE_TRANSFER_PORT,
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
    Prepared,
    ProtocolError,
    Reassembly,
    Reject,
    RejectReason,
)
from .const import (
    CHAT_FILES_STORAGE_VERSION,
    CHAT_FILES_URL,
    CHAT_STORAGE_SAVE_DELAY,
    CONF_OPTION_CHAT,
    CONF_OPTION_CHAT_FILES_ENABLE,
    CONF_OPTION_CHAT_FILES_ENABLE_DEFAULT,
    CONF_OPTION_CHAT_FILES_IMAGE_PX,
    CONF_OPTION_CHAT_FILES_IMAGE_PX_DEFAULT,
    CONF_OPTION_CHAT_FILES_MAX_KB,
    CONF_OPTION_CHAT_FILES_MAX_KB_DEFAULT,
    CONF_OPTION_CHAT_FILES_STORAGE_MB,
    CONF_OPTION_CHAT_FILES_STORAGE_MB_DEFAULT,
    CONF_OPTION_CHAT_FILES_WINDOW,
    CONF_OPTION_CHAT_FILES_WINDOW_DEFAULT,
    DOMAIN,
    EVENT_MESHTASTIC_CHAT_FILE_RECEIVED,
    EVENT_MESHTASTIC_CHAT_FILE_SENT,
    LOGGER,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from homeassistant.auth.models import User
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

    from .aiomeshtastic.interface import MeshNode
    from .aiomeshtastic.packet import Packet
    from .chat import MeshtasticChat
    from .data import MeshtasticConfigEntry

_LOGGER = LOGGER.getChild(__name__)

DATA_CHAT_FILE_VIEW_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_chat_file_view")
STORAGE_SUBDIR = f"{DOMAIN}.files"
MESSAGE_KIND_FILE = "file"
INFLATE_FACTOR = 4  # a deflated file may inflate to this many times the size cap, no more

# seconds of airtime of a full packet per modem preset (250 kHz, CR 4/5, 16 symbol preamble), approximate
AIRTIME_PER_PACKET = {
    "SHORT_TURBO": 0.1,
    "SHORT_FAST": 0.2,
    "SHORT_SLOW": 0.4,
    "MEDIUM_FAST": 0.65,
    "MEDIUM_SLOW": 1.3,
    "LONG_FAST": 2.2,
    "LONG_MODERATE": 4.5,
    "LONG_SLOW": 7.0,
    "VERY_LONG_SLOW": 20.0,
}
DEFAULT_AIRTIME = AIRTIME_PER_PACKET["LONG_FAST"]
ROUND_TRIP_OVERHEAD = 1.0  # seconds of processing and ack airtime per chunk, for the estimate shown to the user

LINK_ERRORS = frozenset({"MAX_RETRANSMIT", "NO_ROUTE", "TIMEOUT", "NO_RESPONSE", "GOT_NAK"})

REJECT_TEXTS = {
    RejectReason.DISABLED: "the node does not accept files",
    RejectReason.TOO_LARGE: "the file is larger than the node accepts",
    RejectReason.STORAGE_FULL: "the node has no storage left for files",
    RejectReason.BUSY: "the node is already receiving a file",
    RejectReason.DECLINED: "the file was declined",
    RejectReason.UNSUPPORTED_VERSION: "the node runs another version of the file transfer",
}
CANCEL_TEXTS = {
    CancelReason.USER: "cancelled by the other side",
    CancelReason.TIMEOUT: "the other side gave up waiting",
    CancelReason.ERROR: "the other side ran into an error",
}


class FileStatus(StrEnum):
    """Status of a file message; the values shared with text messages mean the same thing there."""

    OFFERING = "offering"  # waiting for the other side to accept
    OFFERED = "offered"  # incoming, waiting for a user here to accept
    SENDING = "sending"
    RECEIVING = "receiving"
    DELIVERED = "delivered"  # the other side has the whole file
    RECEIVED = "received"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


FINAL_STATUSES = frozenset(
    {FileStatus.DELIVERED, FileStatus.RECEIVED, FileStatus.FAILED, FileStatus.CANCELLED, FileStatus.REJECTED}
)


@dataclass
class Timing:
    """Every timeout and pause of the state machines; tests shrink them."""

    offer_timeout: float = 300  # how long the sender waits for ACCEPT or REJECT
    routing_ack_timeout: float = 60  # per packet, plus ``per_hop`` for every hop
    per_hop: float = 20
    app_ack_timeout: float = 30  # after the radio acknowledged a window, how long until the receiver's ACK
    done_timeout: float = 30
    link_retry_delay: float = 15
    link_retries: int = 5
    ack_misses: int = 5
    duty_cycle_backoff: tuple[float, ...] = (60, 120, 300, 600)
    chunk_gap_factor: float = 1.5  # minimum gap between chunks, in airtimes of a packet
    utilization_limit: float = 25.0  # percent; the firmware's own modules hold back above this
    utilization_recheck: float = 10
    utilization_max_wait: float = 300
    receiver_ack_delay: float = 2.0  # an incomplete window is acknowledged this long after its last chunk
    idle_timeout: float = 1800
    idle_check: float = 60


class TransferError(Exception):
    """A transfer that cannot go on; the text is shown to the user."""


def file_options(entry: ConfigEntry) -> dict[str, Any]:
    return entry.options.get(CONF_OPTION_CHAT, {})


def file_transfer_enabled(entry: ConfigEntry) -> bool:
    return bool(file_options(entry).get(CONF_OPTION_CHAT_FILES_ENABLE, CONF_OPTION_CHAT_FILES_ENABLE_DEFAULT))


def safe_file_name(name: str) -> str:
    """Reduce a file name from the mesh or the browser to a harmless basename."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name.replace("\\", "/")).name).strip("._")
    return (base or "file")[:NAME_MAX_BYTES]


def estimate_seconds(chunk_count: int, preset: str | None, hops: int | None) -> int:
    """Roughly how long a transfer takes on a preset over a number of hops, stop-and-wait, no duty cycle."""
    airtime = AIRTIME_PER_PACKET.get(preset or "", DEFAULT_AIRTIME)
    return int(chunk_count * (airtime * (1 + (hops or 0)) + ROUND_TRIP_OVERHEAD))


def _routing_text(error: str | None) -> str:
    if error is None:
        return "no acknowledgement from the mesh"
    hint = ROUTING_ERROR_HINTS.get(error)
    return f"{error} ({hint})" if hint else error


@dataclass
class Outgoing:
    transfer_id: int
    conversation: str
    message_id: str
    node: int
    prepared: Prepared
    path: str  # the file as stored here
    window: int = 1
    have: frozenset[int] = frozenset()
    answered: asyncio.Event = field(default_factory=asyncio.Event)
    answer: Accept | Reject | None = None
    acked: asyncio.Event = field(default_factory=asyncio.Event)
    last_ack: Ack | None = None
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    done: Done | None = None
    cancelled_by_peer: Cancel | None = None
    task: asyncio.Task | None = None
    last_activity: float = field(default_factory=time.time)
    hops: int = 0
    preset: str | None = None

    def touch(self) -> None:
        self.last_activity = time.time()


@dataclass
class Incoming:
    transfer_id: int
    conversation: str
    message_id: str
    node: int
    reassembly: Reassembly
    accepted: bool = False
    window: int = 1
    window_start: int = 0  # the sender's current window begins at the next_seq of the last ACK we sent
    ack_timer: asyncio.TimerHandle | None = None
    last_activity: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_activity = time.time()


class MeshtasticFileTransfers:
    """Sends and receives files for one gateway's chat."""

    def __init__(self, hass: HomeAssistant, entry: MeshtasticConfigEntry, chat: MeshtasticChat) -> None:
        self.hass = hass
        self.entry = entry
        self.chat = chat
        self.timing = Timing()
        self._store: Store[dict[str, Any]] = Store(
            hass, CHAT_FILES_STORAGE_VERSION, f"{DOMAIN}.chat_files.{entry.entry_id}", private=True
        )
        self._outgoing: dict[int, Outgoing] = {}
        self._incoming: dict[int, Incoming] = {}
        self._unsubscribe: CALLBACK_TYPE | None = None
        self._cancel_idle_check: CALLBACK_TYPE | None = None
        self._background: set[asyncio.Task] = set()
        self._directory = Path(hass.config.path(STORAGE_DIR, STORAGE_SUBDIR, entry.entry_id))

    # --- options -------------------------------------------------------------------------------------------------

    @property
    def max_file_bytes(self) -> int:
        return (
            int(file_options(self.entry).get(CONF_OPTION_CHAT_FILES_MAX_KB, CONF_OPTION_CHAT_FILES_MAX_KB_DEFAULT))
            * 1024
        )

    @property
    def storage_bytes(self) -> int:
        return (
            int(
                file_options(self.entry).get(
                    CONF_OPTION_CHAT_FILES_STORAGE_MB, CONF_OPTION_CHAT_FILES_STORAGE_MB_DEFAULT
                )
            )
            * 1024
            * 1024
        )

    @property
    def window(self) -> int:
        return max(
            1,
            min(
                MAX_WINDOW,
                int(file_options(self.entry).get(CONF_OPTION_CHAT_FILES_WINDOW, CONF_OPTION_CHAT_FILES_WINDOW_DEFAULT)),
            ),
        )

    @property
    def image_max_px(self) -> int:
        return int(
            file_options(self.entry).get(CONF_OPTION_CHAT_FILES_IMAGE_PX, CONF_OPTION_CHAT_FILES_IMAGE_PX_DEFAULT)
        )

    async def async_info(self) -> dict[str, Any]:
        """Tell the chat panel what it needs to know, including how long a packet takes on this gateway's preset."""
        preset = None
        try:
            preset = (
                (await self.entry.runtime_data.client.async_get_node_local_config()).get("lora", {}).get("modemPreset")
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not read the LoRa config for the estimate", exc_info=True)
        return {
            "enabled": True,
            "max_bytes": self.max_file_bytes,
            "window": self.window,
            "image_max_px": self.image_max_px,
            "chunk_bytes": chat_files.DEFAULT_CHUNK_BYTES,
            "preset": preset,
            "airtime": AIRTIME_PER_PACKET.get(preset or "", DEFAULT_AIRTIME),
            "overhead": ROUND_TRIP_OVERHEAD,
            "url": f"{CHAT_FILES_URL}/{self.entry.entry_id}",
        }

    # --- lifecycle -----------------------------------------------------------------------------------------------

    async def async_setup(self) -> None:
        data = await self._store.async_load() or {}
        await self.hass.async_add_executor_job(self._make_directory)
        for record in data.get("incoming", {}).values():
            try:
                self._restore_incoming(record)
            except (ProtocolError, KeyError, ValueError):
                _LOGGER.debug("Dropping an incoming transfer that cannot be restored", exc_info=True)
        for record in data.get("outgoing", {}).values():
            try:
                await self._restore_outgoing(record)
            except (ProtocolError, KeyError, ValueError, OSError):
                _LOGGER.debug("Dropping an outgoing transfer that cannot be restored", exc_info=True)
        self._unsubscribe = self.entry.runtime_data.client.add_data_listener(FILE_TRANSFER_PORT, self._on_packet)
        self._cancel_idle_check = async_track_time_interval(
            self.hass, self._check_idle, timedelta(seconds=self.timing.idle_check)
        )
        async_register_file_view(self.hass)
        for transfer in self._outgoing.values():
            self._start(transfer)

    async def async_unload(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._cancel_idle_check:
            self._cancel_idle_check()
            self._cancel_idle_check = None
        # nothing waits for the radio here: a transfer in flight is stored and resumes after the restart
        tasks = [t.task for t in self._outgoing.values() if t.task] + list(self._background)
        for task in tasks:
            task.cancel()
        for transfer in self._incoming.values():
            self._cancel_ack_timer(transfer)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._store.async_save(self._data_to_save())

    @staticmethod
    async def async_remove(hass: HomeAssistant, entry_id: str) -> None:
        """Remove the stored state and the files of a gateway that is being deleted."""
        await Store(hass, CHAT_FILES_STORAGE_VERSION, f"{DOMAIN}.chat_files.{entry_id}", private=True).async_remove()
        directory = Path(hass.config.path(STORAGE_DIR, STORAGE_SUBDIR, entry_id))

        def _remove() -> None:
            if directory.is_dir():
                for file in directory.iterdir():
                    file.unlink(missing_ok=True)
                directory.rmdir()

        await hass.async_add_executor_job(_remove)

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "outgoing": {
                str(t.transfer_id): {
                    "transfer_id": t.transfer_id,
                    "conversation": t.conversation,
                    "message_id": t.message_id,
                    "node": t.node,
                    "path": t.path,
                    "name": t.prepared.offer.name,
                    "mime": t.prepared.offer.mime,
                    "chunk_bytes": t.prepared.offer.chunk_bytes,
                }
                for t in self._outgoing.values()
            },
            "incoming": {
                str(t.transfer_id): {
                    "transfer_id": t.transfer_id,
                    "conversation": t.conversation,
                    "message_id": t.message_id,
                    "node": t.node,
                    "accepted": t.accepted,
                    "window": t.window,
                    "window_start": t.window_start,
                    "offer": base64.b64encode(t.reassembly.offer.encode()).decode(),
                    "chunks": {str(seq): base64.b64encode(data).decode() for seq, data in t.reassembly.chunks.items()},
                }
                for t in self._incoming.values()
            },
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, CHAT_STORAGE_SAVE_DELAY)

    def _restore_incoming(self, record: dict[str, Any]) -> None:
        offer = Offer.decode(base64.b64decode(record["offer"]))
        reassembly = Reassembly(offer)
        for seq, data in record.get("chunks", {}).items():
            reassembly.add(Data(offer.transfer_id, int(seq), base64.b64decode(data)))
        self._incoming[offer.transfer_id] = Incoming(
            transfer_id=offer.transfer_id,
            conversation=record["conversation"],
            message_id=record["message_id"],
            node=int(record["node"]),
            reassembly=reassembly,
            accepted=bool(record.get("accepted")),
            window=int(record.get("window", 1)),
            window_start=int(record.get("window_start", 0)),
        )

    async def _restore_outgoing(self, record: dict[str, Any]) -> None:
        path = Path(record["path"])
        if path.parent != self._directory:
            msg = "Stored file is outside the gateway's directory"
            raise ValueError(msg)
        data = await self.hass.async_add_executor_job(path.read_bytes)
        prepared = chat_files.prepare(
            data,
            transfer_id=int(record["transfer_id"]),
            name=record["name"],
            mime=record.get("mime", ""),
            chunk_bytes=int(record.get("chunk_bytes", chat_files.DEFAULT_CHUNK_BYTES)),
        )
        self._outgoing[prepared.offer.transfer_id] = Outgoing(
            transfer_id=prepared.offer.transfer_id,
            conversation=record["conversation"],
            message_id=record["message_id"],
            node=int(record["node"]),
            prepared=prepared,
            path=str(path),
        )

    # --- messages in the chat ------------------------------------------------------------------------------------

    def _message(self, conversation: str, message_id: str) -> dict[str, Any] | None:
        return self.chat.find_message(conversation, message_id=message_id)

    def _update(self, transfer: Outgoing | Incoming, status: str | None = None, **file_fields: Any) -> None:
        message = self._message(transfer.conversation, transfer.message_id)
        if message is None:
            return
        if status is not None:
            message["status"] = status
        message["file"].update(file_fields)
        self.chat.publish_update(transfer.conversation, message)

    def _file_message(  # noqa: PLR0913
        self,
        *,
        direction: str,
        node: int,
        offer: Offer,
        original_size: int,
        status: str,
        user: User | None = None,
    ) -> dict[str, Any]:
        gateway_id = self.chat.gateway_id or 0
        sender = gateway_id if direction == "out" else node
        long_name, short_name, user_id = self.chat.node_names(sender)
        return {
            "id": uuid.uuid4().hex,
            "packet_id": None,
            "reply_to": None,
            "reply_preview": None,
            "ts": time.time(),
            "direction": direction,
            "from": sender,
            "from_user_id": user_id,
            "from_name": long_name,
            "from_short": short_name,
            "to": node if direction == "out" else None,
            "text": f"\U0001f4ce {offer.name}",
            "kind": MESSAGE_KIND_FILE,
            "status": status,
            "sent_by": user.name if user else None,
            "prefix": None,
            "file": {
                "transfer_id": offer.transfer_id,
                "name": offer.name,
                "mime": offer.mime,
                "size": original_size,
                "wire_size": offer.size,
                "chunk_count": offer.chunk_count,
                "chunks_done": 0,
                "image": offer.image,
                "compressed": offer.deflate,
                "url": None,
                "path": None,
                "error": None,
                "note": None,
            },
        }

    def _file_url(self, transfer_id: int) -> str:
        return f"{CHAT_FILES_URL}/{self.entry.entry_id}/{transfer_id}"

    def stored_file(self, transfer_id: int) -> dict[str, Any] | None:
        """Find the file message of a transfer whose file is on disk, for the HTTP view."""
        for message in self.chat.file_messages():
            file = message["file"]
            if file["transfer_id"] == transfer_id and file.get("path"):
                return message
        return None

    def async_discard_messages(self, messages: list[dict[str, Any]]) -> None:
        """Messages left the history: stop their transfers and delete their files."""
        paths = []
        for message in messages:
            if message.get("kind") != MESSAGE_KIND_FILE:
                continue
            transfer_id = message["file"]["transfer_id"]
            if (outgoing := self._outgoing.get(transfer_id)) is not None:
                self._background_task(self._cancel_outgoing(outgoing, CancelReason.USER, notify=True))
            if (incoming := self._incoming.get(transfer_id)) is not None:
                self._background_task(self._cancel_incoming(incoming, CancelReason.USER, notify=True))
            if message["file"].get("path"):
                paths.append(message["file"]["path"])
        if paths:
            self._background_task(self._async_unlink_all(paths))

    async def _async_unlink_all(self, paths: list[str]) -> None:
        await self.hass.async_add_executor_job(self._unlink_all, paths)

    def _unlink_all(self, paths: list[str]) -> None:
        for path in paths:
            file = Path(path)
            if file.parent == self._directory:
                file.unlink(missing_ok=True)

    # --- storage -------------------------------------------------------------------------------------------------

    def _path_for(self, transfer_id: int, name: str) -> Path:
        return self._directory / f"{transfer_id:08x}_{safe_file_name(name)}"

    async def _async_store(self, transfer_id: int, name: str, data: bytes) -> Path:
        path = self._path_for(transfer_id, name)

        def _write() -> None:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data)

        await self.hass.async_add_executor_job(_write)
        return path

    def _make_directory(self) -> None:
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _usage(self) -> int:
        if not self._directory.is_dir():
            return 0
        return sum(file.stat().st_size for file in self._directory.iterdir() if file.is_file())

    async def _async_usage(self) -> int:
        return await self.hass.async_add_executor_job(self._usage)

    # --- sending -------------------------------------------------------------------------------------------------

    async def async_send(
        self,
        conversation: str,
        data: bytes,
        *,
        name: str,
        mime: str = "",
        user: User | None = None,
    ) -> dict[str, Any]:
        """Offer a file to the node of a direct message conversation; the transfer then runs in the background."""
        kind, node = self.chat.parse_conversation(conversation)
        if kind != "node":
            msg = "Files can only be sent in a direct message"
            raise vol.Invalid(msg)
        if not data:
            msg = "The file is empty"
            raise vol.Invalid(msg)
        if len(data) > self.max_file_bytes:
            msg = f"The file is larger than {self.max_file_bytes // 1024} kB"
            raise vol.Invalid(msg)
        name = safe_file_name(name)
        if any(t.node == node for t in self._outgoing.values()):
            msg = "A file is already being sent to this node"
            raise vol.Invalid(msg)
        if await self._async_usage() + len(data) > self.storage_bytes:
            msg = "The file storage of this gateway is full"
            raise vol.Invalid(msg)

        transfer_id = secrets.randbits(32)
        while transfer_id in self._outgoing or transfer_id in self._incoming:
            transfer_id = secrets.randbits(32)
        try:
            prepared = chat_files.prepare(data, transfer_id=transfer_id, name=name, mime=mime)
        except ProtocolError as e:
            raise vol.Invalid(str(e)) from e
        path = await self._async_store(transfer_id, name, data)

        message = self._file_message(
            direction="out",
            node=node,
            offer=prepared.offer,
            original_size=len(data),
            status=FileStatus.OFFERING,
            user=user,
        )
        message["file"]["path"] = str(path)
        message["file"]["url"] = self._file_url(transfer_id)
        transfer = Outgoing(
            transfer_id=transfer_id,
            conversation=conversation,
            message_id=message["id"],
            node=node,
            prepared=prepared,
            path=str(path),
        )
        self._outgoing[transfer_id] = transfer
        self.chat.add_message(conversation, message)
        self._save()
        self._start(transfer)
        return dict(message)

    def _start(self, transfer: Outgoing) -> None:
        transfer.task = self.hass.async_create_background_task(
            self._run_outgoing(transfer), name=f"{DOMAIN}_chat_file_{transfer.transfer_id:08x}"
        )

    async def _run_outgoing(self, transfer: Outgoing) -> None:
        try:
            await self._learn_link(transfer)
            await self._offer(transfer)
            await self._send_body(transfer)
        except TransferError as e:
            status = FileStatus.REJECTED if isinstance(transfer.answer, Reject) else FileStatus.FAILED
            if transfer.cancelled_by_peer is not None:
                status = FileStatus.CANCELLED
            self._finish_outgoing(transfer, status, error=str(e))
            return
        except asyncio.CancelledError:
            # unloading: the transfer stays stored and resumes after the restart
            raise
        except Exception:
            _LOGGER.exception("File transfer %08x failed", transfer.transfer_id)
            self._finish_outgoing(transfer, FileStatus.FAILED, error="internal error, see the log")
            return
        self._finish_outgoing(transfer, FileStatus.DELIVERED)

    async def _learn_link(self, transfer: Outgoing) -> None:
        """Read the modem preset and the hops to the node, for pacing and the estimate."""
        client = self.entry.runtime_data.client
        try:
            lora = (await client.async_get_node_local_config()).get("lora", {})
            transfer.preset = lora.get("modemPreset")
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not read the LoRa config for pacing", exc_info=True)
        node = client.get_node_info(transfer.node)
        hops = getattr(node, "hops_away", None)
        transfer.hops = int(hops) if isinstance(hops, int) else 0
        self._update(
            transfer,
            estimate_seconds=estimate_seconds(transfer.prepared.offer.chunk_count, transfer.preset, transfer.hops),
        )

    async def _offer(self, transfer: Outgoing) -> None:
        self._update(transfer, FileStatus.OFFERING, note=None)
        offer = transfer.prepared.offer
        _LOGGER.debug(
            "Offering %s (%s, %d bytes, %d chunks) to %08x as transfer %08x",
            offer.name,
            offer.mime,
            offer.size,
            offer.chunk_count,
            transfer.node,
            transfer.transfer_id,
        )
        await self._send_reliable(transfer, offer.encode())
        _LOGGER.debug("Offer %08x left the radio, waiting for the answer of %08x", transfer.transfer_id, transfer.node)
        try:
            await asyncio.wait_for(transfer.answered.wait(), self.timing.offer_timeout)
        except TimeoutError:
            _LOGGER.debug("Offer %08x was not answered by %08x", transfer.transfer_id, transfer.node)
            msg = "No Mesh Chat with file transfer answered at this node"
            raise TransferError(msg) from None
        self._raise_if_cancelled(transfer)
        answer = transfer.answer
        if isinstance(answer, Reject):
            msg = REJECT_TEXTS.get(answer.reason, f"rejected ({answer.reason})")
            raise TransferError(msg)
        if not isinstance(answer, Accept):  # pragma: no cover - answered is only set with an answer
            msg = "No answer"
            raise TransferError(msg)
        transfer.window = max(1, min(MAX_WINDOW, answer.window))
        transfer.have = answer.have

    def _ack_from_have(self, transfer: Outgoing) -> Ack:
        """Turn what the receiver told us it holds into the ACK the sending loop starts from."""
        count = transfer.prepared.offer.chunk_count
        next_seq = 0
        while next_seq in transfer.have:
            next_seq += 1
        received = {seq for seq in transfer.have if next_seq < seq <= next_seq + MAX_WINDOW}
        return Ack(transfer.transfer_id, min(next_seq, count), frozenset(received))

    async def _send_body(self, transfer: Outgoing) -> None:
        count = transfer.prepared.offer.chunk_count
        ack = self._ack_from_have(transfer)
        misses = 0
        self._update(transfer, FileStatus.SENDING, chunks_done=ack.next_seq, note=None)
        while ack.next_seq < count:
            transfer.acked.clear()
            for seq in chat_files.missing_after(ack, count, transfer.window):
                await self._pace(transfer)
                await self._send_reliable(transfer, transfer.prepared.chunk(seq).encode())
            try:
                await asyncio.wait_for(transfer.acked.wait(), self.timing.app_ack_timeout)
            except TimeoutError:
                self._raise_if_cancelled(transfer)
                misses += 1
                if misses >= self.timing.ack_misses:
                    msg = "The Mesh Chat at this node stopped answering"
                    raise TransferError(msg) from None
                self._update(transfer, note="no answer from the other side, sending again")
                continue
            self._raise_if_cancelled(transfer)
            misses = 0
            ack = transfer.last_ack or ack
            self._update(transfer, chunks_done=ack.next_seq, note=None)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(transfer.finished.wait(), self.timing.done_timeout)
        self._raise_if_cancelled(transfer)
        if transfer.done is not None and transfer.done.result != DoneResult.OK:
            msg = "The file arrived damaged at the other side"
            raise TransferError(msg)

    def _raise_if_cancelled(self, transfer: Outgoing) -> None:
        if transfer.cancelled_by_peer is not None:
            raise TransferError(CANCEL_TEXTS.get(transfer.cancelled_by_peer.reason, "cancelled by the other side"))

    async def _pace(self, transfer: Outgoing) -> None:
        """Keep the gap between chunks and hold back while the channel is busy."""
        gap = self.timing.chunk_gap_factor * AIRTIME_PER_PACKET.get(transfer.preset or "", DEFAULT_AIRTIME)
        if gap > 0:
            await asyncio.sleep(gap)
        waited = 0.0
        while (utilization := self._channel_utilization()) is not None and utilization > self.timing.utilization_limit:
            if waited >= self.timing.utilization_max_wait:
                break
            self._update(transfer, note=f"waiting, the channel is {utilization:.0f}% busy")
            await asyncio.sleep(self.timing.utilization_recheck)
            waited += self.timing.utilization_recheck

    def _channel_utilization(self) -> float | None:
        coordinator = getattr(self.entry.runtime_data, "coordinator", None)
        gateway_id = self.chat.gateway_id
        if coordinator is None or gateway_id is None or not coordinator.data:
            return None
        value = coordinator.data.get(gateway_id, {}).get("deviceMetrics", {}).get("channelUtilization")
        return float(value) if value is not None else None

    async def _send_reliable(self, transfer: Outgoing, payload: bytes) -> None:
        """Send one packet with ``want_ack`` and act on what the radio reports (section 5 of the design)."""
        client = self.entry.runtime_data.client
        timeout = self.timing.routing_ack_timeout + self.timing.per_hop * transfer.hops
        link_attempts = 0
        duty_cycle_waits = 0
        while True:
            transfer.touch()
            try:
                ack_from, error = await client.send_data_with_ack(
                    payload, transfer.node, port_num=FILE_TRANSFER_PORT, ack_timeout=timeout
                )
            except MeshtasticApiClientError as e:
                raise TransferError(str(e)) from e
            if error == "NONE":
                return
            self._raise_if_cancelled(transfer)
            if error == "DUTY_CYCLE_LIMIT":
                delay = self.timing.duty_cycle_backoff[min(duty_cycle_waits, len(self.timing.duty_cycle_backoff) - 1)]
                duty_cycle_waits += 1
                self._update(transfer, note=f"waiting for airtime ({_routing_text(error)}), retry in {int(delay)} s")
                await asyncio.sleep(delay)
                continue
            if error is None or error in LINK_ERRORS:
                link_attempts += 1
                if link_attempts >= self.timing.link_retries:
                    raise TransferError(_routing_text(error))
                self._update(transfer, note=f"{_routing_text(error)}, retrying")
                await asyncio.sleep(self.timing.link_retry_delay)
                continue
            raise TransferError(_routing_text(error))

    def _finish_outgoing(self, transfer: Outgoing, status: str, *, error: str | None = None) -> None:
        self._outgoing.pop(transfer.transfer_id, None)
        if status == FileStatus.DELIVERED:
            self._update(transfer, status, error=None, note=None, chunks_done=transfer.prepared.offer.chunk_count)
        else:
            self._update(transfer, status, error=error, note=None)
        self._save()
        message = self._message(transfer.conversation, transfer.message_id)
        if message is None:
            return
        self.hass.bus.async_fire(
            EVENT_MESHTASTIC_CHAT_FILE_SENT,
            {
                "config_entry_id": self.entry.entry_id,
                "gateway": self.chat.gateway_id,
                "to": transfer.node,
                "message_id": message["id"],
                "name": transfer.prepared.offer.name,
                "size": transfer.prepared.original_size,
                "mime": transfer.prepared.offer.mime,
                "path": transfer.path,
                "url": self._file_url(transfer.transfer_id),
                "status": status,
                "error": error,
            },
        )
        if status == FileStatus.DELIVERED:
            self.chat.log_file(message, transfer.node)

    async def _cancel_outgoing(self, transfer: Outgoing, reason: CancelReason, *, notify: bool) -> None:
        self._outgoing.pop(transfer.transfer_id, None)
        if transfer.task and transfer.task is not asyncio.current_task():
            transfer.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await transfer.task
        self._update(transfer, FileStatus.CANCELLED, note=None)
        self._save()
        if notify:
            await self._send_control(transfer.node, Cancel(transfer.transfer_id, reason).encode())

    async def async_cancel(self, message_id: str) -> None:
        """Stop the transfer behind a file message, from either side."""
        for transfer in list(self._outgoing.values()):
            if transfer.message_id == message_id:
                await self._cancel_outgoing(transfer, CancelReason.USER, notify=True)
                return
        for transfer in list(self._incoming.values()):
            if transfer.message_id == message_id:
                await self._cancel_incoming(transfer, CancelReason.USER, notify=True)
                return
        msg = "No transfer is running for this message"
        raise vol.Invalid(msg)

    # --- receiving -----------------------------------------------------------------------------------------------

    async def _on_packet(self, node: MeshNode, packet: Packet[bytes]) -> None:  # noqa: PLR0912
        payload = packet.app_payload
        if not isinstance(payload, bytes) or not chat_files.is_file_transfer_packet(payload):
            return
        if packet.to_id != self.chat.gateway_id:
            return  # not for us (private app packets on a channel, for example)
        try:
            kind = chat_files.packet_type(payload)
            transfer_id = chat_files.transfer_id_of(payload)
            _LOGGER.debug(
                "File transfer packet %s of %08x from %08x, %d bytes", kind, transfer_id, node.id, len(payload)
            )
            if kind == chat_files.PacketType.OFFER:
                await self._on_offer(node.id, Offer.decode(payload))
            elif kind == chat_files.PacketType.ACCEPT:
                if (outgoing := self._outgoing.get(transfer_id)) is not None and outgoing.node == node.id:
                    self._on_answer(outgoing, Accept.decode(payload, outgoing.prepared.offer.chunk_count))
            elif kind == chat_files.PacketType.REJECT:
                if (outgoing := self._outgoing.get(transfer_id)) is not None and outgoing.node == node.id:
                    self._on_answer(outgoing, Reject.decode(payload))
            elif kind == chat_files.PacketType.DATA:
                if (incoming := self._incoming.get(transfer_id)) is not None and incoming.node == node.id:
                    await self._on_data(incoming, Data.decode(payload))
            elif kind == chat_files.PacketType.ACK:
                if (outgoing := self._outgoing.get(transfer_id)) is not None and outgoing.node == node.id:
                    self._on_ack(outgoing, Ack.decode(payload))
            elif kind == chat_files.PacketType.DONE:
                if (outgoing := self._outgoing.get(transfer_id)) is not None and outgoing.node == node.id:
                    self._on_done(outgoing, Done.decode(payload))
            elif kind == chat_files.PacketType.CANCEL:
                await self._on_cancel(node.id, transfer_id, Cancel.decode(payload))
        except ProtocolError:
            _LOGGER.debug("Ignoring a malformed file transfer packet from %08x", node.id, exc_info=True)

    def _on_answer(self, transfer: Outgoing, answer: Accept | Reject) -> None:
        if transfer.answered.is_set():
            return
        transfer.answer = answer
        transfer.touch()
        transfer.answered.set()

    def _on_ack(self, transfer: Outgoing, ack: Ack) -> None:
        if transfer.last_ack is not None and ack.next_seq < transfer.last_ack.next_seq:
            return  # a late ACK of an earlier window
        transfer.last_ack = ack
        transfer.touch()
        transfer.acked.set()

    def _on_done(self, transfer: Outgoing, done: Done) -> None:
        transfer.done = done
        transfer.touch()
        if done.result == DoneResult.OK:
            # the other side has everything, whether or not its last ACK reached us
            self._on_ack(transfer, Ack(transfer.transfer_id, transfer.prepared.offer.chunk_count))
        transfer.finished.set()

    async def _on_cancel(self, node: int, transfer_id: int, cancel: Cancel) -> None:
        if (outgoing := self._outgoing.get(transfer_id)) is not None and outgoing.node == node:
            outgoing.cancelled_by_peer = cancel
            outgoing.touch()
            # wake whatever the sending task waits for; it raises the cancellation itself
            outgoing.answered.set()
            outgoing.acked.set()
            outgoing.finished.set()
        elif (incoming := self._incoming.get(transfer_id)) is not None and incoming.node == node:
            await self._cancel_incoming(incoming, cancel.reason, notify=False)

    async def _on_offer(self, node: int, offer: Offer) -> None:
        existing = self._incoming.get(offer.transfer_id)
        if existing is not None:
            if existing.node != node:
                await self._send_control(node, Reject(offer.transfer_id, RejectReason.BUSY).encode())
                return
            # the sender restarted: tell it what we already have
            existing.touch()
            await self._send_accept(existing)
            return
        reason = await self._reject_reason(node, offer)
        if reason is not None:
            await self._send_control(node, Reject(offer.transfer_id, reason).encode())
            return
        message = self._file_message(
            direction="in",
            node=node,
            offer=offer,
            original_size=offer.size,
            status=FileStatus.RECEIVING,
        )
        transfer = Incoming(
            transfer_id=offer.transfer_id,
            conversation=self.chat.node_conversation(node),
            message_id=message["id"],
            node=node,
            reassembly=Reassembly(offer),
            window=self.window,
        )
        self._incoming[offer.transfer_id] = transfer
        self.chat.add_message(transfer.conversation, message)
        self._save()
        # nobody has to be watching: the gateway accepts every offer that passes the size and storage limits
        await self._accept(transfer)

    async def _reject_reason(self, node: int, offer: Offer) -> RejectReason | None:
        if offer.version > chat_files.PROTOCOL_VERSION:
            return RejectReason.UNSUPPORTED_VERSION
        if offer.size > self.max_file_bytes or offer.chunk_bytes > chat_files.MAX_CHUNK_BYTES:
            return RejectReason.TOO_LARGE
        if any(t.node == node for t in self._incoming.values()):
            return RejectReason.BUSY
        if await self._async_usage() + offer.size * (INFLATE_FACTOR if offer.deflate else 1) > self.storage_bytes:
            return RejectReason.STORAGE_FULL
        return None

    async def _accept(self, transfer: Incoming) -> None:
        transfer.accepted = True
        transfer.window_start = transfer.reassembly.next_seq
        self._update(transfer, FileStatus.RECEIVING)
        self._save()
        await self._send_accept(transfer)

    async def _send_accept(self, transfer: Incoming) -> None:
        transfer.window_start = transfer.reassembly.next_seq
        accept = Accept(
            transfer.transfer_id,
            window=transfer.window,
            have=transfer.reassembly.have,
            chunk_count=transfer.reassembly.offer.chunk_count,
        )
        await self._send_control(transfer.node, accept.encode())

    async def _on_data(self, transfer: Incoming, chunk: Data) -> None:
        if not transfer.accepted:
            return
        transfer.touch()
        added = transfer.reassembly.add(chunk)
        if added:
            self._update(transfer, chunks_done=len(transfer.reassembly.chunks))
            self._save()
        if transfer.reassembly.complete:
            self._cancel_ack_timer(transfer)
            await self._complete(transfer)
            return
        count = transfer.reassembly.offer.chunk_count
        window = range(transfer.window_start, min(transfer.window_start + transfer.window, count))
        window_complete = all(seq in transfer.reassembly.chunks for seq in window)
        if not added or window_complete:
            # a duplicate means our ACK was lost; a complete window is what the sender waits for
            await self._send_ack(transfer)
        elif transfer.ack_timer is None:
            loop = asyncio.get_running_loop()
            transfer.ack_timer = loop.call_later(
                self.timing.receiver_ack_delay, lambda: self._background_task(self._send_ack(transfer))
            )

    def _cancel_ack_timer(self, transfer: Incoming) -> None:
        if transfer.ack_timer is not None:
            transfer.ack_timer.cancel()
            transfer.ack_timer = None

    async def _send_ack(self, transfer: Incoming) -> None:
        self._cancel_ack_timer(transfer)
        if transfer.transfer_id not in self._incoming:
            return
        ack = transfer.reassembly.ack()
        transfer.window_start = ack.next_seq
        await self._send_control(transfer.node, ack.encode())

    async def _complete(self, transfer: Incoming) -> None:
        self._incoming.pop(transfer.transfer_id, None)
        offer = transfer.reassembly.offer
        try:
            data = transfer.reassembly.assemble(max_size=self.max_file_bytes * INFLATE_FACTOR)
        except ProtocolError as e:
            self._update(transfer, FileStatus.FAILED, error=str(e))
            self._save()
            await self._send_control(transfer.node, Done(transfer.transfer_id, DoneResult.HASH_MISMATCH).encode())
            return
        path = await self._async_store(transfer.transfer_id, offer.name, data)
        self._update(
            transfer,
            FileStatus.RECEIVED,
            chunks_done=offer.chunk_count,
            size=len(data),
            path=str(path),
            url=self._file_url(transfer.transfer_id),
        )
        self._save()
        await self._send_control(transfer.node, transfer.reassembly.ack().encode())
        await self._send_control(transfer.node, Done(transfer.transfer_id, DoneResult.OK).encode())
        message = self._message(transfer.conversation, transfer.message_id)
        if message is None:
            return
        self.hass.bus.async_fire(
            EVENT_MESHTASTIC_CHAT_FILE_RECEIVED,
            {
                "config_entry_id": self.entry.entry_id,
                "gateway": self.chat.gateway_id,
                "from": transfer.node,
                "message_id": message["id"],
                "name": offer.name,
                "size": len(data),
                "mime": offer.mime,
                "path": str(path),
                "url": self._file_url(transfer.transfer_id),
            },
        )
        self.chat.log_file(message, transfer.node)

    async def _cancel_incoming(self, transfer: Incoming, reason: CancelReason | int, *, notify: bool) -> None:
        self._cancel_ack_timer(transfer)
        self._incoming.pop(transfer.transfer_id, None)
        status = FileStatus.CANCELLED
        self._update(
            transfer, status, error=None if notify else CANCEL_TEXTS.get(reason, "cancelled by the other side")
        )
        self._save()
        if notify:
            await self._send_control(transfer.node, Cancel(transfer.transfer_id, reason).encode())

    async def _send_control(self, node: int, payload: bytes) -> None:
        """Send an answer once; a lost one is repaired by the sender's retry of what it answered."""
        client = self.entry.runtime_data.client
        try:
            _, error = await client.send_data_with_ack(
                payload, node, port_num=FILE_TRANSFER_PORT, ack_timeout=self.timing.routing_ack_timeout
            )
        except MeshtasticApiClientError:
            _LOGGER.debug("Could not send a file transfer packet to %08x", node, exc_info=True)
            return
        if error not in (None, "NONE"):
            _LOGGER.debug("File transfer packet to %08x: %s", node, _routing_text(error))

    # --- housekeeping --------------------------------------------------------------------------------------------

    async def _check_idle(self, _now: Any = None) -> None:
        limit = time.time() - self.timing.idle_timeout
        for transfer in [t for t in self._outgoing.values() if t.last_activity < limit]:
            self._outgoing.pop(transfer.transfer_id, None)
            if transfer.task:
                transfer.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await transfer.task
            self._update(transfer, FileStatus.FAILED, error="timed out", note=None)
            self._save()
            await self._send_control(transfer.node, Cancel(transfer.transfer_id, CancelReason.TIMEOUT).encode())
        for transfer in [t for t in self._incoming.values() if t.last_activity < limit]:
            await self._cancel_incoming(transfer, CancelReason.TIMEOUT, notify=True)
            self._update(transfer, FileStatus.FAILED, error="timed out")

    def _background_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = self.hass.async_create_background_task(coro, name=f"{DOMAIN}_chat_file_task")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task


# --- the HTTP view -------------------------------------------------------------------------------------------------


def async_register_file_view(hass: HomeAssistant) -> None:
    if hass.data.get(DATA_CHAT_FILE_VIEW_REGISTERED) or hass.http is None:
        return
    hass.data[DATA_CHAT_FILE_VIEW_REGISTERED] = True
    hass.http.register_view(MeshtasticChatFileView())


class MeshtasticChatFileView(HomeAssistantView):
    """Serve a received or sent file to a user who may use the chat of its gateway."""

    url = CHAT_FILES_URL + "/{entry_id}/{transfer_id}"
    name = "api:meshtastic:chat:file"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, transfer_id: str) -> web.StreamResponse:
        from .chat import DATA_CHAT

        hass: HomeAssistant = request.app[KEY_HASS]
        chat = hass.data.get(DATA_CHAT, {}).get(entry_id)
        if chat is None or chat.files is None:
            raise web.HTTPNotFound
        user = request[KEY_HASS_USER]
        if chat.requires_admin and not user.is_admin:
            raise web.HTTPForbidden
        try:
            message = chat.files.stored_file(int(transfer_id))
        except ValueError:
            message = None
        if message is None:
            raise web.HTTPNotFound
        file = message["file"]
        path = Path(file["path"])
        if not await hass.async_add_executor_job(path.is_file):
            raise web.HTTPNotFound
        mime = file.get("mime") or ""
        image = file.get("image") and mime.lower().startswith("image/") and "/" in mime
        disposition = "inline" if image else "attachment"
        name = safe_file_name(file.get("name") or "file")
        headers = {
            "Content-Type": mime if image else "application/octet-stream",
            "Content-Disposition": f'{disposition}; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=86400",
        }
        return web.FileResponse(path, headers=headers)
