"""An abstract class common to all Bond entities."""
from __future__ import annotations

from abc import abstractmethod
from asyncio import Lock, TimeoutError as AsyncIOTimeoutError
from datetime import timedelta
import logging
from typing import Any

from aiohttp import ClientError
from bond_api import BPUPSubscriptions

from homeassistant.const import (
    ATTR_HW_VERSION,
    ATTR_MODEL,
    ATTR_NAME,
    ATTR_SUGGESTED_AREA,
    ATTR_SW_VERSION,
    ATTR_VIA_DEVICE,
)
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .utils import BondDevice, BondHub

_LOGGER = logging.getLogger(__name__)

_FALLBACK_SCAN_INTERVAL = timedelta(seconds=10)


class BondEntity(Entity):
    """Generic Bond entity encapsulating common features of any Bond controlled device."""

    _attr_should_poll = False

    def __init__(
        self,
        hub: BondHub,
        device: BondDevice,
        bpup_subs: BPUPSubscriptions,
        sub_device: str | None = None,
    ) -> None:
        """Initialize entity with API and device info."""
        self._hub = hub
        self._device = device
        self._device_id = device.device_id
        self._sub_device = sub_device
        self._attr_available = True
        self._bpup_subs = bpup_subs
        self._update_lock: Lock | None = None
        self._initialized = False
        sub_device_id: str = f"_{sub_device}" if sub_device else ""
        self._attr_unique_id = f"{hub.bond_id}_{device.device_id}{sub_device_id}"
        if sub_device:
            sub_device_name = sub_device.replace("_", " ").title()
            self._attr_name = f"{device.name} {sub_device_name}"
        else:
            self._attr_name = device.name

    @property
    def device_info(self) -> DeviceInfo:
        """Get a an HA device representing this Bond controlled device."""
        device_info = DeviceInfo(
            manufacturer=self._hub.make,
            # type ignore: tuple items should not be Optional
            identifiers={(DOMAIN, self._hub.bond_id, self._device.device_id)},  # type: ignore[arg-type]
            configuration_url=f"http://{self._hub.host}",
        )
        if self.name is not None:
            device_info[ATTR_NAME] = self.name
        if self._hub.bond_id is not None:
            device_info[ATTR_VIA_DEVICE] = (DOMAIN, self._hub.bond_id)
        if self._device.location is not None:
            device_info[ATTR_SUGGESTED_AREA] = self._device.location
        if not self._hub.is_bridge:
            if self._hub.model is not None:
                device_info[ATTR_MODEL] = self._hub.model
            if self._hub.fw_ver is not None:
                device_info[ATTR_SW_VERSION] = self._hub.fw_ver
            if self._hub.mcu_ver is not None:
                device_info[ATTR_HW_VERSION] = self._hub.mcu_ver
        else:
            model_data = []
            if self._device.branding_profile:
                model_data.append(self._device.branding_profile)
            if self._device.template:
                model_data.append(self._device.template)
            if model_data:
                device_info[ATTR_MODEL] = " ".join(model_data)

        return device_info

    async def async_update(self) -> None:
        """Fetch assumed state of the cover from the hub using API."""
        await self._async_update_from_api()

    async def _async_update_if_bpup_not_alive(self, *_: Any) -> None:
        """Fetch via the API if BPUP is not alive."""
        if (
            self.hass.is_stopping
            or self._bpup_subs.alive
            and self._initialized
            and self.available
        ):
            return

        assert self._update_lock is not None
        if self._update_lock.locked():
            _LOGGER.warning(
                "Updating %s took longer than the scheduled update interval %s",
                self.entity_id,
                _FALLBACK_SCAN_INTERVAL,
            )
            return

        async with self._update_lock:
            await self._async_update_from_api()
            self.async_write_ha_state()

    async def _async_update_from_api(self) -> None:
        """Fetch via the API."""
        try:
            state: dict = await self._hub.bond.device_state(self._device_id)
        except (ClientError, AsyncIOTimeoutError, OSError) as error:
            if self.available:
                _LOGGER.warning(
                    "Entity %s has become unavailable", self.entity_id, exc_info=error
                )
            self._attr_available = False
        else:
            self._async_state_callback(state)
        self._attr_assumed_state = self._hub.is_bridge and not self._device.trust_state

    @abstractmethod
    def _apply_state(self, state: dict) -> None:
        raise NotImplementedError

    @callback
    def _async_state_callback(self, state: dict) -> None:
        """Process a state change."""
        self._initialized = True
        if not self.available:
            _LOGGER.info("Entity %s has come back", self.entity_id)
        self._attr_available = True
        _LOGGER.debug(
            "Device state for %s (%s) is:\n%s", self.name, self.entity_id, state
        )
        self._apply_state(state)

    @callback
    def _async_bpup_callback(self, state: dict) -> None:
        """Process a state change from BPUP."""
        self._async_state_callback(state)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to BPUP and start polling."""
        await super().async_added_to_hass()
        self._update_lock = Lock()
        self._bpup_subs.subscribe(self._device_id, self._async_bpup_callback)
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_update_if_bpup_not_alive, _FALLBACK_SCAN_INTERVAL
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from BPUP data on remove."""
        await super().async_will_remove_from_hass()
        self._bpup_subs.unsubscribe(self._device_id, self._async_bpup_callback)
