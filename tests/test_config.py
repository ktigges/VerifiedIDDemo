"""Verify Entra configuration names without contacting Microsoft services."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.config import load_config, validate_runtime_config
from app.services.graph import GraphClient
from app.services.verified_id import VerifiedIdClient


class EntraConfigurationTests(unittest.TestCase):
    """Protect the configuration contract used by both Microsoft clients."""

    def setUp(self):
        # Use complete but inert settings so validation and client setup stay local.
        self.config = {
            "entraTenantId": "tenant-id",
            "entraClientId": "client-id",
            "entraClientSecret": "client-secret",
            "DidAuthority": "did:web:issuer.example",
            "CredentialManifest": "https://issuer.example/manifest",
            "CredentialType": "VerifiedEmployeeCard",
            "acceptedIssuers": "did:web:issuer.example",
        }

    # Environment values must override the JSON file using the Entra naming scheme.
    def test_loads_entra_environment_names(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(self.config), encoding="utf-8")
            environment = {
                "ENTRA_TENANT_ID": "environment-tenant",
                "ENTRA_CLIENT_ID": "environment-client",
                "ENTRA_CLIENT_SECRET": "environment-secret",
                "CALLBACK_API_KEY": "callback-secret",
            }
            with patch.dict(os.environ, environment, clear=True):
                loaded = load_config(config_path)

        self.assertEqual(loaded["entraTenantId"], "environment-tenant")
        self.assertEqual(loaded["entraClientId"], "environment-client")
        self.assertEqual(loaded["entraClientSecret"], "environment-secret")
        self.assertEqual(validate_runtime_config(loaded), [])

    # Patch MSAL construction to inspect client credentials without requesting tokens.
    @patch("app.services.graph.msal.ConfidentialClientApplication")
    def test_graph_client_uses_entra_configuration(self, confidential_client):
        GraphClient(self.config)

        confidential_client.assert_called_once_with(
            "client-id",
            authority="https://login.microsoftonline.com/tenant-id",
            client_credential="client-secret",
        )

    @patch("app.services.verified_id.msal.ConfidentialClientApplication")
    def test_verified_id_client_uses_entra_configuration(self, confidential_client):
        VerifiedIdClient(self.config)

        confidential_client.assert_called_once_with(
            "client-id",
            authority="https://login.microsoftonline.com/tenant-id",
            client_credential="client-secret",
        )


if __name__ == "__main__":
    unittest.main()
