"""Custom services for the Petkit integration."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from pypetkitapi import (
    DEVICES_FEEDER,
    Feeder,
    FeederCommand,
)
from pypetkitapi.feeder_container import FeedDailyList, FeedItem

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import DOMAIN, LOGGER
from .utils import (
    get_schedule_attributes,
    is_dual_hopper,
    seconds_to_time,
    time_to_seconds,
)

if TYPE_CHECKING:
    from .data import PetkitConfigEntry

SERVICE_GET_FEEDING_SCHEDULE = "get_feeding_schedule"
SERVICE_SET_FEEDING_ITEM = "set_feeding_item"
SERVICE_REMOVE_FEEDING_ITEM = "remove_feeding_item"

ATTR_DEVICE_ID = "device_id"
ATTR_TIME = "time"
ATTR_AMOUNT = "amount"
ATTR_AMOUNT1 = "amount1"
ATTR_AMOUNT2 = "amount2"
ATTR_DAYS = "days"
ATTR_NAME = "name"

GET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
    }
)

SET_FEEDING_ITEM_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_TIME): cv.string,
        vol.Optional(ATTR_AMOUNT): vol.Coerce(int),
        vol.Optional(ATTR_AMOUNT1): vol.Coerce(int),
        vol.Optional(ATTR_AMOUNT2): vol.Coerce(int),
        vol.Optional(ATTR_DAYS): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        vol.Optional(ATTR_NAME): cv.string,
    }
)

REMOVE_FEEDING_ITEM_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_TIME): cv.string,
        vol.Optional(ATTR_DAYS): vol.All(cv.ensure_list, [vol.Coerce(int)]),
    }
)


def _find_feeder(hass: HomeAssistant, device_id: str) -> tuple[Any, Feeder]:
    """Find a feeder device by HA device ID. Returns (client, feeder)."""
    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get(device_id)
    if not device_entry:
        raise HomeAssistantError(f"Device {device_id} not found")

    # Find the config entry for this device
    for entry_id in device_entry.config_entries:
        entry: PetkitConfigEntry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == DOMAIN:
            client = entry.runtime_data.client
            coordinator = entry.runtime_data.coordinator
            # Find the feeder matching this device
            for pk_device in client.petkit_entities.values():
                if isinstance(pk_device, Feeder) and pk_device.sn in str(
                    device_entry.identifiers
                ):
                    return client, pk_device, coordinator

    raise HomeAssistantError(f"Feeder device {device_id} not found in Petkit integration")


def _parse_time_string(time_str: str) -> int:
    """Parse a time string (HH:MM) to seconds since midnight."""
    parts = time_str.split(":")
    if len(parts) != 2:
        raise HomeAssistantError(f"Invalid time format: {time_str}. Use HH:MM")
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError as err:
        raise HomeAssistantError(f"Invalid time format: {time_str}. Use HH:MM") from err
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise HomeAssistantError(f"Time out of range: {time_str}")
    return hours * 3600 + minutes * 60


async def async_get_feeding_schedule(hass: HomeAssistant, call: ServiceCall) -> dict:
    """Handle get_feeding_schedule service call."""
    device_id = call.data[ATTR_DEVICE_ID]
    _client, feeder, coordinator = _find_feeder(hass, device_id)

    # Get fresh data from coordinator
    updated = coordinator.data.get(feeder.id)
    if updated and isinstance(updated, Feeder):
        feeder = updated

    return get_schedule_attributes(feeder)


async def async_set_feeding_item(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle set_feeding_item service call."""
    device_id = call.data[ATTR_DEVICE_ID]
    time_str = call.data[ATTR_TIME]
    amount = call.data.get(ATTR_AMOUNT)
    amount1 = call.data.get(ATTR_AMOUNT1)
    amount2 = call.data.get(ATTR_AMOUNT2)
    days = call.data.get(ATTR_DAYS, list(range(1, 8)))
    name = call.data.get(ATTR_NAME, "")

    client, feeder, coordinator = _find_feeder(hass, device_id)
    time_seconds = _parse_time_string(time_str)

    # Get fresh data
    updated = coordinator.data.get(feeder.id)
    if updated and isinstance(updated, Feeder):
        feeder = updated

    dual = is_dual_hopper(feeder)

    # Validate amounts
    if dual:
        if amount1 is None and amount2 is None:
            raise HomeAssistantError(
                "Dual-hopper feeder requires amount1 and/or amount2"
            )
        amount1 = amount1 or 0
        amount2 = amount2 or 0
    else:
        if amount is None:
            raise HomeAssistantError("Single-hopper feeder requires amount")

    # Build the new feed item
    new_item_dict: dict[str, Any] = {
        "time": time_seconds,
        "id": str(time_seconds),
        "name": name,
    }
    if dual:
        new_item_dict["amount1"] = amount1
        new_item_dict["amount2"] = amount2
    else:
        new_item_dict["amount"] = amount

    mfi = feeder.multi_feed_item
    if not mfi or not mfi.feed_daily_list:
        # Create a fresh weekly plan
        feed_daily_list = []
        for day in range(1, 8):
            if day in days:
                feed_daily_list.append({
                    "items": [new_item_dict],
                    "repeats": day,
                    "suspended": 0,
                })
            else:
                feed_daily_list.append({
                    "items": [],
                    "repeats": day,
                    "suspended": 0,
                })
    else:
        # Deep copy and modify existing plan
        feed_daily_list = mfi.to_api_list()
        for daily_dict in feed_daily_list:
            day_num = daily_dict.get("repeats")
            if day_num in days:
                items = daily_dict.get("items", [])
                # Replace existing item at same time or add new
                replaced = False
                for i, existing in enumerate(items):
                    if existing.get("time") == time_seconds:
                        items[i] = new_item_dict
                        replaced = True
                        break
                if not replaced:
                    items.append(new_item_dict)
                    items.sort(key=lambda x: x.get("time", 0))
                daily_dict["items"] = items

    LOGGER.debug("Setting feeding schedule: %s", feed_daily_list)
    await client.send_api_request(feeder.id, FeederCommand.SAVE_FEED, feed_daily_list)

    # Trigger refresh
    coordinator.enable_smart_polling(3)
    await coordinator.async_request_refresh()


