"""
AIS Decoder and Vessel State Store.

Consumes raw NMEA sentences from an asyncio.Queue, decodes them with pyais,
and maintains in-memory state dicts exported for use by the REST endpoints.
"""

import asyncio
import logging
from collections import deque, defaultdict

from pyais import decode
from pyais.exceptions import AISBaseException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state stores — exported for use by REST endpoints
# ---------------------------------------------------------------------------

# Latest decoded state per MMSI: { mmsi_str: { field: value, ... } }
vessel_store: dict[str, dict] = {}

# Position history per MMSI: { mmsi_str: deque([[lat, lon], ...]) }
# Exposed as a plain dict; values are deques that serialise like lists.
track_store: dict[str, deque] = {}

_TRACK_MAX = 500

# ---------------------------------------------------------------------------
# Multi-sentence fragment buffer
# ---------------------------------------------------------------------------
# NMEA AIS sentences carry:
#   field 0: talker+type  (!AIVDM)
#   field 1: total fragment count
#   field 2: fragment number (1-based)
#   field 3: sequential message ID (empty for single-sentence messages)
#   field 4: radio channel (A/B)
#   field 5: encoded payload
#   field 6: fill bits + checksum
#
# We key the accumulator by (channel, seq_id) so fragments from different
# interleaved multi-sentence messages are kept separate.
_fragment_buffer: dict[tuple, list] = defaultdict(list)


def _fragment_key(sentence: str) -> tuple:
    """Return (channel, seq_id) for a given NMEA sentence string."""
    fields = sentence.split(",")
    channel = fields[4] if len(fields) > 4 else ""
    seq_id = fields[3] if len(fields) > 3 else ""
    return (channel, seq_id)


def _fragment_count(sentence: str) -> int:
    """Return the total fragment count declared in the sentence."""
    try:
        return int(sentence.split(",")[1])
    except (IndexError, ValueError):
        return 1


# ---------------------------------------------------------------------------
# Main decode loop
# ---------------------------------------------------------------------------

async def decode_loop(queue: asyncio.Queue, broadcast_fn) -> None:
    """
    Background task: reads NMEA sentences from *queue*, decodes them with
    pyais, updates vessel_store / track_store, and calls broadcast_fn.

    broadcast_fn signature: async def broadcast_fn(vessel_store, track_store)
    """
    while True:
        sentence: str = await queue.get()
        sentence = sentence.strip()

        if not sentence:
            continue

        total_fragments = _fragment_count(sentence)

        if total_fragments == 1:
            # Single-sentence message — decode immediately.
            parts = [sentence]
        else:
            # Multi-part message (e.g. type 5 with 2 fragments).
            key = _fragment_key(sentence)
            _fragment_buffer[key].append(sentence)
            if len(_fragment_buffer[key]) < total_fragments:
                # Not all parts have arrived yet; wait for the rest.
                continue
            parts = _fragment_buffer.pop(key)

        try:
            # decode().asdict() may contain Enum values — convert to their
            # underlying primitive (int/str) so they are JSON-serialisable.
            raw = decode(*parts).asdict()
            msg = {k: (v.value if hasattr(v, 'value') else v) for k, v in raw.items()}
        except AISBaseException as exc:
            logger.warning("AIS decode error: %s | sentences: %s", exc, parts)
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected decode error: %s | sentences: %s", exc, parts)
            continue

        mmsi = str(msg.get("mmsi", ""))
        if not mmsi:
            continue

        # Merge into vessel_store (preserve fields from earlier messages).
        if mmsi not in vessel_store:
            vessel_store[mmsi] = {}
        vessel_store[mmsi].update(msg)

        # Append position to track_store when lat/lon are present and non-zero.
        lat = msg.get("lat")
        lon = msg.get("lon")
        if lat and lon:
            if mmsi not in track_store:
                track_store[mmsi] = deque(maxlen=_TRACK_MAX)
            track_store[mmsi].append([lat, lon])

        await broadcast_fn(vessel_store, track_store)
