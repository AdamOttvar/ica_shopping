# 🛒 ICA Shopping – Home Assistant Integration

Sync an ICA shopping list with Home Assistant, and add items to it with the
Home Assistant voice assistant.

The integration logs in the same way as the ICA app, with your personal ID number
and ICA PIN code. The login is renewed automatically, so there is no session
cookie to copy and update every few months.

## Limitations

**ICA API**  
Changes made to your ICA shopping list (e.g. via the ICA app or website) are fetched
on the update interval (15 minutes by default), or when the `ica_shopping.refresh`
action is called. ICA does not push updates.

## Installation via HACS

1. Open HACS in Home Assistant.
1. Click on the ... in the top right corner and select Custom repositories.
1. Add the URL https://github.com/AdamOttvar/ica_shopping/
1. Set the category to Integration and click Add.
1. Download the integration and restart Home Assistant.

## Configuration

1. Go to Settings -> Devices & Services.
1. Click Add Integration.
1. Search for and select ICA Shopping.
1. Log in with your personal ID number (10 or 12 digits) and ICA PIN code.
1. Choose the shopping list to use, whether checked items should be removed when
   the list is refreshed, and how often to fetch the list.

To use several shopping lists, add the integration once per list.

### Upgrading from the session ID version

Earlier versions logged in with the `thSessionId` cookie and could sync with a
Google Keep to-do list. After upgrading, Home Assistant asks you to log in again
(Settings -> Devices & Services shows that the integration needs attention). Enter your personal ID
number and PIN, and the integration keeps using the same list and sensor. The Google
Keep sync has been removed.

## Entities and actions

- `sensor.ica_<list>_shoppinglist` – the number of items in the list. The attributes
  hold the list name (`list_name`), the items (`item_1`, `item_2`, ...) and the
  checked items (`striked_item_1`, ...).
- `ica_shopping.add_item` – adds an item (`text`) to the list. `list_id` can be set to
  add to another list in the same ICA account.
- `ica_shopping.refresh` – fetches the list from ICA. If enabled in the options, the
  items checked in ICA are removed first.

## Examples

### Automations for update
You can refresh the list when you enter or leave a store.
This only works for ICA stores you've added as zones in Home Assistant.
Purchases made in other locations won't trigger any update.

```yaml
alias: Ica shopping update
description: ""
triggers:
  - trigger: zone
    entity_id: device_tracker.max
    zone: zone.ica
    event: leave
  - trigger: zone
    entity_id: device_tracker.max
    zone: zone.ica
    event: enter
conditions: []
actions:
  - action: ica_shopping.refresh
    data: {}
mode: single

```

### Voice assistant intents
Copy the files in `custom_sentences/sv/` to `config/custom_sentences/sv/` in Home Assistant, and add this to configuration.yaml to handle the intents. This overrides the shopping list built into the voice assistant, so items end up in the ICA list.

```yaml
intent_script:
  # Röststyrning för att lägga till i ICAs inköpslista
  IcaAddItem:
    action:
      - action: ica_shopping.add_item
        data:
          text: >
            {{ item | trim }}
    speech:
      text: "Okej, jag lade till {{ item }}."

  # Röststyrning för att uppdatera inköpslistan
  IcaRefresh:
    action:
      - action: ica_shopping.refresh
    speech:
      text: "Jag uppdaterade inköpslistan."

  # Override för interna shoppinglist (Home Assistant built-in)
  HassShoppingListAddItem:
    action:
      - action: ica_shopping.add_item
        data:
          text: "{{ name | trim }}"
    speech:
      text: "Okej, jag lade till {{ name }} på ICA-listan."
```
