"""Logbook entries of the integration that belong to a device but to none of its entities (travel time measurement:
the step messages would otherwise appear under the name of the button they are attached to)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN

EVENT_DEVICE_MESSAGE = f"{DOMAIN}_meldung"  # data: device_id, name, message


@callback
def async_describe_events(hass: HomeAssistant,
                          async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None]) -> None:
    @callback
    def describe(event: Event) -> dict[str, Any]:
        return {"name": event.data.get("name", ""), "message": event.data.get("message", "")}

    async_describe_event(DOMAIN, EVENT_DEVICE_MESSAGE, describe)
