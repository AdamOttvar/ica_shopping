from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import requests
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .authenticator import IcaAuthError
from .const import (
    CONF_AUTH_STATE,
    CONF_ICA_ID,
    CONF_ICA_LIST_ID,
    CONF_ICA_PIN,
    CONF_REMOVE_STRIKED,
    CONF_TODO_ENTITY_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import entry_option
from .ica_api import ICAApi

_LOGGER = logging.getLogger(__name__)

CONF_SCAN_INTERVAL = "scan_interval"


async def _async_login(
    hass: HomeAssistant, personal_id: str, pin: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Log in to ICA. Returns the auth state and the account's shopping lists."""
    api = ICAApi(personal_id, pin, None)

    def login():
        api.ensure_login()
        return api.get_shopping_lists()

    lists = await hass.async_add_executor_job(login)
    return api.auth_state, [x for x in lists if x.get("offlineId")]


def _login_error(err: Exception) -> str:
    if isinstance(err, IcaAuthError):
        return "invalid_auth"
    if isinstance(err, requests.exceptions.RequestException):
        response = getattr(err, "response", None)
        if response is not None and response.status_code == 401:
            return "invalid_auth"
        return "cannot_connect"
    return "unknown"


def _find_list(lists: list[dict[str, Any]], list_id: str | None) -> dict | None:
    if not list_id:
        return None
    # ICA returns upper case ids, while ids copied from ica.se are lower case
    return next(
        (x for x in lists if str(x["offlineId"]).lower() == list_id.lower()), None
    )


def _list_selector(lists: list[dict[str, Any]]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=x["offlineId"], label=x.get("title") or x["offlineId"])
                for x in lists
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _todo_field(default: str | None) -> dict:
    """Optional to-do entity to sync with, cleared by leaving it empty."""
    return {
        vol.Optional(
            CONF_TODO_ENTITY_ID, description={"suggested_value": default}
        ): EntitySelector(EntitySelectorConfig(domain="todo"))
    }


def _settings(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_TODO_ENTITY_ID: user_input.get(CONF_TODO_ENTITY_ID) or None,
        CONF_REMOVE_STRIKED: user_input[CONF_REMOVE_STRIKED],
        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
    }


def _scan_interval_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=1, max=1440, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min"
        )
    )


class ICAConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._login_data: dict[str, Any] = {}
        self._lists: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ICAOptionsFlowHandler()

    async def _async_try_login(
        self, personal_id: str, pin: str, errors: dict[str, str]
    ) -> bool:
        try:
            auth_state, self._lists = await _async_login(self.hass, personal_id, pin)
        except Exception as err:  # noqa: BLE001
            errors["base"] = _login_error(err)
            if errors["base"] == "unknown":
                _LOGGER.exception("Unexpected error during ICA login")
            return False

        if not self._lists:
            errors["base"] = "no_lists"
            return False

        self._login_data = {
            CONF_ICA_ID: personal_id,
            CONF_ICA_PIN: pin,
            CONF_AUTH_STATE: auth_state,
        }
        return True

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            personal_id = user_input[CONF_ICA_ID].strip().replace("-", "")
            if await self._async_try_login(personal_id, user_input[CONF_ICA_PIN], errors):
                return await self.async_step_list()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ICA_ID): str,
                    vol.Required(CONF_ICA_PIN): str,
                }
            ),
            errors=errors,
        )

    async def async_step_list(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the shopping list to use."""
        if user_input is not None:
            the_list = _find_list(self._lists, user_input[CONF_ICA_LIST_ID])
            list_id = the_list["offlineId"]
            await self.async_set_unique_id(list_id.lower())
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=the_list.get("title") or list_id,
                data={**self._login_data, CONF_ICA_LIST_ID: list_id},
                options=_settings(user_input),
            )

        # The list of the built-in Shopping list integration, if it is set up
        default_todo = (
            "todo.shopping_list" if self.hass.states.get("todo.shopping_list") else None
        )

        return self.async_show_form(
            step_id="list",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ICA_LIST_ID): _list_selector(self._lists),
                    **_todo_field(default_todo),
                    vol.Required(CONF_REMOVE_STRIKED, default=True): BooleanSelector(),
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): _scan_interval_selector(),
                }
            ),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Log in again, also used to move from the old session cookie login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            personal_id = user_input[CONF_ICA_ID].strip().replace("-", "")
            if await self._async_try_login(personal_id, user_input[CONF_ICA_PIN], errors):
                the_list = _find_list(self._lists, entry_option(entry, CONF_ICA_LIST_ID))
                if the_list:
                    return self._async_finish_reauth(entry, the_list["offlineId"])
                return await self.async_step_reauth_list()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ICA_ID, default=entry.data.get(CONF_ICA_ID, "")
                    ): str,
                    vol.Required(CONF_ICA_PIN): str,
                }
            ),
            description_placeholders={"account": entry.title},
            errors=errors,
        )

    async def async_step_reauth_list(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The configured list was not found in the account, pick a new one."""
        entry = self._get_reauth_entry()
        if user_input is not None:
            return self._async_finish_reauth(entry, user_input[CONF_ICA_LIST_ID])

        return self.async_show_form(
            step_id="reauth_list",
            data_schema=vol.Schema(
                {vol.Required(CONF_ICA_LIST_ID): _list_selector(self._lists)}
            ),
        )

    @callback
    def _async_finish_reauth(self, entry: ConfigEntry, list_id: str) -> ConfigFlowResult:
        # The list id may have been set by an earlier options flow
        options = {k: v for k, v in entry.options.items() if k != CONF_ICA_LIST_ID}
        return self.async_update_reload_and_abort(
            entry,
            unique_id=list_id.lower(),
            data_updates={**self._login_data, CONF_ICA_LIST_ID: list_id},
            options=options,
        )


class ICAOptionsFlowHandler(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self.async_create_entry(
                data={**entry.options, **_settings(user_input)}
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    **_todo_field(entry_option(entry, CONF_TODO_ENTITY_ID)),
                    vol.Required(
                        CONF_REMOVE_STRIKED,
                        default=entry_option(entry, CONF_REMOVE_STRIKED, True),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=entry_option(
                            entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): _scan_interval_selector(),
                }
            ),
        )
