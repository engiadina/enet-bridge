"""Connection to the eNet bridge via USB (protocol 1, docs/INTERFACE.md).

One JSON line per message. Commands carry an id; the ESP answers immediately with "re" and reports
radio results later as "ev":"result" with the same id. Events ("ev") go to all listeners.
Reading happens in a separate thread (pyserial), dispatching in the Home Assistant event loop.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

import serial

from .texts import tr

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[dict[str, Any]], None]
HISTORY_MAX = 300  # last messages to/from the bridge for "download diagnostics"
HISTORY_CHARS = 400  # at most this many characters per entry (the configuration upload is long)


class BridgeError(Exception):
    """Command rejected or bridge not reachable."""

    def __init__(self, err: str, msg: str = "", rc: int | None = None) -> None:
        super().__init__(msg if err == "mithoeren" else f"{err}: {msg}" if msg else err)  # mithoeren: for the user
        self.err, self.rc = err, rc


class Bridge:
    """Serial connection with reconnect, command/response and event dispatch."""

    RECONNECT_S = 10

    def __init__(self, loop: asyncio.AbstractEventLoop, port: str) -> None:
        self._loop = loop
        self.port = port
        self.listen_only_mode = False  # "listen only": no radio commands (request, send) to the bridge
        self._ser: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._released = False
        self._ids = itertools.count(1)
        self._replies: dict[int, asyncio.Future] = {}
        self._results: dict[int, asyncio.Future] = {}
        self.history: deque[tuple[str, str, str]] = deque(maxlen=HISTORY_MAX)  # (time, ">"/"<", JSON line)
        self._listeners: list[Listener] = []
        self.connected = False
        self.info: dict[str, Any] = {}

    # --- Connection -------------------------------------------------------
    def _open(self) -> None:
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, 115200, 0.2
        s.dtr = s.rts = False  # otherwise reset (on Linux the ESP restarts on open anyway)
        s.open()
        self._ser = s

    def _reader(self) -> None:
        buf = b""
        while not self._stop.is_set():
            if self._released:
                self._stop.wait(0.5)
                continue
            if self._ser is None:
                try:
                    self._open()
                    self._loop.call_soon_threadsafe(self._set_connected, True)
                except (serial.SerialException, OSError) as err:
                    _LOGGER.debug("Bridge on %s not reachable: %s", self.port, err)
                    self._stop.wait(self.RECONNECT_S)
                    continue
            try:
                buf += self._ser.read(self._ser.in_waiting or 1)
            except (serial.SerialException, OSError, TypeError) as err:
                if self._released:  # port was closed deliberately (release)
                    buf = b""
                    continue
                _LOGGER.warning("Connection to the bridge lost: %s", err)
                self._close_port()
                self._loop.call_soon_threadsafe(self._set_connected, False)
                buf = b""
                continue
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("{"):
                    continue  # ESP32 boot messages
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                self._loop.call_soon_threadsafe(self._dispatch, msg)

    def _close_port(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except (serial.SerialException, OSError):
                pass
        self._ser = None

    def _set_connected(self, value: bool) -> None:
        self.connected = value
        if not value:
            for fut in list(self._replies.values()) + list(self._results.values()):
                if not fut.done():
                    fut.set_exception(BridgeError("disconnected", tr("verbindung_verloren")))
            self._replies.clear()
            self._results.clear()
        self._dispatch({"ev": "_connection", "connected": value})

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="enet-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._close_port()

    def release(self) -> None:
        """Release the port (e.g. to flash the firmware)."""
        self._released = True
        self._close_port()
        self._loop.call_soon_threadsafe(self._set_connected, False)

    def reconnect(self) -> None:
        self._released = False

    # --- Messages --------------------------------------------------------
    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _remember(self, direction: str, obj: dict[str, Any]) -> None:
        text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        self.history.append((datetime.now().isoformat(timespec="milliseconds"), direction, text[:HISTORY_CHARS]))

    def _dispatch(self, msg: dict[str, Any]) -> None:
        self._remember("<", msg)
        if "re" in msg:
            fut = self._replies.pop(msg["re"], None)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        if msg.get("ev") == "result":
            fut = self._results.pop(msg.get("id"), None)
            if fut and not fut.done():
                fut.set_result(msg)
        for listener in list(self._listeners):
            try:
                listener(msg)
            except Exception:  # noqa: BLE001 – a faulty listener must not stop the dispatch
                _LOGGER.exception("Error while processing %s", msg)

    def _write(self, obj: dict[str, Any]) -> None:
        if self._ser is None:
            raise BridgeError("disconnected", tr("bridge_nicht_verbunden"))
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        if len(line) > 512:
            raise BridgeError("line_too_long", tr("befehl_zu_lang"))
        self._ser.write(line)
        self._remember(">", obj)

    def _not_listen_only(self) -> None:
        if self.listen_only_mode:
            raise BridgeError("mithoeren", tr("mithoeren_sendet_nicht"))

    async def command(self, cmd: str, timeout: float = 2.0, **fields: Any) -> dict[str, Any]:
        """Send a command and wait for the immediate response ("re")."""
        if cmd == "send":
            self._not_listen_only()
        cid = next(self._ids)
        fut = self._loop.create_future()
        self._replies[cid] = fut
        try:
            await self._loop.run_in_executor(None, self._write, {"cmd": cmd, "id": cid, **fields})
            reply = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as err:
            raise BridgeError("timeout", tr("keine_antwort_auf", cmd=cmd)) from err
        finally:
            self._replies.pop(cid, None)
        if not reply.get("ok"):
            raise BridgeError(reply.get("err", "error"), reply.get("msg", ""))
        return reply

    async def request(self, cmd: str, timeout: float = 10.0, **fields: Any) -> dict[str, Any]:
        """Send a radio command and wait for the result ("ev":"result")."""
        self._not_listen_only()
        cid = next(self._ids)
        reply_fut = self._loop.create_future()
        result_fut = self._loop.create_future()
        self._replies[cid] = reply_fut
        self._results[cid] = result_fut
        try:
            await self._loop.run_in_executor(None, self._write, {"cmd": cmd, "id": cid, **fields})
            reply = await asyncio.wait_for(reply_fut, 2.0)
            if not reply.get("ok"):
                raise BridgeError(reply.get("err", "error"), reply.get("msg", ""))
            result = await asyncio.wait_for(result_fut, timeout)
        except asyncio.TimeoutError as err:
            raise BridgeError("timeout", tr("kein_ergebnis", cmd=cmd)) from err
        finally:
            self._replies.pop(cid, None)
            self._results.pop(cid, None)
        if not result.get("ok"):
            raise BridgeError(result.get("err", "error"), result.get("msg", ""), result.get("rc"))
        return result

    async def hello(self) -> dict[str, Any]:
        """Check the connection, set the clock, fetch the bridge state."""
        self.info = await self.command("hello", time=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        return self.info

    async def upload_config(self, split_parts: list[str]) -> dict[str, Any]:
        """Transfer the server configuration in parts; returns the config event (crc, channels, …)."""
        done = self._loop.create_future()

        def wait(msg: dict[str, Any]) -> None:
            if msg.get("ev") == "config" and not done.done():
                done.set_result(msg)

        unsubscribe = self.add_listener(wait)
        try:
            for n, part in enumerate(split_parts):
                await self.command("config", part=n, data=part, last=n == len(split_parts) - 1)
            outcome = await asyncio.wait_for(done, 3.0)
        except asyncio.TimeoutError as err:
            raise BridgeError("timeout", tr("config_unbestaetigt")) from err
        finally:
            unsubscribe()
        if not outcome.get("ok"):
            raise BridgeError(outcome.get("err", "error"), outcome.get("msg", ""))
        return outcome


# Radio modules of other integrations (Thread, Zigbee, Z-Wave) by their by-id name: never open, never flash.
# Just opening one crashes e.g. the OpenThread Border Router on the ZBT-2 (2026-11-02).
FOREIGN_RADIOS = ("nabu_casa", "skyconnect", "zbt-", "zigbee", "conbee", "raspbee", "slzb", "z-wave", "zwave",
                     "zooz", "aeotec")


def foreign_radio(port: str) -> bool:
    name = port.rsplit("/", 1)[-1].lower()
    return any(k in name for k in FOREIGN_RADIOS)


def list_ports() -> list[str]:
    """Serial ports without foreign radio modules, preferring stable /dev/serial/by-id paths (runs in the executor)."""
    import glob

    ports = sorted(glob.glob("/dev/serial/by-id/*")) or sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    return [p for p in ports if not foreign_radio(p)]
