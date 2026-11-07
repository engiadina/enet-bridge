"""Mitgelieferte Bridge-Firmware (bridge_fw/, erzeugt von tools/fw_bundle.sh) und Flashen per esptool.

Abbild wie tools/flash_raspi.sh: Bootloader, Partitionen, boot_app0, App. Der NVS-Bereich (Konfiguration,
Frequenzkorrektur, Server-Schalter) wird nicht beschrieben und bleibt erhalten.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .texte import tr

_LOGGER = logging.getLogger(__name__)

FW_DIR = Path(__file__).parent / "bridge_fw"
ABBILD = (("0x1000", "bootloader.bin"), ("0x8000", "partitions.bin"), ("0xe000", "boot_app0.bin"),
          ("0x10000", "firmware.bin"))


def mitgeliefert() -> str | None:
    """Version des Bündels oder None (kein Bündel; läuft im Executor)."""
    try:
        version = (FW_DIR / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return version if all((FW_DIR / f).is_file() for _, f in ABBILD) else None


def version_tupel(version: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in (version or "").split("."))
    except ValueError:
        return ()


def flashen(port: str) -> None:
    """Bündel auf die Bridge schreiben (blockiert ~15 s, im Executor; Port muss freigegeben sein)."""
    import esptool  # erst hier: wird von HA aus den requirements installiert

    argv = ["--chip", "esp32", "--port", port, "--baud", "460800", "write-flash", "-z"]
    for adresse, datei in ABBILD:
        argv += [adresse, str(FW_DIR / datei)]
    _LOGGER.info("Flashing bridge firmware: esptool %s", " ".join(argv))
    try:
        esptool.main(argv)
    except SystemExit as err:  # esptool beendet bei Argument- und Verbindungsfehlern per sys.exit
        if err.code not in (0, None):
            raise RuntimeError(tr("esptool_ende", rc=err.code)) from err
