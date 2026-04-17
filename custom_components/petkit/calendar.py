"""Calendar platform for Petkit Smart Devices integration.

Exposes feeding schedules as calendar events.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pypetkitapi import Feeder

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER, POWER_ONLINE_STATE
from .utils import (
    is_dual_hopper,
    seconds_to_time,
    weekday_to_petkit_day,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import PetkitDataUpdateCoordinator
    from .data import PetkitConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up calendar entities for feeders with schedules."""
    devices = entry.runtime_data.client.petkit_entities.values()
    entities = [
        PetkitFeedingCalendar(
            coordinator=entry.runtime_data.coordinator,
            device=device,
        )
        for device in devices
        if isinstance(device, Feeder) and device.multi_feed_item is not None
    ]
    LOGGER.debug("CALENDAR : Adding %s feeding schedule calendars", len(entities))
    async_add_entities(entities)


class PetkitFeedingCalendar(
    CoordinatorEntity["PetkitDataUpdateCoordinator"],
    CalendarEntity,
):
    """Calendar entity representing a feeder's scheduled feeding plan."""

    _attr_has_entity_name = True
    _attr_translation_key = "feeding_schedule"

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        device: Feeder,
    ) -> None:
        """Initialize the calendar entity."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.device = device
        self._attr_unique_id = (
            f"{device.device_nfo.device_type}_{device.sn}_feeding_schedule"
        )

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._attr_unique_id

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        device_info = DeviceInfo(
            identifiers={(DOMAIN, self.device.sn)},
            manufacturer="Petkit",
            model=self.device.device_nfo.modele_name,
            model_id=self.device.device_nfo.device_type.upper(),
            name=self.device.name,
        )
        if self.device.mac is not None:
            device_info["connections"] = {(CONNECTION_NETWORK_MAC, self.device.mac)}
        if self.device.firmware is not None:
            device_info["sw_version"] = str(self.device.firmware)
        if self.device.hardware is not None:
            device_info["hw_version"] = str(self.device.hardware)
        if self.device.sn is not None:
            device_info["serial_number"] = str(self.device.sn)
        return device_info

    @property
    def available(self) -> bool:
        """Return if device is online."""
        state = getattr(self.coordinator.data.get(self.device.id), "state", None)
        return state.pim in POWER_ONLINE_STATE if hasattr(state, "pim") else True

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming feeding event."""
        updated_device = self.coordinator.data.get(self.device.id)
        if not updated_device or not isinstance(updated_device, Feeder):
            return None

        mfi = updated_device.multi_feed_item
        if not mfi or not mfi.feed_daily_list:
            return None

        now = dt_util.now()
        current_seconds = now.hour * 3600 + now.minute * 60 + now.second
        today_petkit_day = weekday_to_petkit_day(now.weekday())
        dual = is_dual_hopper(updated_device)

        # Search today first, then subsequent days
        for day_offset in range(8):
            check_day = ((today_petkit_day - 1 + day_offset) % 7) + 1
            daily = mfi.get_daily_list_for_day(check_day)
            if not daily or not daily.items or daily.suspended == 1:
                continue

            for item in sorted(daily.items, key=lambda x: x.time or 0):
                if item.time is None:
                    continue
                # For today, skip past events
                if day_offset == 0 and item.time <= current_seconds:
                    continue

                event_date = now.date() + timedelta(days=day_offset)
                t = seconds_to_time(item.time)
                start = datetime.combine(event_date, t, tzinfo=now.tzinfo)
                end = start + timedelta(minutes=1)

                summary = _format_feed_summary(item, dual)
                return CalendarEvent(
                    summary=summary,
                    start=start,
                    end=end,
                )

        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return all feeding events in the requested date range."""
        updated_device = self.coordinator.data.get(self.device.id)
        if not updated_device or not isinstance(updated_device, Feeder):
            return []

        mfi = updated_device.multi_feed_item
        if not mfi or not mfi.feed_daily_list:
            return []

        dual = is_dual_hopper(updated_device)
        events: list[CalendarEvent] = []

        tz = dt_util.get_default_time_zone()
        current_date = start_date.date() if isinstance(start_date, datetime) else start_date
        end = end_date.date() if isinstance(end_date, datetime) else end_date

        while current_date <= end:
            petkit_day = weekday_to_petkit_day(current_date.weekday())
            daily = mfi.get_daily_list_for_day(petkit_day)

            if daily and daily.items:
                suspended = daily.suspended == 1
                for item in daily.items:
                    if item.time is None:
                        continue

                    t = seconds_to_time(item.time)
                    start_dt = datetime.combine(current_date, t, tzinfo=tz)
                    end_dt = start_dt + timedelta(minutes=1)

                    summary = _format_feed_summary(item, dual)
                    if suspended:
                        summary = f"[Suspended] {summary}"

                    events.append(
                        CalendarEvent(
                            summary=summary,
                            start=start_dt,
                            end=end_dt,
                        )
                    )

            current_date += timedelta(days=1)

        return events


def _format_feed_summary(item, dual: bool) -> str:
    """Format a FeedItem into a readable calendar event summary."""
    t = seconds_to_time(item.time) if item.time is not None else None
    time_str = t.strftime("%H:%M") if t else "??:??"
    name = item.name or ""

    if dual:
        a1 = item.amount1 or 0
        a2 = item.amount2 or 0
        label = f"Feed: H1={a1}g H2={a2}g at {time_str}"
    else:
        amount = item.amount or 0
        label = f"Feed: {amount}g at {time_str}"

    if name:
        label = f"{label} ({name})"

    return label
