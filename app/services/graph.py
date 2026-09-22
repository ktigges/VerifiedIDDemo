"""Call Microsoft Graph for users, mail, enablement, and TAP creation.

Author: Kevin Tigges
Date: 2026-09-22
Demo only; not authorized by Microsoft and not for production use.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import msal
import requests


class GraphClient:
    """Use application credentials for the demo's Graph operations."""

    def __init__(self, config: dict[str, Any]):
        """Initialize the confidential client from validated configuration."""
        self.config = config
        self._cca = msal.ConfidentialClientApplication(
            config["entraClientId"],
            authority=f"https://login.microsoftonline.com/{config['entraTenantId']}",
            client_credential=config["entraClientSecret"],
        )

    def _access_token(self) -> str:
        """Acquire an app-only Microsoft Graph access token."""
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
                "$select": "id,userPrincipalName,otherMails,givenName,surname,displayName,jobTitle,department,officeLocation,accountEnabled"
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

    def list_users_by_office_location(self, office_location: str) -> list[dict[str, Any]]:
        """List real Entra users tagged with an exact office location."""
        token = self._access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "ConsistencyLevel": "eventual",
        }
        escaped_location = office_location.strip().replace("'", "''")
        url = "https://graph.microsoft.com/v1.0/users"
        params: dict[str, str] | None = {
            "$filter": f"officeLocation eq '{escaped_location}'",
            "$count": "true",
            "$select": "id,userPrincipalName,otherMails,givenName,surname,displayName,jobTitle,department,officeLocation,accountEnabled",
        }
        users: list[dict[str, Any]] = []
        while url:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            data = _safe_json(response)
            if response.status_code != 200:
                raise RuntimeError(f"VIDDEMO user lookup failed ({response.status_code}): {json.dumps(data)}")
            page = data.get("value")
            if not isinstance(page, list):
                raise RuntimeError("Microsoft Graph returned an invalid user list.")
            users.extend(user for user in page if isinstance(user, dict))
            next_link = data.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) else ""
            params = None
        return sorted(
            users,
            key=lambda user: str(user.get("displayName") or user.get("userPrincipalName") or "").casefold(),
        )

    def enable_user(self, user_id: str) -> None:
        """Enable a pre-created Entra user after credential issuance."""
        token = self._access_token()
        encoded_user_id = quote(user_id.strip(), safe="")
        response = requests.patch(
            f"https://graph.microsoft.com/v1.0/users/{encoded_user_id}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"accountEnabled": True},
            timeout=30,
        )
        if response.status_code != 204:
            raise RuntimeError(f"User enable failed ({response.status_code}): {json.dumps(_safe_json(response))}")

    def send_onboarding_email(self, recipient: str, invitation_url: str) -> None:
        """Send a personal-email onboarding invitation from the configured mailbox."""
        sender = str(self.config.get("onboardingSenderUpn") or "").strip()
        if not sender:
            raise RuntimeError("onboardingSenderUpn is required when onboarding email is enabled.")
        token = self._access_token()
        encoded_sender = quote(sender, safe="")
        response = requests.post(
            f"https://graph.microsoft.com/v1.0/users/{encoded_sender}/sendMail",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "message": {
                    "subject": "Complete your employee credential onboarding",
                    "body": {
                        "contentType": "HTML",
                        "content": (
                            "<p>Complete your employee credential onboarding using this "
                            f"single-use link. It expires in 15 minutes:</p><p><a href=\"{invitation_url}\">"
                            "Start credential onboarding</a></p><p>If you did not expect this message, ignore it.</p>"
                        ),
                    },
                    "toRecipients": [{"emailAddress": {"address": recipient}}],
                },
                "saveToSentItems": True,
            },
            timeout=30,
        )
        if response.status_code != 202:
            raise RuntimeError(f"Onboarding email failed ({response.status_code}): {json.dumps(_safe_json(response))}")

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
    """Return a JSON response or preserve the text body for diagnostics."""
    try:
        return response.json()
    except Exception:
        return {"raw": response.text}


def _allowed_tap_lifetime(status_code: int, data: dict[str, Any]) -> int | None:
    """Extract a supported TAP lifetime from a Graph validation error."""
    if status_code != 400:
        return None
    error_text = json.dumps(data)
    match = re.search(r"valid range between\s+(\d+)\s+and\s+(\d+)", error_text, re.IGNORECASE)
    if not match:
        return None
    minimum, maximum = (int(value) for value in match.groups())
    return minimum if minimum == maximum else max(minimum, min(60, maximum))