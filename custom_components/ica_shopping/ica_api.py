"""Client for the ICA shopping list API.

All methods are blocking and are run in the executor by the coordinator.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

import requests

from .authenticator import IcaAuthenticator
from .const import API_LIST, API_LIST_SYNC, API_LISTS

_LOGGER = logging.getLogger(__name__)

# Seconds to wait for the ICA API before giving up (connect, read)
REQUEST_TIMEOUT = (10, 30)


def utc_timestamp() -> str:
    """ICA style UTC timestamp, e.g. '2024-03-10T12:00:00Z'."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ICAApi:
    def __init__(self, personal_id: str, pin: str, auth_state: dict[str, Any] | None):
        self._session = requests.Session()
        self._auth = IcaAuthenticator(personal_id, pin, auth_state, self._session)

    @property
    def auth_state(self) -> dict[str, Any]:
        return self._auth.auth_state

    def token_needs_refresh(self) -> bool:
        return self._auth.token_needs_refresh()

    def ensure_login(self, refresh: bool = False) -> dict[str, Any]:
        return self._auth.ensure_login(refresh=refresh)

    def _headers(self, with_content: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._auth.access_token}",
            "Accept": "application/json",
        }
        if with_content:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return headers

    def _request(self, method: str, url: str, json_data: Any | None = None):
        response = self._session.request(
            method,
            url,
            headers=self._headers(with_content=json_data is not None),
            json=json_data,
            timeout=REQUEST_TIMEOUT,
        )
        _LOGGER.debug("HTTP [%s] %s -> %s", method, url, response.status_code)
        if not response.ok:
            _LOGGER.debug("Response body: %s", response.text[:500])
        response.raise_for_status()
        return response.json() if response.content else None

    def get_shopping_lists(self) -> list[dict[str, Any]]:
        data = self._request("GET", API_LISTS) or {}
        return data.get("shoppingLists") or []

    def get_shopping_list(self, list_id: str) -> dict[str, Any]:
        return self._request("GET", API_LIST.format(list_id=list_id))

    def sync_shopping_list(self, list_id: str, changes: dict[str, Any]) -> dict | None:
        """Push created/changed/deleted rows, returns the updated list."""
        return self._request("POST", API_LIST_SYNC.format(list_id=list_id), changes)

    def add_item(self, list_id: str, text: str) -> dict | None:
        row = {
            "offlineId": str(uuid.uuid4()),
            "productName": text,
            "isStrikedOver": False,
            "sourceId": -1,
            "latestChange": utc_timestamp(),
        }
        return self.sync_shopping_list(list_id, {"createdRows": [row]})

    def remove_items(self, list_id: str, row_ids: list[str]) -> dict | None:
        """Delete rows by their offlineId."""
        return self.sync_shopping_list(list_id, {"deletedRows": row_ids})
