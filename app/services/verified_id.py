"""Microsoft Entra Verified ID Request Service helpers."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any
from urllib.parse import urlparse

import msal
import requests

log = logging.getLogger("verifiedid")


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT payload without validating the signature."""
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("Invalid JWT format")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
    return json.loads(raw.decode("utf-8"))


class VerifiedIdClient:
    """Thin client around MSAL + Verified ID Request Service APIs."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._cca = msal.ConfidentialClientApplication(
            config["azClientId"],
            authority=f"https://login.microsoftonline.com/{config['azTenantId']}",
            client_credential=config["azClientSecret"],
        )

    def get_access_token(self) -> str:
        result = self._cca.acquire_token_for_client(scopes=[self.config["vcServiceScope"]])
        if "access_token" not in result:
            error = result.get("error")
            description = result.get("error_description")
            raise RuntimeError(f"Failed to acquire Verified ID access token: {error} - {description}")

        token = result["access_token"]
        claims = decode_jwt_payload(token)
        roles = claims.get("roles") or []
        if "VerifiableCredential.Create.All" not in roles:
            # Granular roles may also work depending on tenant configuration.
            allowed = {
                "VerifiableCredential.Create.All",
                "VerifiableCredential.Issue.All",
                "VerifiableCredential.Present.All",
            }
            if not any(role in allowed for role in roles):
                raise RuntimeError(
                    "Access token is missing Verified ID app roles. "
                    "Grant admin consent for Verifiable Credentials Service Request permissions."
                )
        return token

    def _endpoint(self, action: str) -> str:
        host = self.config.get("msIdentityHostName") or "https://verifiedid.did.msidentity.com/v1.0/"
        if not host.endswith("/"):
            host += "/"
        return f"{host}verifiableCredentials/{action}"

    def create_issuance_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = self.get_access_token()
        url = self._endpoint("createIssuanceRequest")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        log.info("POST %s", url)
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        data = _safe_json(response)
        log.debug("Issuance API response status=%s request_id=%s", response.status_code, data.get("requestId"))
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Issuance API error ({response.status_code}): {json.dumps(data)}")
        return data

    def create_presentation_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = self.get_access_token()
        url = self._endpoint("createPresentationRequest")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        log.info("POST %s", url)
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        data = _safe_json(response)
        log.debug("Presentation API response status=%s request_id=%s", response.status_code, data.get("requestId"))
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Presentation API error ({response.status_code}): {json.dumps(data)}")
        return data

    def fetch_manifest(self) -> dict[str, Any]:
        manifest_url = self.config["CredentialManifest"]
        response = requests.get(manifest_url, timeout=60)
        response.raise_for_status()
        body = response.json()
        if "token" in body:
            return decode_jwt_payload(body["token"])
        return body


def public_base_url(request_url_root: str, configured_public_base_url: str = "") -> str:
    """
    Build the externally reachable HTTPS base URL used in Verified ID callbacks.

    Localhost alone is not enough: Microsoft's Request Service must POST callbacks
    to a public HTTPS endpoint. Use ngrok/dev tunnels and set publicBaseUrl, or
    rely on reverse-proxy headers.
    """
    if configured_public_base_url:
        return configured_public_base_url.rstrip("/") + "/"

    root = request_url_root
    # Common reverse-proxy / tunnel headers are handled by the Flask request object
    # when ProxyFix is enabled. As a final local fallback, force https scheme.
    if root.startswith("http://"):
        parsed = urlparse(root)
        host = parsed.netloc.lower()
        if host not in {"localhost", "127.0.0.1"} and not host.startswith("localhost:"):
            root = "https://" + root[len("http://") :]
    if not root.endswith("/"):
        root += "/"
    return root


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text}
