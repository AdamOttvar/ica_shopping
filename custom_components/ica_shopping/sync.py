"""Two-way sync between the ICA list and a Home Assistant to-do entity.

Both lists are compared with the state after the last sync, so it is known on
which side an item was added, removed, checked or unchecked. Items are matched
on their text, ignoring case.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store

from .coordinator import IcaShoppingCoordinator
from .ica_api import utc_timestamp
from .sensor import row_text

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
DEBOUNCE_SECONDS = 1


def _key(text: str) -> str:
    return " ".join(text.split()).casefold()


class TodoSync:
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: IcaShoppingCoordinator,
        todo_entity_id: str,
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.todo_entity_id = todo_entity_id
        self._store = self._get_store(hass, entry)
        # Item key -> checked, after the last sync. None until the first sync.
        self._synced: dict[str, bool] | None = None
        self._lock = asyncio.Lock()
        self._debounce_unsub: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []

    @staticmethod
    def _get_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, Any]]:
        return Store(hass, STORAGE_VERSION, f"ica_shopping.{entry.entry_id}.sync")

    @classmethod
    async def async_remove_storage(cls, hass: HomeAssistant, entry: ConfigEntry) -> None:
        await cls._get_store(hass, entry).async_remove()

    async def async_start(self) -> None:
        stored = await self._store.async_load()
        if stored and stored.get("todo_entity_id") == self.todo_entity_id:
            self._synced = stored.get("items")

        self._unsubs = [
            self.coordinator.async_add_listener(self._schedule_sync),
            async_track_state_change_event(
                self.hass, [self.todo_entity_id], self._handle_event
            ),
            # Changes that don't change the state, e.g. removing a checked item
            self.hass.bus.async_listen("call_service", self._handle_call_service),
            self.hass.bus.async_listen("shopping_list_updated", self._handle_event),
        ]
        self._schedule_sync()

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._debounce_unsub:
            self._debounce_unsub()
            self._debounce_unsub = None

    @callback
    def _handle_call_service(self, event: Event) -> None:
        # get_items is also called by the sync itself
        if event.data.get("domain") != "todo" or event.data.get("service") == "get_items":
            return
        entity_ids = (event.data.get("service_data") or {}).get("entity_id") or []
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if self.todo_entity_id in entity_ids:
            self._schedule_sync()

    @callback
    def _handle_event(self, event: Event) -> None:
        self._schedule_sync()

    @callback
    def _schedule_sync(self) -> None:
        if self._debounce_unsub:
            self._debounce_unsub()
        self._debounce_unsub = async_call_later(
            self.hass, DEBOUNCE_SECONDS, self._async_run_sync
        )

    async def _async_run_sync(self, _now=None) -> None:
        self._debounce_unsub = None
        try:
            await self.async_sync()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not sync ICA with %s", self.todo_entity_id)

    async def _async_get_todo_items(self) -> list[dict[str, Any]] | None:
        if self.hass.states.get(self.todo_entity_id) is None:
            return None
        result = await self.hass.services.async_call(
            "todo",
            "get_items",
            {"entity_id": self.todo_entity_id},
            blocking=True,
            return_response=True,
        )
        return (result or {}).get(self.todo_entity_id, {}).get("items", [])

    async def async_sync(self) -> None:
        async with self._lock:
            if not self.coordinator.last_update_success or not self.coordinator.data:
                return
            todo_items = await self._async_get_todo_items()
            if todo_items is None:
                _LOGGER.debug("%s is not available, skipping sync", self.todo_entity_id)
                return

            ha: dict[str, dict[str, Any]] = {}
            for item in todo_items:
                if summary := (item.get("summary") or "").strip():
                    ha.setdefault(_key(summary), item)
            ica: dict[str, list[dict[str, Any]]] = {}
            for row in self.coordinator.data.get("rows") or []:
                if row.get("offlineId") and (text := row_text(row)):
                    ica.setdefault(_key(text), []).append(row)

            # Without an earlier sync, nothing is removed, only added on both sides
            synced = self._synced or {}
            first_sync = self._synced is None
            result: dict[str, bool] = {}
            ha_add: list[str] = []
            ha_remove: list[str] = []
            ha_status: list[tuple[str, bool]] = []
            ica_created: list[dict[str, Any]] = []
            ica_changed: list[dict[str, Any]] = []
            ica_deleted: list[str] = []

            for key, item in ha.items():
                ha_checked = item.get("status") == "completed"
                rows = ica.get(key)
                if rows is None:
                    if key in synced and not first_sync:
                        ha_remove.append(item["uid"])  # Removed in ICA
                    else:
                        ica_created.append(self._new_row(item["summary"], ha_checked))
                        result[key] = ha_checked
                    continue

                ica_checked = rows[0].get("isStrikedOver", False)
                if ha_checked != ica_checked:
                    if key in synced and synced[key] == ica_checked:
                        # Checked or unchecked in Home Assistant
                        ica_changed.extend(self._changed_row(r, ha_checked) for r in rows)
                        ica_checked = ha_checked
                    else:
                        ha_status.append((item["uid"], ica_checked))
                result[key] = ica_checked

            for key, rows in ica.items():
                if key in ha:
                    continue
                if key in synced and not first_sync:
                    ica_deleted.extend(r["offlineId"] for r in rows)  # Removed in HA
                else:
                    ha_add.append(row_text(rows[0]))
                    ica_checked = rows[0].get("isStrikedOver", False)
                    if ica_checked:
                        ha_status.append((row_text(rows[0]), True))
                    result[key] = ica_checked

            await self._async_apply_to_ha(ha_add, ha_remove, ha_status)
            changes = {
                k: v
                for k, v in (
                    ("createdRows", ica_created),
                    ("changedRows", ica_changed),
                    ("deletedRows", ica_deleted),
                )
                if v
            }
            if changes:
                _LOGGER.debug("Syncing to ICA: %s", changes)
                await self.coordinator.async_sync(changes)

            if result != self._synced:
                self._synced = result
                await self._store.async_save(
                    {"todo_entity_id": self.todo_entity_id, "items": result}
                )

    async def _async_apply_to_ha(
        self,
        add: list[str],
        remove: list[str],
        status: list[tuple[str, bool]],
    ) -> None:
        for summary in add:
            _LOGGER.debug("Adding '%s' to %s", summary, self.todo_entity_id)
            await self._call_todo("add_item", {"item": summary})
        if remove:
            _LOGGER.debug("Removing %s from %s", remove, self.todo_entity_id)
            await self._call_todo("remove_item", {"item": remove})
        for item, checked in status:
            await self._call_todo(
                "update_item",
                {"item": item, "status": "completed" if checked else "needs_action"},
            )

    async def _call_todo(self, service: str, data: dict[str, Any]) -> None:
        await self.hass.services.async_call(
            "todo", service, {"entity_id": self.todo_entity_id, **data}, blocking=True
        )

    @staticmethod
    def _new_row(text: str, checked: bool) -> dict[str, Any]:
        return {
            "offlineId": str(uuid.uuid4()),
            "productName": text.strip(),
            "isStrikedOver": checked,
            "sourceId": -1,
            "latestChange": utc_timestamp(),
        }

    @staticmethod
    def _changed_row(row: dict[str, Any], checked: bool) -> dict[str, Any]:
        # Like the ICA app, send the whole row and not only the changed field
        return {
            "offlineId": row["offlineId"],
            "productName": row.get("productName"),
            "quantity": row.get("quantity"),
            "unit": row.get("unit"),
            "isStrikedOver": checked,
            "latestChange": utc_timestamp(),
        }
