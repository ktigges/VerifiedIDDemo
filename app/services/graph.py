"""Microsoft Graph helper for dynamic Temporary Access Pass creation."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import msal
import requests


class GraphClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._cca = msal.ConfidentialClientApplication(
            config["azClientId"],
            authority=f"https://login.microsoftonline.com/{config['azTenantId']}",
            client_credential=config["azClientSecret"],
        )

    def _access_token(self) -> str:
        result = self._cca.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise RuntimeError(f"Graph token acquisition failed: {result.get('error_description') or result.get('error')}")
        return result["access_token"]

    def get_user_by_upn(self, user_principal_name: str) -> dict[str, Any]:
        """Resolve one pre-created Entra user by exact UPN."""
        token = self._access_token()
        encoded_upn = quote(user_principal_name.strip(), safe="")
        response = requests.get(
            f"https://graph.microsoft.com/v1.0/users/{encoded_upn}",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "$select": "id,userPrincipalName,givenName,surname,displayName,jobTitle,department,accountEnabled"
            },
            timeout=30,
        )
        data = _safe_json(response)
        if response.status_code == 404:
            raise RuntimeError("No pre-created Entra user was found for that exact UPN.")
        if response.status_code != 200:
            raise RuntimeError(f"User lookup failed ({response.status_code}): {json.dumps(data)}")
        if not data.get("id") or not data.get("userPrincipalName"):
            raise RuntimeError("Microsoft Graph returned an incomplete user record.")
        return data

    def create_temporary_access_pass(self, user_id: str) -> dict[str, Any]:
        """Create a one-time TAP for the linked Entra user."""
        token = self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        requested_lifetime = self.config["tapLifetimeMinutes"]
        tap_response = requests.post(
            f"https://graph.microsoft.com/v1.0/users/{user_id}/authentication/temporaryAccessPassMethods",
            headers=headers,
            json={
                "lifetimeInMinutes": requested_lifetime,
                "isUsableOnce": True,
            },
            timeout=30,
        )
        tap_data = _safe_json(tap_response)
        allowed_lifetime = _allowed_tap_lifetime(tap_response.status_code, tap_data)
        if allowed_lifetime is not None and allowed_lifetime != requested_lifetime:
            tap_response = requests.post(
                f"https://graph.microsoft.com/v1.0/users/{user_id}/authentication/temporaryAccessPassMethods",
                headers=headers,
                json={
                    "lifetimeInMinutes": allowed_lifetime,
                    "isUsableOnce": True,
                },
                timeout=30,
            )
            tap_data = _safe_json(tap_response)
        if tap_response.status_code != 201:
            raise RuntimeError(f"TAP creation failed ({tap_response.status_code}): {json.dumps(tap_data)}")
        tap_data.setdefault("lifetimeInMinutes", allowed_lifetime or requested_lifetime)
        return tap_data

def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text}


def _allowed_tap_lifetime(status_code: int, data: dict[str, Any]) -> int | None:
    if status_code != 400:
        return None
    error_text = json.dumps(data)
    match = re.search(r"valid range between\s+(\d+)\s+and\s+(\d+)", error_text, re.IGNORECASE)
    if not match:
        return None
    minimum, maximum = (int(value) for value in match.groups())
    return minimum if minimum == maximum else max(minimum, min(60, maximum))