async def async_remove_feeding_item(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle remove_feeding_item service call."""
    device_id = call.data[ATTR_DEVICE_ID]
    time_str = call.data[ATTR_TIME]
    days = call.data.get(ATTR_DAYS, list(range(1, 8)))

    client, feeder, coordinator = _find_feeder(hass, device_id)
    time_seconds = _parse_time_string(time_str)

    # Get fresh data
    updated = coordinator.data.get(feeder.id)
    if updated and isinstance(updated, Feeder):
        feeder = updated

    mfi = feeder.multi_feed_item
    if not mfi or not mfi.feed_daily_list:
        raise HomeAssistantError("No feeding schedule exists to remove items from")

    feed_daily_list = mfi.to_api_list()
    removed = False
    for daily_dict in feed_daily_list:
        day_num = daily_dict.get("repeats")
        if day_num in days:
            items = daily_dict.get("items", [])
            new_items = [it for it in items if it.get("time") != time_seconds]
            if len(new_items) < len(items):
                removed = True
            daily_dict["items"] = new_items

    if not removed:
        raise HomeAssistantError(
            f"No feeding item found at {time_str} on the specified days"
        )

    LOGGER.debug("Removing feeding item, new schedule: %s", feed_daily_list)
    await client.send_api_request(feeder.id, FeederCommand.SAVE_FEED, feed_daily_list)

    coordinator.enable_smart_polling(3)
    await coordinator.async_request_refresh()


def async_register_services(hass: HomeAssistant) -> None:
    """Register custom services for the Petkit integration."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_FEEDING_SCHEDULE):
        return

    async def _handle_get_schedule(call: ServiceCall) -> dict:
        return await async_get_feeding_schedule(hass, call)

    async def _handle_set_item(call: ServiceCall) -> None:
        await async_set_feeding_item(hass, call)

    async def _handle_remove_item(call: ServiceCall) -> None:
        await async_remove_feeding_item(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_FEEDING_SCHEDULE,
        _handle_get_schedule,
        schema=GET_SCHEDULE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_FEEDING_ITEM,
        _handle_set_item,
        schema=SET_FEEDING_ITEM_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_FEEDING_ITEM,
        _handle_remove_item,
        schema=REMOVE_FEEDING_ITEM_SCHEMA,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister custom services."""
    hass.services.async_remove(DOMAIN, SERVICE_GET_FEEDING_SCHEDULE)
    hass.services.async_remove(DOMAIN, SERVICE_SET_FEEDING_ITEM)
    hass.services.async_remove(DOMAIN, SERVICE_REMOVE_FEEDING_ITEM)
