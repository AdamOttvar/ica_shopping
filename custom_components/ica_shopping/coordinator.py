"""Keeps the configured ICA shopping list up to date."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

import requests

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .authenticator import IcaAuthError
from .const import (
    CONF_AUTH_STATE,
    CONF_ICA_LIST_ID,
    CONF_REMOVE_STRIKED,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .ica_api import ICAApi

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


def entry_option(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    """Read a setting, where the options flow overrides the initial setup."""
    return entry.options.get(key, entry.data.get(key, default))


class IcaShoppingCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches the shopping list and handles the ICA login."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: ICAApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=entry_option(entry, "scan_interval", DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.api = api
        self.list_id: str = entry_option(entry, CONF_ICA_LIST_ID)

    @property
    def remove_striked(self) -> bool:
        return bool(entry_option(self.config_entry, CONF_REMOVE_STRIKED, True))

    async def _call(self, func: Callable[[], T]) -> T:
        """Run an API call in the executor, logging in or renewing the token as needed.

        Raises `IcaAuthError` when the credentials no longer work.
        """
        auth_before = self.api.auth_state
        try:
            if self.api.token_needs_refresh():
                await self.hass.async_add_executor_job(self.api.ensure_login, True)
            try:
                return await self.hass.async_add_executor_job(func)
            except requests.exceptions.HTTPError as err:
                if err.response is None or err.response.status_code != 401:
                    raise
                _LOGGER.debug("ICA request was unauthorized, renewing the login")
                await self.hass.async_add_executor_job(self.api.ensure_login, True)
                return await self.hass.async_add_executor_job(func)
        finally:
            if self.api.auth_state != auth_before:
                self._persist_auth_state()

    def _persist_auth_state(self) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_AUTH_STATE: self.api.auth_state},
        )
        _LOGGER.debug("Saved the renewed ICA login")

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            the_list = await self._call(lambda: self.api.get_shopping_list(self.list_id))
        except IcaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except requests.exceptions.HTTPError as err:
            if err.response is not None and err.response.status_code == 404:
                raise UpdateFailed(f"ICA shopping list {self.list_id} was not found") from err
            raise UpdateFailed(f"Error communicating with ICA: {err}") from err
        except (requests.exceptions.RequestException, ValueError) as err:
            raise UpdateFailed(f"Error communicating with ICA: {err}") from err

        if not isinstance(the_list, dict):
            raise UpdateFailed("Unexpected response from ICA")
        return the_list

    async def _async_api_action(self, func: Callable[[], T]) -> T:
        """Run a service action, turning API errors into errors shown to the user."""
        try:
            return await self._call(func)
        except IcaAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"ICA login failed: {err}") from err
        except (requests.exceptions.RequestException, ValueError) as err:
            raise HomeAssistantError(f"Error communicating with ICA: {err}") from err

    def _apply_updated_list(self, list_id: str, updated: Any) -> bool:
        """Use the list returned by a sync, if it is the tracked one."""
        if list_id.lower() != self.list_id.lower():
            return True
        if isinstance(updated, dict) and isinstance(updated.get("rows"), list):
            self.async_set_updated_data(updated)
            return True
        return False

    async def async_add_item(self, text: str, list_id: str | None = None) -> None:
        list_id = list_id or self.list_id
        updated = await self._async_api_action(lambda: self.api.add_item(list_id, text))
        _LOGGER.info("Added '%s' to ICA list %s", text, list_id)
        if not self._apply_updated_list(list_id, updated):
            await self.async_request_refresh()

    async def async_sync(self, changes: dict[str, Any]) -> None:
        """Push created/changed/deleted rows of the tracked list."""
        updated = await self._async_api_action(
            lambda: self.api.sync_shopping_list(self.list_id, changes)
        )
        if not self._apply_updated_list(self.list_id, updated):
            await self.async_request_refresh()

    async def async_refresh_list(self) -> None:
        """Fetch the list, and remove the checked items if configured to."""
        if self.remove_striked:
            the_list = await self._async_api_action(
                lambda: self.api.get_shopping_list(self.list_id)
            )
            striked = [
                row["offlineId"]
                for row in (the_list or {}).get("rows", [])
                if row.get("isStrikedOver") and row.get("offlineId")
            ]
            if striked:
                updated = await self._async_api_action(
                    lambda: self.api.remove_items(self.list_id, striked)
                )
                _LOGGER.info("Removed %s checked items from ICA", len(striked))
                if self._apply_updated_list(self.list_id, updated):
                    return
        await self.async_refresh()
