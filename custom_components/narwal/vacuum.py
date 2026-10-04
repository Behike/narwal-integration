"""Vacuum entity for Narwal robot vacuum."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import CLEAN_MODE_MAP, FAN_SPEED_LIST, FAN_SPEED_MAP, FAN_SPEED_REVERSE
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CleanMode, FanLevel, NarwalCommandError, WorkingStatus

_LOGGER = logging.getLogger(__name__)

WORKING_STATUS_TO_ACTIVITY: dict[WorkingStatus, VacuumActivity] = {
    WorkingStatus.UNKNOWN: VacuumActivity.IDLE,
    WorkingStatus.STANDBY: VacuumActivity.IDLE,
    WorkingStatus.PAUSED: VacuumActivity.PAUSED,
    WorkingStatus.SLEEPING: VacuumActivity.IDLE,
    WorkingStatus.CLEANING: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING_ALT: VacuumActivity.CLEANING,
    WorkingStatus.RETURNING: VacuumActivity.RETURNING,
    WorkingStatus.CHARGING: VacuumActivity.DOCKED,
    WorkingStatus.MOP_WASHING: VacuumActivity.DOCKED,
    WorkingStatus.MOP_DRYING: VacuumActivity.DOCKED,
    WorkingStatus.DOCKED: VacuumActivity.DOCKED,
    WorkingStatus.DUST_COLLECTING: VacuumActivity.DOCKED,
    WorkingStatus.UPGRADING: VacuumActivity.IDLE,
    WorkingStatus.SELF_CHECK: VacuumActivity.IDLE,
    WorkingStatus.CHARGED: VacuumActivity.DOCKED,
    WorkingStatus.ERROR: VacuumActivity.ERROR,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal vacuum entity."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalVacuum(coordinator)])


class NarwalVacuum(NarwalEntity, StateVacuumEntity):
    """Representation of a Narwal robot vacuum."""

    _attr_translation_key = "narwal"
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.SEND_COMMAND
    )
    _attr_fan_speed_list = FAN_SPEED_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['device_name']}_vacuum"

    @property
    def available(self) -> bool:
        state = self.coordinator.data
        return state is not None and state.device_reachable

    @property
    def activity(self) -> VacuumActivity:
        state = self.coordinator.data
        if state is None:
            return VacuumActivity.IDLE

        return WORKING_STATUS_TO_ACTIVITY.get(
            state.working_status, VacuumActivity.IDLE
        )

    @property
    def fan_speed(self) -> str | None:
        return FAN_SPEED_REVERSE.get(self.coordinator.selected_fan_level)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.data
        attrs: dict[str, Any] = {}
        if state and state.rooms:
            attrs["rooms"] = {
                r.room_id: r.display_name for r in state.rooms
            }
        if state and state.error_code is not None:
            # Also here (not only on the status sensor) so automations
            # triggered by the vacuum turning to "error" see the cause:
            # the sensor's state is written after the vacuum's.
            attrs["error_code"] = f"0x{state.error_code:08X}"
            attrs["error_reason"] = state.error_reason
        return attrs

    async def async_start(self) -> None:
        state = self.coordinator.data
        if state and state.is_paused:
            resp = await self.coordinator.client.resume()
            if not resp.success:
                _LOGGER.warning("Resume returned code=%s", resp.result_code)
        else:
            mode = self.coordinator.selected_clean_mode
            resp = await self.coordinator.client.start_plan(
                mode=mode,
                fan_level=self.coordinator.selected_fan_level,
                mop_humidity=self.coordinator.selected_mop_humidity,
            )
            if not resp.success:
                _LOGGER.warning(
                    "Start returned code=%s (mode=%s, status=%s)",
                    resp.result_code, mode.name,
                    state.working_status.name if state else "unknown",
                )

    async def async_stop(self, **kwargs: Any) -> None:
        resp = await self.coordinator.client.stop()
        if not resp.success:
            _LOGGER.warning("Stop returned code=%s", resp.result_code)

    async def async_pause(self) -> None:
        resp = await self.coordinator.client.pause()
        if not resp.success:
            _LOGGER.warning("Pause returned code=%s", resp.result_code)

    async def async_return_to_base(self, **kwargs: Any) -> None:
        resp = await self.coordinator.client.return_to_base()
        if not resp.success:
            _LOGGER.warning("Return-to-base returned code=%s", resp.result_code)

    async def async_locate(self, **kwargs: Any) -> None:
        await self.coordinator.client.locate()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        level = FAN_SPEED_MAP.get(fan_speed)
        if level is None:
            return
        # Used for the next start; also pushed live if a clean is running.
        self.coordinator.selected_fan_level = FanLevel(level)
        self.async_write_ha_state()
        state = self.coordinator.data
        if state and state.is_cleaning:
            try:
                await self.coordinator.client.set_fan_speed(FanLevel(level))
            except NarwalCommandError:
                _LOGGER.warning("Live fan speed change not acknowledged")

    async def async_send_command(
        self, command: str, params: dict[str, Any] | list[Any] | None = None, **kwargs: Any,
    ) -> None:
        """Handle vacuum.send_command for room-specific cleaning.

        Usage:
          service: vacuum.send_command
          data:
            entity_id: vacuum.narwal_...
            command: clean_rooms
            params:
              rooms: [1, 2, 5]
              mode: "Vacuum & Mop"   # optional
        """
        if command == "clean_rooms":
            p = params if isinstance(params, dict) else {}
            room_ids = p.get("rooms", [])
            mode_name = p.get("mode")

            mode: CleanMode | None = None
            if mode_name and mode_name in CLEAN_MODE_MAP:
                mode = CleanMode(CLEAN_MODE_MAP[mode_name])
            else:
                mode = self.coordinator.selected_clean_mode

            if not room_ids:
                _LOGGER.warning("clean_rooms called without room IDs")
                return

            state = self.coordinator.data
            _LOGGER.info(
                "Starting room clean: rooms=%s mode=%s vacuum_status=%s",
                room_ids, mode,
                state.working_status.name if state else "unknown",
            )
            resp = await self.coordinator.client.start_plan(
                mode=mode, room_ids=room_ids,
                fan_level=self.coordinator.selected_fan_level,
                mop_humidity=self.coordinator.selected_mop_humidity,
            )
            if not resp.success:
                _LOGGER.warning(
                    "Room clean returned code=%s (rooms=%s, mode=%s, status=%s)",
                    resp.result_code, room_ids, mode.name if mode else "None",
                    state.working_status.name if state else "unknown",
                )
        else:
            _LOGGER.warning("Unknown send_command: %s", command)
