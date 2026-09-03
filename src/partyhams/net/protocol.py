"""Wire protocol for peer-to-peer log sync.

JSON over UDP — deliberately human-readable so the protocol is inspectable
(a design principle). Every datagram is one :class:`Message` wrapped in an
envelope carrying the protocol version, the event's network name (so multiple
events on one LAN don't cross-talk), and the sender's station id.

A change to a QSO — add, edit, or delete — is always a single :class:`QsoMessage`
carrying the *full* QSO (with its ``lamport`` and ``deleted`` flag). Merge is an
idempotent upsert keyed by ``uuid``; there is no separate delete message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from partyhams.core.models import QSO, Mode
from partyhams.hunters.models import HunterRecord

PROTOCOL_VERSION = 1


# --------------------------------------------------------------------------- #
# QSO <-> wire dict
# --------------------------------------------------------------------------- #
def qso_to_wire(qso: QSO) -> dict:
    return {
        "uuid": qso.uuid,
        "station_id": qso.station_id,
        "operator": qso.operator,
        "station_callsign": qso.station_callsign,
        "lamport": qso.lamport,
        "deleted": qso.deleted,
        "call": qso.call,
        "timestamp": qso.timestamp.isoformat(),
        "freq_hz": qso.freq_hz,
        "mode": qso.mode.value,
        "rst_sent": qso.rst_sent,
        "rst_rcvd": qso.rst_rcvd,
        "serial_sent": qso.serial_sent,
        "exchange_rcvd": qso.exchange_rcvd,
        "exchange_sent": qso.exchange_sent,
        "comment": qso.comment,
    }


def qso_from_wire(d: dict) -> QSO:
    return QSO(
        uuid=d["uuid"],
        station_id=d["station_id"],
        operator=d["operator"],
        station_callsign=d.get("station_callsign", ""),  # older peers omit it
        lamport=d["lamport"],
        deleted=d["deleted"],
        call=d["call"],
        timestamp=datetime.fromisoformat(d["timestamp"]),
        freq_hz=d["freq_hz"],
        mode=Mode(d["mode"]),
        rst_sent=d["rst_sent"],
        rst_rcvd=d["rst_rcvd"],
        serial_sent=d["serial_sent"],
        exchange_rcvd=dict(d["exchange_rcvd"]),
        exchange_sent=dict(d["exchange_sent"]),
        comment=d.get("comment", ""),  # older peers omit it
    )


# --------------------------------------------------------------------------- #
# POTA hunter record <-> wire dict
# --------------------------------------------------------------------------- #
def hunter_to_wire(r: HunterRecord) -> dict:
    return {
        "call": r.call,
        "station_id": r.station_id,
        "name": r.name,
        "last_worked": r.last_worked.isoformat(),
        "freq_hz": r.freq_hz,
        "band": r.band,
        "mode": r.mode,
        "worked_count": r.worked_count,
        "lamport": r.lamport,
    }


def hunter_from_wire(d: dict) -> HunterRecord:
    return HunterRecord(
        call=d["call"],
        station_id=d["station_id"],
        name=d.get("name", ""),
        last_worked=datetime.fromisoformat(d["last_worked"]),
        freq_hz=int(d.get("freq_hz", 0)),
        band=d.get("band", ""),
        mode=d.get("mode", ""),
        worked_count=int(d.get("worked_count", 0)),
        lamport=int(d.get("lamport", 0)),
    )

# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
@dataclass
class Hello:
    """Announce presence on join, advertising what this station has already seen."""

    operator: str
    call: str
    high_water: dict[str, int] = field(default_factory=dict)  # station_id -> max lamport
    type: str = "hello"


@dataclass
class QsoMessage:
    """An add/edit/delete of one QSO (full record)."""

    qso: QSO
    type: str = "qso"


@dataclass
class SyncRequest:
    """Ask peers for everything past the given per-station high-water marks."""

    high_water: dict[str, int] = field(default_factory=dict)
    type: str = "sync_request"


@dataclass
class SyncResponse:
    """A batch of QSOs answering a :class:`SyncRequest`."""

    qsos: list[QSO] = field(default_factory=list)
    type: str = "sync_response"


@dataclass
class FullLogRequest:
    """Ask every peer to send its *entire* log (anti-entropy from scratch).

    Unlike :class:`SyncRequest`, which only fetches records past a high-water
    mark, this asks for a complete copy — used by the "request all logs" button
    and on (re)join so a station ends up with everyone's QSOs.
    """

    type: str = "full_log_request"


@dataclass
class Heartbeat:
    """Periodic liveness + divergence detector (``log_hash`` compares logs).

    ``sender_utc`` advertises the sender's current UTC time (ISO-8601) so peers
    can flag a station whose clock has drifted (see ``partyhams.net.clocksync``).
    Defaults to ``""`` for back-compat with builds that predate clock-sync.
    """

    count: int
    log_hash: str
    lamport_max: int
    sender_utc: str = ""  # ISO-8601 UTC; "" => not advertised (older peer)
    type: str = "heartbeat"


@dataclass
class StationStatus:
    """Presence + current operating state of a station (for the network panel).

    ``power_w`` / ``swr`` are the station's last-known transmit power (watts) and
    SWR; ``0`` means "unknown" (default, for back-compat with older peers). For
    FT8/FT4, ``ft_tx_even`` says which sequence the station transmits on:
    ``-1`` unknown, ``0`` odd, ``1`` even.
    """

    operator: str
    call: str
    freq_hz: int
    mode: str
    power_w: float = 0.0  # 0 => unknown
    swr: float = 0.0  # 0 => unknown
    ft_tx_even: int = -1  # -1 unknown, 0 odd, 1 even
    type: str = "status"


@dataclass
class Chat:
    """A chat message. ``to_op`` empty or ``"*"`` means everyone.

    ``uuid`` + ``station_id`` give each message a stable identity so it can be
    persisted, deduped, and synced across machines just like a QSO.
    """

    from_op: str
    to_op: str
    text: str
    ts: str  # ISO-8601 UTC
    uuid: str = ""
    station_id: str = ""
    type: str = "chat"


@dataclass
class ChatSyncResponse:
    """A batch of chat messages answering a :class:`FullLogRequest`."""

    chats: list[Chat] = field(default_factory=list)
    type: str = "chat_sync_response"


@dataclass
class HunterMessage:
    """An add/update of one station's POTA hunter-roster record."""

    hunter: HunterRecord
    type: str = "pota_hunter"


