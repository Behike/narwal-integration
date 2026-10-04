"""Select entities for Narwal robot vacuum."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NarwalConfigEntry
from .const import (
    CLEAN_MODE_LIST,
    CLEAN_MODE_MAP,
    MOP_HUMIDITY_LIST,
    MOP_HUMIDITY_MAP,
)
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CleanMode, MopHumidity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal select entities."""
    coordinator = entry.runtime_data
    async_add_entities([
        NarwalCleanModeSelect(coordinator),
        NarwalMopHumiditySelect(coordinator),
    ])


class NarwalCleanModeSelect(NarwalEntity, SelectEntity, RestoreEntity):
    """Select entity for choosing the cleaning mode."""

    _attr_translation_key = "clean_mode"
    _attr_icon = "mdi:spray-bottle"
    _attr_options = CLEAN_MODE_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data['device_name']}_clean_mode"
        )
        self._attr_current_option = next(
            k for k, v in CLEAN_MODE_MAP.items()
            if v == coordinator.selected_clean_mode
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last selected mode across restarts."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in CLEAN_MODE_MAP:
            await self.async_select_option(last.state)

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        val = CLEAN_MODE_MAP.get(option)
        if val is not None:
            self.coordinator.selected_clean_mode = CleanMode(val)
        self.async_write_ha_state()


class NarwalMopHumiditySelect(NarwalEntity, SelectEntity, RestoreEntity):
    """Mop wetness used for the next clean that involves mopping."""

    _attr_translation_key = "mop_humidity"
    _attr_icon = "mdi:water-percent"
    _attr_options = MOP_HUMIDITY_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data['device_name']}_mop_humidity"
        )
        self._attr_current_option = next(
            k for k, v in MOP_HUMIDITY_MAP.items()
            if v == coordinator.selected_mop_humidity
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last selected humidity across restarts."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in MOP_HUMIDITY_MAP:
            await self.async_select_option(last.state)

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        val = MOP_HUMIDITY_MAP.get(option)
        if val is not None:
            self.coordinator.selected_mop_humidity = MopHumidity(val)
        self.async_write_ha_state()
