"""
forwarder.py — UDP NMEA sentence forwarder.

Reads FORWARD_TARGETS (comma-separated host:port pairs) at import time and
re-uses a single UDP socket to fire-and-forget each raw NMEA sentence to every
configured target.  When FORWARD_TARGETS is unset or empty the module is a
complete no-op.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level initialisation
# ---------------------------------------------------------------------------

def parse_targets(raw: str) -> list[tuple[str, int]]:
    """Parse a comma-separated string of host:port pairs.

    Malformed entries are skipped with a WARNING log.  Returns an empty list
    when *raw* is empty or contains only whitespace.
    """
    targets: list[tuple[str, int]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            host, port_str = entry.rsplit(":", 1)
            port = int(port_str)
            if not host:
                raise ValueError("empty host")
            targets.append((host, port))
        except ValueError as exc:
            logger.warning("forwarder: skipping malformed FORWARD_TARGETS entry %r: %s", entry, exc)
    return targets


_raw_env: str = os.environ.get("FORWARD_TARGETS", "")
TARGETS: list[tuple[str, int]] = parse_targets(_raw_env)

# Only create a socket when there is at least one valid target; otherwise the
# module stays truly side-effect-free.
_sock: socket.socket | None = None
if TARGETS:
    _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    logger.info("forwarder: NMEA forwarding enabled — %d target(s): %s", len(TARGETS), TARGETS)
else:
    logger.info(
        "forwarder: FORWARD_TARGETS=%r — no targets configured, forwarding disabled",
        _raw_env,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_forward_count: int = 0  # used to log a one-time confirmation on first forward


async def forward(sentence: str) -> None:
    """Send *sentence* as a UDP datagram to every configured target.

    Returns immediately when no targets are configured.  Errors for individual
    targets are logged at WARNING level and never propagate — the forwarder is
    strictly fire-and-forget.
    """
    global _forward_count
    if not TARGETS:
        return

    if _forward_count == 0:
        logger.info("forwarder: first NMEA sentence forwarded — forwarding is working")
    _forward_count += 1

    # Ensure standard NMEA line termination.
    if not sentence.endswith("\r\n"):
        sentence = sentence.rstrip("\n").rstrip("\r") + "\r\n"
    data: bytes = sentence.encode("utf-8")

    loop = asyncio.get_event_loop()
    for host, port in TARGETS:
        try:
            await loop.run_in_executor(None, _sock.sendto, data, (host, port))  # type: ignore[union-attr]
        except OSError as exc:
            logger.warning("forwarder: failed to send to %s:%d — %s", host, port, exc)