@dataclass
class HunterSyncResponse:
    """A batch of hunter records answering a :class:`FullLogRequest`."""

    hunters: list[HunterRecord] = field(default_factory=list)
    type: str = "pota_hunter_sync_response"


Message = (
    Hello
    | QsoMessage
    | SyncRequest
    | SyncResponse
    | FullLogRequest
    | Heartbeat
    | StationStatus
    | Chat
    | ChatSyncResponse
    | HunterMessage
    | HunterSyncResponse
)


# --------------------------------------------------------------------------- #
# Envelope encode / decode
# --------------------------------------------------------------------------- #
def encode(msg: Message, network: str, sender: str) -> bytes:
    """Serialize a message into a UDP datagram payload."""
    body = _body_to_dict(msg)
    envelope = {"v": PROTOCOL_VERSION, "net": network, "sender": sender, **body}
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def decode(data: bytes) -> tuple[str, str, Message]:
    """Parse a datagram into ``(network, sender, message)``.

    Raises ``ValueError`` on a malformed payload or unknown/mismatched version.
    """
    obj = json.loads(data.decode("utf-8"))
    if obj.get("v") != PROTOCOL_VERSION:
        raise ValueError(f"unsupported protocol version: {obj.get('v')}")
    network = obj["net"]
    sender = obj["sender"]
    return network, sender, _body_from_dict(obj)


