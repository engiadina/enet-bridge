"""Mitgelieferte Bridge-Firmware (bridge_fw/<chip>/, erzeugt von tools/fw_bundle.sh) und Flashen per esptool.

Ein Bündel je Chip (ESP32-DevKit, XIAO ESP32-C6), gemeinsame Version in bridge_fw/version.txt. Abbild wie
tools/flash_raspi.sh: Bootloader, Partitionen, boot_app0, App. Der NVS-Bereich (Konfiguration, Frequenzkorrektur,
Server-Schalter) wird nicht beschrieben und bleibt erhalten.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .texte import tr

_LOGGER = logging.getLogger(__name__)

FW_DIR = Path(__file__).parent / "bridge_fw"
_REST = (("0x8000", "partitions.bin"), ("0xe000", "boot_app0.bin"), ("0x10000", "firmware.bin"))
ABBILD: dict[str, tuple[tuple[str, str], ...]] = {
    "esp32": (("0x1000", "bootloader.bin"), *_REST),
    "esp32c6": (("0x0", "bootloader.bin"), *_REST),
}
STANDARD_CHIP = "esp32"  # Firmware vor 0.8.14 meldet keinen Chip – das waren nur ESP32-DevKits


def _vollstaendig(chip: str) -> bool:
    return chip in ABBILD and all((FW_DIR / chip / f).is_file() for _, f in ABBILD[chip])


def mitgeliefert() -> str | None:
    """Version des Bündels oder None (kein Bündel; läuft im Executor)."""
    try:
        version = (FW_DIR / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return version if _vollstaendig(STANDARD_CHIP) else None


def version_tupel(version: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in (version or "").split("."))
    except ValueError:
        return ()


def chip_erkennen(port: str) -> str:
    """Chip eines ESP ohne Bridge-Firmware per esptool erkennen ("esp32", "esp32c6", …; im Executor)."""
    from esptool.cmds import detect_chip

    esp = detect_chip(port)
    try:
        return esp.CHIP_NAME.lower().replace("-", "")
    finally:
        esp._port.close()  # noqa: SLF001 – esptool bietet kein öffentliches close


def flashen(port: str, chip: str | None = None) -> None:
    """Bündel auf die Bridge schreiben (blockiert ~15 s, im Executor; Port muss freigegeben sein).
    chip: aus dem hello der Bridge; None = per esptool erkennen (leerer ESP)."""
    import esptool  # erst hier: wird von HA aus den requirements installiert

    chip = chip or chip_erkennen(port)
    if not _vollstaendig(chip):
        raise RuntimeError(tr("chip_nicht_unterstuetzt", chip=chip, chips=", ".join(ABBILD)))
    argv = ["--chip", chip, "--port", port, "--baud", "460800", "write-flash", "-z"]
    for adresse, datei in ABBILD[chip]:
        argv += [adresse, str(FW_DIR / chip / datei)]
    _LOGGER.info("Flashing bridge firmware: esptool %s", " ".join(argv))
    try:
        esptool.main(argv)
    except SystemExit as err:  # esptool beendet bei Argument- und Verbindungsfehlern per sys.exit
        if err.code not in (0, None):
            raise RuntimeError(tr("esptool_ende", rc=err.code)) from err
