"""Load configuration for the Verified ID onboarding demo.

Author: Kevin Tigges
Date: 2026-09-22
Demo only; not authorized by Microsoft and not for production use.
"""

from __future__ import annotations

import json
import os
import secrets
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.json"
EXAMPLE_CONFIG_PATH = ROOT_DIR / "config.example.json"
CALLBACK_KEY_PATH = ROOT_DIR / ".callback_api_key"

# Request Service API application ID used in the OAuth scope.
VC_SERVICE_SCOPE = "3db474b9-6a0c-4840-96ac-1fceb342124f/.default"
DEFAULT_MS_IDENTITY_HOST = "https://verifiedid.did.msidentity.com/v1.0/"


def _env(name: str, default: str | None = None) -> str | None:
    """Read one environment value with an optional default."""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _load_or_create_callback_key() -> str:
    """Load or create the local secret used to authenticate callbacks."""
    configured = os.getenv("CALLBACK_API_KEY")
    if configured:
        return configured
    try:
        existing = CALLBACK_KEY_PATH.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass

    value = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(CALLBACK_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return CALLBACK_KEY_PATH.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    return value


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load config from JSON file and/or environment variables."""
    load_dotenv(ROOT_DIR / ".env")

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    file_cfg: dict[str, Any] = {}

    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            file_cfg = json.load(handle)
    elif EXAMPLE_CONFIG_PATH.exists() and not config_path:
        # Allow first-run boot with example values for static pages only.
        with EXAMPLE_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            file_cfg = json.load(handle)

    cfg = deepcopy(file_cfg)

    # Environment variables override file values (useful for demos / CI).
    overrides = {
        "entraTenantId": _env("entraTenantId") or _env("ENTRA_TENANT_ID"),
        "entraClientId": _env("entraClientId") or _env("ENTRA_CLIENT_ID"),
        "entraClientSecret": _env("entraClientSecret") or _env("ENTRA_CLIENT_SECRET"),
        "DidAuthority": _env("DidAuthority") or _env("DID_AUTHORITY"),
        "CredentialManifest": _env("CredentialManifest") or _env("CREDENTIAL_MANIFEST"),
        "CredentialType": _env("CredentialType") or _env("CREDENTIAL_TYPE"),
        "acceptedIssuers": _env("acceptedIssuers") or _env("ACCEPTED_ISSUERS"),
        "organizationName": _env("organizationName") or _env("ORGANIZATION_NAME"),
        "clientName": _env("clientName") or _env("CLIENT_NAME"),
        "purpose": _env("purpose") or _env("PURPOSE"),
        "issuancePinCodeLength": _env("issuancePinCodeLength") or _env("ISSUANCE_PIN_CODE_LENGTH"),
        "port": _env("PORT") or _env("port"),
        "debug": _env("DEBUG") or _env("debug"),
        "publicBaseUrl": _env("publicBaseUrl") or _env("PUBLIC_BASE_URL"),
        "msIdentityHostName": _env("msIdentityHostName") or _env("MS_IDENTITY_HOST_NAME"),
        "accessMode": _env("accessMode") or _env("ACCESS_MODE"),
        "tapLifetimeMinutes": _env("tapLifetimeMinutes") or _env("TAP_LIFETIME_MINUTES"),
        "faceCheckMatchConfidenceThreshold": _env("faceCheckMatchConfidenceThreshold") or _env("FACE_CHECK_MATCH_CONFIDENCE_THRESHOLD"),
        "demoOfficeLocation": _env("demoOfficeLocation") or _env("DEMO_OFFICE_LOCATION"),
        "onboardingEmailEnabled": _env("onboardingEmailEnabled") or _env("ONBOARDING_EMAIL_ENABLED"),
        "onboardingSenderUpn": _env("onboardingSenderUpn") or _env("ONBOARDING_SENDER_UPN"),
    }
    for key, value in overrides.items():
        if value is not None and value != "":
            cfg[key] = value

    # Defaults for demo-friendly local development.
    cfg.setdefault("organizationName", "Example Organization")
    cfg.setdefault("clientName", "Employee Verified ID Demo")
    cfg.setdefault("purpose", "Present your employee credential to continue")
    cfg.setdefault("CredentialType", "VerifiedEmployeeCard")
    cfg.setdefault("issuancePinCodeLength", 4)
    cfg.setdefault("port", 8080)
    cfg.setdefault("debug", True)
    cfg.setdefault("publicBaseUrl", "")
    cfg.setdefault("msIdentityHostName", DEFAULT_MS_IDENTITY_HOST)
    cfg.setdefault("accessMode", "graphTap")
    cfg.setdefault("tapLifetimeMinutes", 60)
    cfg.setdefault("faceCheckMatchConfidenceThreshold", 70)
    cfg.setdefault("demoOfficeLocation", "VIDDEMO")
    cfg.setdefault("onboardingEmailEnabled", False)
    cfg.setdefault("onboardingSenderUpn", "")
    cfg.setdefault("acceptedIssuers", cfg.get("DidAuthority", ""))

    # Runtime-only values.
    cfg["apiKey"] = _load_or_create_callback_key()
    cfg["vcServiceScope"] = VC_SERVICE_SCOPE

    # Normalize types.
    try:
        cfg["port"] = int(cfg.get("port", 8080))
    except (TypeError, ValueError):
        cfg["port"] = 8080

    try:
        cfg["issuancePinCodeLength"] = int(cfg.get("issuancePinCodeLength", 4))
    except (TypeError, ValueError):
        cfg["issuancePinCodeLength"] = 4

    try:
        cfg["tapLifetimeMinutes"] = int(cfg.get("tapLifetimeMinutes", 60))
    except (TypeError, ValueError):
        cfg["tapLifetimeMinutes"] = 60

    try:
        cfg["faceCheckMatchConfidenceThreshold"] = int(cfg.get("faceCheckMatchConfidenceThreshold", 70))
    except (TypeError, ValueError):
        cfg["faceCheckMatchConfidenceThreshold"] = 70
    if not 50 <= cfg["faceCheckMatchConfidenceThreshold"] <= 100:
        cfg["faceCheckMatchConfidenceThreshold"] = 70

    if isinstance(cfg.get("debug"), str):
        cfg["debug"] = cfg["debug"].strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(cfg.get("onboardingEmailEnabled"), str):
        cfg["onboardingEmailEnabled"] = cfg["onboardingEmailEnabled"].strip().lower() in {"1", "true", "yes", "on"}
    # acceptedIssuers can be a single DID or a semicolon-separated list.
    accepted = cfg.get("acceptedIssuers") or cfg.get("DidAuthority") or ""
    if isinstance(accepted, str):
        cfg["acceptedIssuersList"] = [part.strip() for part in accepted.split(";") if part.strip()]
    elif isinstance(accepted, list):
        cfg["acceptedIssuersList"] = accepted
    else:
        cfg["acceptedIssuersList"] = []

    return cfg


def validate_runtime_config(cfg: dict[str, Any]) -> list[str]:
    """Return a list of missing required settings for issuance/presentation."""
    required = [
        "entraTenantId",
        "entraClientId",
        "entraClientSecret",
        "DidAuthority",
        "CredentialManifest",
        "CredentialType",
    ]
    missing = []
    for key in required:
        value = cfg.get(key)
        if value is None or str(value).strip() == "" or str(value).startswith("YOUR-"):
            missing.append(key)
    accepted_issuers = cfg.get("acceptedIssuersList") or []
    if not accepted_issuers or any("YOUR-" in str(issuer) for issuer in accepted_issuers):
        missing.append("acceptedIssuers")
    if cfg.get("onboardingEmailEnabled") and not str(cfg.get("onboardingSenderUpn") or "").strip():
        missing.append("onboardingSenderUpn")
    return missing
