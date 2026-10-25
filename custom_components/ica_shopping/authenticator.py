"""Login to ICA with personal ID number and PIN.

Mirrors the OAuth flow of the ICA app (as done in LazyTarget/ha-ica-todo):
a client is registered dynamically, then an Authorization Code + PKCE login
is made with the personal ID number and PIN. The resulting token is renewed
with the refresh token, and a full login is only made when that fails.
"""

from __future__ import annotations

import base64
import copy
import datetime
import hashlib
import logging
import re
from os import urandom
from typing import Any

import homeassistant.util.dt as dt_util
import jwt
import requests

from .const import (
    APP_REGISTRATION_CLIENT_ID,
    APP_REGISTRATION_CLIENT_SECRET,
    APP_REGISTRATION_ENDPOINT,
    AUTH_ACR,
    AUTH_BASE_URL,
    AUTH_REDIRECT_URI,
    LOGIN_ENDPOINT,
    OAUTH2_AUTHORIZE_ENDPOINT,
    OAUTH2_TOKEN_ENDPOINT,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
DEFAULT_TOKEN_LIFETIME = 2592000  # 30 days


class IcaAuthError(Exception):
    """Login to ICA failed, e.g. due to an invalid personal ID number or PIN."""


class IcaAuthenticator:
    """Handles login and token renewal against ICA."""

    def __init__(
        self,
        personal_id: str,
        pin: str,
        auth_state: dict[str, Any] | None,
        session: requests.Session,
    ) -> None:
        self._personal_id = personal_id
        self._pin = pin
        # Deep copy, so a renewed token never mutates the stored config entry data
        self._auth_state = copy.deepcopy(auth_state) if auth_state else {}
        self._session = session

    @property
    def auth_state(self) -> dict[str, Any]:
        return self._auth_state

    @property
    def access_token(self) -> str | None:
        token = self._auth_state.get("token") or {}
        return token.get("access_token")

    def token_needs_refresh(self) -> bool:
        """True when the token is missing or close to its expiry."""
        token = self._auth_state.get("token")
        if not token or not token.get("expiry"):
            return True
        expiry = dt_util.parse_datetime(token["expiry"])
        if not expiry:
            return True
        # Renew when 80% of the lifetime has passed
        margin = token.get("expires_in", DEFAULT_TOKEN_LIFETIME) * 0.2
        return expiry - datetime.timedelta(seconds=margin) < dt_util.utcnow()

    def ensure_login(self, refresh: bool = False) -> dict[str, Any]:
        """Make sure there is a valid token, logging in or renewing as needed."""
        self._auth_state = self._handle_login(
            copy.deepcopy(self._auth_state), refresh=refresh
        )
        return self._auth_state

    def _url(self, endpoint: str) -> str:
        return f"{AUTH_BASE_URL}/{endpoint}"

    def _get(self, url, **kwargs) -> requests.Response:
        response = self._session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
        if response.status_code not in (200, 201, 302, 303):
            _LOGGER.warning("[GET] %s Response Code: %s", url, response.status_code)
        response.raise_for_status()
        return response

    def _post(self, url, **kwargs) -> requests.Response:
        response = self._session.post(url, timeout=REQUEST_TIMEOUT, **kwargs)
        if response.status_code not in (200, 201, 302, 303):
            _LOGGER.warning("[POST] %s Response Code: %s", url, response.status_code)
        response.raise_for_status()
        return response

    def _register_app(self) -> dict[str, Any]:
        """Register a new OAuth client, which gives a client_id/client_secret."""
        response = self._post(
            self._url(OAUTH2_TOKEN_ENDPOINT),
            data={
                "client_id": APP_REGISTRATION_CLIENT_ID,
                "client_secret": APP_REGISTRATION_CLIENT_SECRET,
                "grant_type": "client_credentials",
                "scope": "dcr",
                "response_type": "token",
            },
        )
        registration_token = response.json()["access_token"]

        response = self._post(
            self._url(APP_REGISTRATION_ENDPOINT),
            json={"software_id": "dcr-ica-app-template"},
            headers={"Authorization": f"Bearer {registration_token}"},
        )
        client = response.json()
        return {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "scope": client["scope"],
        }

    def _init_oauth(self, client: dict[str, Any], code_challenge: str) -> str:
        response = self._get(
            self._url(OAUTH2_AUTHORIZE_ENDPOINT),
            params={
                "client_id": client["client_id"],
                "scope": client["scope"],
                "redirect_uri": AUTH_REDIRECT_URI,
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "login",
                "acr": AUTH_ACR,
            },
            allow_redirects=False,
        )
        location = response.headers.get("Location")
        if not location or not (match := re.search(r"&state=(\w*)", location)):
            raise IcaAuthError("Expected a Location redirect with a state")

        # Opens the login form, which sets the session cookies
        self._get(location)
        return match[1]

    def _post_login_form(self, state: str) -> str:
        try:
            response = self._post(
                self._url(LOGIN_ENDPOINT),
                data={"userName": self._personal_id, "password": self._pin},
            )
        except requests.exceptions.HTTPError as err:
            if err.response is not None and err.response.status_code in (400, 401):
                raise IcaAuthError(
                    "Login was rejected, check the personal ID number and PIN"
                ) from err
            raise

        api_state = re.search(
            r'<input type="hidden" name="state" value="(\w*)', response.text
        )
        token = re.search(
            r'<input type="hidden" name="token" value="(\w*)', response.text
        )
        if not api_state or not token:
            # The login form is shown again when the credentials are wrong
            raise IcaAuthError("Login was rejected, check the personal ID number and PIN")

        if api_state[1] != state:
            _LOGGER.warning("Login states differ between client and server")
        return token[1]

    def _get_access_token(
        self, client: dict[str, Any], state: str, token: str, code_verifier: str
    ) -> dict[str, Any]:
        response = self._post(
            self._url(OAUTH2_AUTHORIZE_ENDPOINT),
            params={
                "client_id": client["client_id"],
                "forceAuthN": "true",
                "acr": AUTH_ACR,
            },
            data={"token": token, "state": state},
            allow_redirects=False,
        )
        location = response.headers.get("Location")
        if not location or not (match := re.search(r"&code=(\w*)", location)):
            raise IcaAuthError("Expected a Location redirect with an authorization code")

        response = self._post(
            self._url(OAUTH2_TOKEN_ENDPOINT),
            data={
                "code": match[1],
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "grant_type": "authorization_code",
                "scope": client["scope"],
                "response_type": "token",
                "code_verifier": code_verifier,
                "redirect_uri": AUTH_REDIRECT_URI,
            },
        )
        return response.json()

    def _get_refresh_token(self, client: dict[str, Any], token: dict[str, Any]):
        basic_auth = base64.b64encode(
            f"{client['client_id']}:{client['client_secret']}".encode()
        ).decode("ascii")
        response = self._post(
            self._url(OAUTH2_TOKEN_ENDPOINT),
            data={"grant_type": "refresh_token", "refresh_token": token["refresh_token"]},
            headers={"Authorization": f"Basic {basic_auth}"},
        )
        return response.json()

    @staticmethod
    def _generate_code_challenge() -> tuple[str, str]:
        """Generate a PKCE code_challenge and code_verifier."""
        code_verifier = base64.urlsafe_b64encode(urandom(40)).decode("utf-8")
        code_verifier = re.sub("[^a-zA-Z0-9]+", "", code_verifier)

        code_challenge = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        code_challenge = base64.urlsafe_b64encode(code_challenge).decode("utf-8")
        return code_challenge.replace("=", ""), code_verifier

    @staticmethod
    def _set_expiry(token: dict[str, Any]) -> None:
        lifetime = token.get("expires_in", DEFAULT_TOKEN_LIFETIME)
        token["expiry"] = str(dt_util.utcnow() + datetime.timedelta(seconds=lifetime))

    def _handle_login(
        self, auth_state: dict[str, Any], refresh: bool = False, retry: int = 0
    ) -> dict[str, Any]:
        new_client = not auth_state.get("client")
        if new_client:
            auth_state["client"] = self._register_app()
            _LOGGER.debug("Registered a new ICA client")

        if new_client or not auth_state.get("token"):
            return self._handle_new_login(auth_state)

        token = auth_state["token"]
        expiry = dt_util.parse_datetime(token["expiry"]) if token.get("expiry") else None
        if not refresh and expiry and expiry > dt_util.utcnow():
            return auth_state

        try:
            return self._handle_refresh_login(auth_state)
        except requests.exceptions.HTTPError as err:
            status = err.response.status_code if err.response is not None else None
            if status not in (400, 401) or retry > 2:
                _LOGGER.warning("Could not refresh the ICA token: %s", err)
                raise
            _LOGGER.info("Token refresh got status %s, doing a new login", status)
            auth_state.pop("token", None)
            return self._handle_login(auth_state, refresh, retry=retry + 1)

    def _handle_new_login(self, auth_state: dict[str, Any]) -> dict[str, Any]:
        _LOGGER.info("Logging in to ICA")
        code_challenge, code_verifier = self._generate_code_challenge()
        state = self._init_oauth(auth_state["client"], code_challenge)
        login_token = self._post_login_form(state)
        token = self._get_access_token(
            auth_state["client"], state, login_token, code_verifier
        )
        self._set_expiry(token)
        auth_state["token"] = token

        if id_token := token.get("id_token"):
            decoded = jwt.decode(id_token, options={"verify_signature": False})
            auth_state["user"] = {
                "given_name": decoded.get("given_name"),
                "family_name": decoded.get("family_name"),
            }
        return auth_state

    def _handle_refresh_login(self, auth_state: dict[str, Any]) -> dict[str, Any]:
        refreshed = self._get_refresh_token(auth_state["client"], auth_state["token"])
        if not refreshed:
            raise IcaAuthError("Failed to refresh the ICA token")
        auth_state["token"].update(refreshed)
        self._set_expiry(auth_state["token"])
        _LOGGER.debug("Refreshed the ICA token")
        return auth_state
