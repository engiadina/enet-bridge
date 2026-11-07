"""Verbindung zur eNet-Bridge über USB (Protokoll 1, docs/SCHNITTSTELLE.md).

Eine JSON-Zeile pro Nachricht. Befehle tragen eine id; der ESP antwortet sofort mit "re" und meldet
Funk-Ergebnisse später als "ev":"result" mit derselben id. Ereignisse ("ev") gehen an alle Zuhörer.
Gelesen wird in einem eigenen Thread (pyserial), verteilt im Event-Loop von Home Assistant.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

import serial

from .texte import tr

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[dict[str, Any]], None]


class BridgeError(Exception):
    """Befehl abgelehnt oder Bridge nicht erreichbar."""

    def __init__(self, err: str, msg: str = "", rc: int | None = None) -> None:
        super().__init__(msg if err == "mithoeren" else f"{err}: {msg}" if msg else err)  # mithoeren: für Nutzer
        self.err, self.rc = err, rc


class Bridge:
    """Serielle Verbindung mit Wiederverbinden, Befehl/Antwort und Ereignisverteilung."""

    RECONNECT_S = 10

    def __init__(self, loop: asyncio.AbstractEventLoop, port: str) -> None:
        self._loop = loop
        self.port = port
        self.nur_hoeren = False  # „Nur mithören“: keine Funk-Befehle (request, send) an die Bridge
        self._ser: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._released = False
        self._ids = itertools.count(1)
        self._replies: dict[int, asyncio.Future] = {}
        self._results: dict[int, asyncio.Future] = {}
        self._listeners: list[Listener] = []
        self.connected = False
        self.info: dict[str, Any] = {}

    # --- Verbindung ---------------------------------------------------------
    def _open(self) -> None:
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, 115200, 0.2
        s.dtr = s.rts = False  # sonst Reset (unter Linux startet der ESP beim Öffnen trotzdem neu)
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
                if self._released:  # Port wurde absichtlich geschlossen (release)
                    buf = b""
                    continue
                _LOGGER.warning("Verbindung zur Bridge verloren: %s", err)
                self._close_port()
                self._loop.call_soon_threadsafe(self._set_connected, False)
                buf = b""
                continue
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("{"):
                    continue  # Bootmeldungen des ESP32
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
        """Port freigeben (z. B. zum Flashen der Firmware)."""
        self._released = True
        self._close_port()
        self._loop.call_soon_threadsafe(self._set_connected, False)

    def reconnect(self) -> None:
        self._released = False

    # --- Nachrichten --------------------------------------------------------
    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _dispatch(self, msg: dict[str, Any]) -> None:
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
            except Exception:  # noqa: BLE001 – ein fehlerhafter Zuhörer darf die Verteilung nicht stoppen
                _LOGGER.exception("Error while processing %s", msg)

    def _write(self, obj: dict[str, Any]) -> None:
        if self._ser is None:
            raise BridgeError("disconnected", tr("bridge_nicht_verbunden"))
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        if len(line) > 512:
            raise BridgeError("line_too_long", tr("befehl_zu_lang"))
        self._ser.write(line)

    def _nicht_mithoeren(self) -> None:
        if self.nur_hoeren:
            raise BridgeError("mithoeren", tr("mithoeren_sendet_nicht"))

    async def command(self, cmd: str, timeout: float = 2.0, **fields: Any) -> dict[str, Any]:
        """Befehl senden und auf die sofortige Antwort ("re") warten."""
        if cmd == "send":
            self._nicht_mithoeren()
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
        """Funk-Befehl senden und auf das Ergebnis ("ev":"result") warten."""
        self._nicht_mithoeren()
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
        """Verbindung prüfen, Uhr stellen, Zustand der Bridge holen."""
        self.info = await self.command("hello", time=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        return self.info

    async def config_hochladen(self, teile: list[str]) -> dict[str, Any]:
        """Server-Konfiguration in Teilen übertragen; liefert das Ereignis config (crc, channels, …)."""
        fertig = self._loop.create_future()

        def warten(msg: dict[str, Any]) -> None:
            if msg.get("ev") == "config" and not fertig.done():
                fertig.set_result(msg)

        abmelden = self.add_listener(warten)
        try:
            for n, teil in enumerate(teile):
                await self.command("config", part=n, data=teil, last=n == len(teile) - 1)
            ergebnis = await asyncio.wait_for(fertig, 3.0)
        except asyncio.TimeoutError as err:
            raise BridgeError("timeout", tr("config_unbestaetigt")) from err
        finally:
            abmelden()
        if not ergebnis.get("ok"):
            raise BridgeError(ergebnis.get("err", "error"), ergebnis.get("msg", ""))
        return ergebnis


# Funkmodule anderer Integrationen (Thread, Zigbee, Z-Wave) an ihrem by-id-Namen: nie öffnen, nie flashen.
# Schon das Öffnen bringt z. B. den OpenThread Border Router am ZBT-2 zum Absturz (2026-11-02).
FREMDE_FUNKMODULE = ("nabu_casa", "skyconnect", "zbt-", "zigbee", "conbee", "raspbee", "slzb", "z-wave", "zwave",
                     "zooz", "aeotec")


def fremdes_funkmodul(port: str) -> bool:
    name = port.rsplit("/", 1)[-1].lower()
    return any(k in name for k in FREMDE_FUNKMODULE)


def list_ports() -> list[str]:
    """Serielle Ports ohne fremde Funkmodule, bevorzugt stabile /dev/serial/by-id-Pfade (läuft im Executor)."""
    import glob

    ports = sorted(glob.glob("/dev/serial/by-id/*")) or sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    return [p for p in ports if not fremdes_funkmodul(p)]
