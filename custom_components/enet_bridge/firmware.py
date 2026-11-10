"""Bundled bridge firmware (bridge_fw/<chip>/, built by tools/fw_bundle.sh) and flashing via esptool.

One bundle per chip (ESP32-DevKit, XIAO ESP32-C6), shared version in bridge_fw/version.txt. Image as in
tools/flash_raspi.sh: bootloader, partitions, boot_app0, app. The NVS area (configuration, frequency correction,
server switch) is not written and is preserved.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .texts import tr

_LOGGER = logging.getLogger(__name__)

FW_DIR = Path(__file__).parent / "bridge_fw"
_REST = (("0x8000", "partitions.bin"), ("0xe000", "boot_app0.bin"), ("0x10000", "firmware.bin"))
IMAGE: dict[str, tuple[tuple[str, str], ...]] = {
    "esp32": (("0x1000", "bootloader.bin"), *_REST),
    "esp32c6": (("0x0", "bootloader.bin"), *_REST),
}
DEFAULT_CHIP = "esp32"  # firmware before 0.8.14 reports no chip – those were ESP32-DevKits only


def _complete(chip: str) -> bool:
    return chip in IMAGE and all((FW_DIR / chip / f).is_file() for _, f in IMAGE[chip])


def bundled() -> str | None:
    """Version of the bundle, or None (no bundle; runs in the executor)."""
    try:
        version = (FW_DIR / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return version if _complete(DEFAULT_CHIP) else None


def version_tuple(version: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in (version or "").split("."))
    except ValueError:
        return ()


def detect_chip(port: str) -> str:
    """Detect the chip of an ESP without bridge firmware via esptool ("esp32", "esp32c6", …; in the executor)."""
    from esptool.cmds import detect_chip

    esp = detect_chip(port)
    try:
        return esp.CHIP_NAME.lower().replace("-", "")
    finally:
        esp._port.close()  # noqa: SLF001 – esptool has no public close


def flash(port: str, chip: str | None = None) -> None:
    """Write the bundle to the bridge (blocks ~15 s, in the executor; the port must be released).
    chip: from the bridge's hello; None = detect via esptool (blank ESP)."""
    import esptool  # imported only here: installed by HA from the requirements

    chip = chip or detect_chip(port)
    if not _complete(chip):
        raise RuntimeError(tr("chip_nicht_unterstuetzt", chip=chip, chips=", ".join(IMAGE)))
    argv = ["--chip", chip, "--port", port, "--baud", "460800", "write-flash", "-z"]
    for adresse, file_ in IMAGE[chip]:
        argv += [adresse, str(FW_DIR / chip / file_)]
    _LOGGER.info("Flashing bridge firmware: esptool %s", " ".join(argv))
    try:
        esptool.main(argv)
    except SystemExit as err:  # esptool exits via sys.exit on argument and connection errors
        if err.code not in (0, None):
            raise RuntimeError(tr("esptool_ende", rc=err.code)) from err
