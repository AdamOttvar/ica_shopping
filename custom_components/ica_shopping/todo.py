"""The ICA shopping list as a Home Assistant to-do list."""

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import IcaShoppingCoordinator
from .ica_api import utc_timestamp
from .sensor import row_text


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([IcaShoppingTodoList(entry.runtime_data, entry.title)])


class IcaShoppingTodoList(CoordinatorEntity[IcaShoppingCoordinator], TodoListEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "shopping_list"
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(self, coordinator: IcaShoppingCoordinator, list_name: str):
        super().__init__(coordinator)
        list_id = coordinator.list_id.lower()
        self._attr_unique_id = f"todo_{list_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, list_id)},
            name=f"ICA – {list_name}",
            manufacturer="ICA",
        )

    def _rows(self) -> list[dict]:
        return [r for r in self.coordinator.data.get("rows") or [] if r.get("offlineId")]

    @property
    def todo_items(self) -> list[TodoItem]:
        return [
            TodoItem(
                uid=row["offlineId"],
                summary=row_text(row),
                status=TodoItemStatus.COMPLETED
                if row.get("isStrikedOver")
                else TodoItemStatus.NEEDS_ACTION,
            )
            for row in self._rows()
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        await self.coordinator.async_add_item(item.summary.strip())

    async def async_update_todo_item(self, item: TodoItem) -> None:
        row = next((r for r in self._rows() if r["offlineId"] == item.uid), None)
        if row is None:
            return
        # Like the ICA app, send the whole row and not only the changed fields
        changed = {
            "offlineId": item.uid,
            "productName": row.get("productName"),
            "quantity": row.get("quantity"),
            "unit": row.get("unit"),
            "isStrikedOver": item.status == TodoItemStatus.COMPLETED,
            "latestChange": utc_timestamp(),
        }
        if item.summary is not None and item.summary.strip() != row_text(row):
            # Renamed, the new text replaces the name and any quantity
            changed.update(productName=item.summary.strip(), quantity=None, unit=None)
        await self.coordinator.async_sync({"changedRows": [changed]})

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        await self.coordinator.async_sync({"deletedRows": uids})
