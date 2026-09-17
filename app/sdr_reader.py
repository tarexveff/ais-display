"""
sdr_reader.py — RTL-SDR subprocess manager and asyncio UDP NMEA reader.

Starts `rtl_ais` as a managed subprocess, listens for NMEA sentences on a
local UDP port, and pushes each sentence onto a shared asyncio.Queue for the
decoder.  Restarts `rtl_ais` with exponential back-off if it exits unexpectedly.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

try:
    from app.forwarder import forward
except ImportError:
    async def forward(sentence: str) -> None:  # type: ignore[misc]
        """No-op fallback used when forwarder.py is not yet available."""
        pass


# ---------------------------------------------------------------------------
# subprocess launcher
# ---------------------------------------------------------------------------

async def launch_rtl_ais(
    udp_port: str,
    gain: str,
    ppm: str,
) -> asyncio.subprocess.Process:
    """Start *rtl_ais* and return the Process handle.

    Correct rtl_ais flags (from its help text):
      -n        log NMEA sentences to stderr (visible in container logs)
      -P port   UDP destination port (default 10110)
      -g gain   tuner gain; 0 = automatic
      -p ppm    frequency correction in PPM

    The built-in AIS decoder sends UDP to 127.0.0.1:10110 by default, which
    is exactly where NMEAUdpProtocol listens. No host flag needed.

    Args:
        udp_port: UDP port string; passed as -P only when not the default 10110.
        gain:     Tuner gain string for ``-g`` (``0`` = auto).
        ppm:      Frequency correction string for ``-p``.
    """
    cmd = [
        "rtl_ais",
        "-n",                               # log NMEA sentences to stderr
        "-g", gain,                         # tuner gain (0 = automatic)
        "-p", ppm,                          # PPM frequency correction
        "-U", f"127.0.0.1:{udp_port}",      # push NMEA via UDP to our listener
    ]
    logger.info("Starting rtl_ais: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    asyncio.ensure_future(_stream_stderr(proc))
    return proc


async def _stream_stderr(proc: asyncio.subprocess.Process) -> None:
    """Read *proc* stderr line-by-line and forward each line to WARNING.

    Logged at WARNING (not DEBUG) so startup errors from rtl_ais are always
    visible in the container logs without needing to enable debug logging.
    """
    assert proc.stderr is not None
    async for line in proc.stderr:
        logger.warning("rtl_ais stderr: %s", line.decode(errors="replace").rstrip())


# ---------------------------------------------------------------------------
# UDP protocol
# ---------------------------------------------------------------------------

class NMEAUdpProtocol(asyncio.DatagramProtocol):
    """Receives UDP datagrams from *rtl_ais* and enqueues NMEA sentences."""

    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        sentence = data.decode("utf-8", errors="replace").strip()
        if not sentence:
            return
        logger.debug("NMEA sentence: %s", sentence)
        self._queue.put_nowait(sentence)
        asyncio.ensure_future(forward(sentence))

    def error_received(self, exc: Exception) -> None:
        logger.warning("UDP error: %s", exc)


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

async def read_sdr(queue: asyncio.Queue) -> None:
    """Launch *rtl_ais*, bind the UDP endpoint, and supervise the subprocess.

    Called by the FastAPI lifespan as a background task.  Reads configuration
    from environment variables at call time:

    * ``RTL_GAIN``     — tuner gain in tenths of dB (default ``"0"`` = auto)
    * ``RTL_PPM``      — frequency correction in PPM (default ``"0"``)
    * ``RTL_UDP_PORT`` — local UDP port for NMEA datagrams (default ``"10110"``)

    Restarts *rtl_ais* with exponential back-off (2 s → 4 s → 8 s, capped at
    30 s) whenever it exits unexpectedly.  Handles :exc:`asyncio.CancelledError`
    by terminating the subprocess and releasing the UDP transport cleanly.
    """
    gain = os.environ.get("RTL_GAIN", "0")
    ppm = os.environ.get("RTL_PPM", "0")
    udp_port = os.environ.get("RTL_UDP_PORT", "10110")

    loop = asyncio.get_running_loop()

    # Bind UDP endpoint once; it stays open for the lifetime of the task.
    transport, _ = await loop.create_datagram_endpoint(
        lambda: NMEAUdpProtocol(queue),
        local_addr=("127.0.0.1", int(udp_port)),
    )

    proc: asyncio.subprocess.Process | None = None
    back_off = 2.0

    try:
        proc = await launch_rtl_ais(udp_port, gain, ppm)

        while True:
            await proc.wait()
            logger.warning(
                "rtl_ais exited with code %s; restarting in %.0f s",
                proc.returncode,
                back_off,
            )
            await asyncio.sleep(back_off)
            back_off = min(back_off * 2, 30.0)
            proc = await launch_rtl_ais(udp_port, gain, ppm)

    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        transport.close()
        raise