def _body_to_dict(msg: Message) -> dict:
    if isinstance(msg, Hello):
        return {
            "type": "hello",
            "operator": msg.operator,
            "call": msg.call,
            "high_water": msg.high_water,
        }
    if isinstance(msg, QsoMessage):
        return {"type": "qso", "qso": qso_to_wire(msg.qso)}
    if isinstance(msg, SyncRequest):
        return {"type": "sync_request", "high_water": msg.high_water}
    if isinstance(msg, SyncResponse):
        return {"type": "sync_response", "qsos": [qso_to_wire(q) for q in msg.qsos]}
    if isinstance(msg, FullLogRequest):
        return {"type": "full_log_request"}
    if isinstance(msg, Heartbeat):
        return {
            "type": "heartbeat",
            "count": msg.count,
            "log_hash": msg.log_hash,
            "lamport_max": msg.lamport_max,
            "sender_utc": msg.sender_utc,
        }
    if isinstance(msg, StationStatus):
        return {
            "type": "status",
            "operator": msg.operator,
            "call": msg.call,
            "freq_hz": msg.freq_hz,
            "mode": msg.mode,
            "power_w": msg.power_w,
            "swr": msg.swr,
            "ft_tx_even": msg.ft_tx_even,
        }
    if isinstance(msg, Chat):
        return {"type": "chat", **_chat_to_wire(msg)}
    if isinstance(msg, HunterMessage):
        return {"type": "pota_hunter", "hunter": hunter_to_wire(msg.hunter)}
    if isinstance(msg, HunterSyncResponse):
        return {
            "type": "pota_hunter_sync_response",
            "hunters": [hunter_to_wire(h) for h in msg.hunters],
        }
    if isinstance(msg, ChatSyncResponse):
        return {
            "type": "chat_sync_response",
            "chats": [_chat_to_wire(c) for c in msg.chats],
        }
    raise TypeError(f"cannot encode message of type {type(msg).__name__}")


def _chat_to_wire(msg: Chat) -> dict:
    return {
        "from_op": msg.from_op,
        "to_op": msg.to_op,
        "text": msg.text,
        "ts": msg.ts,
        "uuid": msg.uuid,
        "station_id": msg.station_id,
    }


def _chat_from_wire(d: dict) -> Chat:
    return Chat(
        from_op=d["from_op"],
        to_op=d["to_op"],
        text=d["text"],
        ts=d["ts"],
        uuid=d.get("uuid", ""),
        station_id=d.get("station_id", ""),
    )


def _body_from_dict(obj: dict) -> Message:
    t = obj.get("type")
    if t == "hello":
        return Hello(
            operator=obj["operator"], call=obj["call"], high_water=dict(obj.get("high_water", {}))
        )
    if t == "qso":
        return QsoMessage(qso=qso_from_wire(obj["qso"]))
    if t == "sync_request":
        return SyncRequest(high_water=dict(obj.get("high_water", {})))
    if t == "sync_response":
        return SyncResponse(qsos=[qso_from_wire(q) for q in obj.get("qsos", [])])
    if t == "full_log_request":
        return FullLogRequest()
    if t == "heartbeat":
        return Heartbeat(
            count=obj["count"],
            log_hash=obj["log_hash"],
            lamport_max=obj["lamport_max"],
            sender_utc=obj.get("sender_utc", ""),
        )
    if t == "status":
        return StationStatus(
            operator=obj["operator"],
            call=obj["call"],
            freq_hz=obj["freq_hz"],
            mode=obj["mode"],
            power_w=float(obj.get("power_w", 0.0)),
            swr=float(obj.get("swr", 0.0)),
            ft_tx_even=int(obj.get("ft_tx_even", -1)),
        )
    if t == "chat":
        return _chat_from_wire(obj)
    if t == "pota_hunter":
        return HunterMessage(hunter=hunter_from_wire(obj["hunter"]))
    if t == "pota_hunter_sync_response":
        return HunterSyncResponse(
            hunters=[hunter_from_wire(h) for h in obj.get("hunters", [])]
        )
    if t == "chat_sync_response":
        return ChatSyncResponse(chats=[_chat_from_wire(c) for c in obj.get("chats", [])])
    raise ValueError(f"unknown message type: {t!r}")
