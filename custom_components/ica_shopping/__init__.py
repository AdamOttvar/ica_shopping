import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_AUTH_STATE,
    CONF_ICA_ID,
    CONF_ICA_PIN,
    CONF_REMOVE_STRIKED,
    DOMAIN,
)
from .coordinator import IcaShoppingCoordinator
from .ica_api import ICAApi

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "todo"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ADD_ITEM_SCHEMA = vol.Schema(
    {
        vol.Required("text"): cv.string,
        vol.Optional("list_id"): cv.string,
    }
)

# Settings of earlier versions, which logged in with a session cookie and
# could sync with a Google Keep to-do list
LEGACY_KEYS = ("session_id", "todo_entity_id")


def _loaded_coordinators(hass: HomeAssistant) -> list[IcaShoppingCoordinator]:
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


async def async_setup(hass: HomeAssistant, config) -> bool:
    async def handle_add_item(call: ServiceCall) -> None:
        text = call.data["text"].strip()
        if not text:
            raise ServiceValidationError("add_item needs a text")

        coordinators = _loaded_coordinators(hass)
        if not coordinators:
            raise ServiceValidationError("No ICA shopping list is loaded")

        list_id = call.data.get("list_id")
        coordinator = next(
            (c for c in coordinators if list_id and c.list_id.lower() == list_id.lower()),
            coordinators[0],
        )
        await coordinator.async_add_item(text, list_id)

    async def handle_refresh(call: ServiceCall) -> None:
        for coordinator in _loaded_coordinators(hass):
            await coordinator.async_refresh_list()

    hass.services.async_register(DOMAIN, "add_item", handle_add_item, schema=ADD_ITEM_SCHEMA)
    hass.services.async_register(DOMAIN, "refresh", handle_refresh)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not entry.data.get(CONF_ICA_ID) or not entry.data.get(CONF_ICA_PIN):
        # Set up with the old session cookie login, ask for personal ID and PIN
        raise ConfigEntryAuthFailed("ICA now needs your personal ID number and PIN")

    api = ICAApi(
        entry.data[CONF_ICA_ID], entry.data[CONF_ICA_PIN], entry.data.get(CONF_AUTH_STATE)
    )
    coordinator = IcaShoppingCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _options_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version > 1:
        # Downgraded from a future version
        return False

    if entry.minor_version < 2:
        # Drop the session cookie and Keep settings, move remove_striked to options
        data = {k: v for k, v in entry.data.items() if k not in LEGACY_KEYS}
        options = {k: v for k, v in entry.options.items() if k not in LEGACY_KEYS}
        if CONF_REMOVE_STRIKED in data:
            options.setdefault(CONF_REMOVE_STRIKED, data.pop(CONF_REMOVE_STRIKED))
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=1, minor_version=2
        )
        _LOGGER.info("Migrated ICA Shopping entry, log in again with personal ID and PIN")

    return True
