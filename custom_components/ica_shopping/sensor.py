from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import IcaShoppingCoordinator


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ShoppingListSensor(entry.runtime_data, entry.title)])


def _format_quantity(quantity) -> str:
    return f"{quantity:g}".replace(".", ",") if isinstance(quantity, float) else str(quantity)


def row_text(row: dict) -> str:
    """Text of a row as shown in the ICA app, e.g. '2 st Gurka'."""
    name = (row.get("productName") or "").strip()
    quantity = row.get("quantity")
    if not quantity:
        return name
    return " ".join(x for x in (_format_quantity(quantity), row.get("unit"), name) if x)


class ShoppingListSensor(CoordinatorEntity[IcaShoppingCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Shoppinglist"
    _attr_native_unit_of_measurement = "items"

    def __init__(self, coordinator: IcaShoppingCoordinator, list_name: str):
        super().__init__(coordinator)
        list_id = coordinator.list_id.lower()
        self._attr_unique_id = f"shoppinglist_{list_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, list_id)},
            name=f"ICA – {list_name}",
            manufacturer="ICA",
        )

    @property
    def native_value(self):
        return len(self.coordinator.data.get("rows") or [])

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        attributes = {"list_name": data.get("title", "")}
        item_idx = 1
        striked_idx = 1
        for row in data.get("rows") or []:
            if row.get("isStrikedOver", False):
                attributes[f"striked_item_{striked_idx}"] = row_text(row)
                striked_idx += 1
            else:
                attributes[f"item_{item_idx}"] = row_text(row)
                item_idx += 1
        return attributes
