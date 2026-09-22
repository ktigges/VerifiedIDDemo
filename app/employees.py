"""Persist employee onboarding records for the local demo.

Author: Kevin Tigges
Date: 2026-09-22
Demo only; not authorized by Microsoft and not for production use.
"""

from __future__ import annotations

import hashlib
import secrets
import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


_state_path = Path(__file__).resolve().parent.parent / ".demo_onboarding_state.json"
_lock = threading.Lock()


# Local persistence keeps demo state across Flask restarts.
def _load_records() -> dict[str, dict[str, Any]]:
    """Load retained records, returning an empty catalog on failure."""
    if not _state_path.exists():
        return {}
    try:
        data = json.loads(_state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        for record in data.values():
            if isinstance(record, dict):
                record.setdefault("demo_employee", True)
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def _save_records() -> None:
    """Atomically save records with owner-only file permissions."""
    temporary_path = _state_path.with_suffix(f".tmp-{os.getpid()}")
    temporary_path.write_text(json.dumps(_records), encoding="utf-8")
    temporary_path.chmod(0o600)
    os.replace(temporary_path, _state_path)


_records = _load_records()


# Onboarding records link wallet operations to one existing Entra user.
def create_onboarding(user: dict[str, Any]) -> dict[str, Any]:
    """Create an onboarding transaction linked to a pre-created Entra user."""
    onboarding_id = str(uuid.uuid4())
    credential_reference = f"EMP-{uuid.uuid4().hex[:8].upper()}"
    personal_email = personal_email_for_user(user)
    record: dict[str, Any] = {
        "onboarding_id": onboarding_id,
        "demo_employee": True,
        "employee_id": credential_reference,
        "credential_reference": credential_reference,
        "given_name": str(user["givenName"]).strip(),
        "family_name": str(user["surname"]).strip(),
        "email": str(user["userPrincipalName"]).strip().lower(),
        "personal_email": personal_email,
        "job_title": str(user["jobTitle"]).strip(),
        "department": str(user["department"]).strip(),
        "status": "created",
        "entra_user_id": str(user["id"]),
        "entra_account_enabled": bool(user.get("accountEnabled")),
        "issuance_state": None,
        "issuance_request_id": None,
        "issuance_succeeded_at": None,
        "issuance_invite_hash": None,
        "issuance_invite_expires_at": None,
        "issuance_invite_used": False,
        "approved_at": None,
        "presentation_state": None,
        "presentation_request_id": None,
        "presentation_context": None,
        "last_callback_status": None,
        "last_callback_at": None,
        "last_callback_request_id": None,
        "last_callback_error_code": None,
        "last_callback_error_message": None,
        "verification_invite_hash": None,
        "verification_invite_context": None,
        "verification_invite_expires_at": None,
        "verification_invite_used": False,
        "face_check_capable": False,
        "face_check_score": None,
        "otp": None,
        "otp_expires_at": None,
        "access_code_kind": "Simulated demo OTP",
        "created_at": datetime.now(UTC).isoformat(),
        "error": None,
    }
    with _lock:
        _records[onboarding_id] = record
        _save_records()
    return deepcopy(record)


def personal_email_for_user(user: dict[str, Any]) -> str:
    """Select the first populated delivery address from Entra otherMails."""
    other_mails = user.get("otherMails")
    if isinstance(other_mails, list):
        for value in other_mails:
            address = str(value or "").strip()
            if address:
                return address
    raise ValueError("The Entra user must have an email address in otherMails.")


def get_onboarding(onboarding_id: str) -> dict[str, Any] | None:
    """Return a defensive copy of one onboarding record."""
    with _lock:
        record = _records.get(onboarding_id)
        return deepcopy(record) if record else None


def find_latest_by_entra_user_id(user_id: str) -> dict[str, Any] | None:
    """Return the newest retained onboarding record for one Entra object."""
    with _lock:
        matches = [
            record
            for record in _records.values()
            if record.get("entra_user_id") == user_id
        ]
        if not matches:
            return None
        latest = max(matches, key=lambda record: record.get("created_at", ""))
        return deepcopy(latest)


def find_latest_issued_by_entra_user_id(user_id: str) -> dict[str, Any] | None:
    """Return the newest record with a confirmed successful issuance callback."""
    issued_statuses = {
        "credential_issued_pending_approval",
        "approved",
        "credential_issued",
        "presentation_pending",
        "presentation_error",
        "verified",
        "helpdesk_verified",
        "facecheck_verified",
    }
    with _lock:
        matches = [
            record
            for record in _records.values()
            if record.get("entra_user_id") == user_id
            and (
                record.get("issuance_succeeded_at")
                or record.get("approved_at")
                or record.get("status") in issued_statuses
            )
        ]
        if not matches:
            return None
        latest = max(matches, key=lambda record: record.get("created_at", ""))
        return deepcopy(latest)


def find_by_state(state: str) -> dict[str, Any] | None:
    """Find the record correlated to an issuance or presentation callback."""
    with _lock:
        for record in _records.values():
            if state in {record.get("issuance_state"), record.get("presentation_state")}:
                return deepcopy(record)
    return None


def update_onboarding(onboarding_id: str, **changes: Any) -> dict[str, Any] | None:
    """Apply and persist state changes for one onboarding record."""
    with _lock:
        record = _records.get(onboarding_id)
        if not record:
            return None
        record.update(changes)
        _save_records()
        return deepcopy(record)


# Issuance invitations expose short-lived tokens while retaining only hashes.
def create_issuance_invite(onboarding_id: str, lifetime_minutes: int = 15) -> str | None:
    """Create a single-use issuance invitation while storing only its token hash."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _lock:
        record = _records.get(onboarding_id)
        if not record:
            return None
        record.update(
            issuance_invite_hash=token_hash,
            issuance_invite_expires_at=(
                datetime.now(UTC) + timedelta(minutes=lifetime_minutes)
            ).isoformat(),
            issuance_invite_used=False,
        )
        _save_records()
    return token


def get_issuance_invite(token: str) -> dict[str, Any] | None:
    """Return an active issuance invitation without consuming it."""
    with _lock:
        return _find_issuance_invite(token, consume=False)


def consume_issuance_invite(token: str) -> dict[str, Any] | None:
    """Atomically consume an active issuance invitation."""
    with _lock:
        return _find_issuance_invite(token, consume=True)


def _find_issuance_invite(token: str, consume: bool) -> dict[str, Any] | None:
    """Resolve an issuance token by hash and optionally mark it used."""
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)
    for record in _records.values():
        stored_hash = record.get("issuance_invite_hash")
        if not isinstance(stored_hash, str) or not secrets.compare_digest(stored_hash, token_hash):
            continue
        try:
            expires_at = datetime.fromisoformat(record["issuance_invite_expires_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if record.get("issuance_invite_used") or expires_at <= now:
            return None
        if consume:
            record["issuance_invite_used"] = True
            _save_records()
        return deepcopy(record)
    return None


# Verification invitations use the same preview-then-consume lifecycle.
def create_verification_invite(
    onboarding_id: str,
    context: str,
    lifetime_minutes: int = 15,
) -> str | None:
    """Create a single-use invitation while storing only its token hash."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _lock:
        record = _records.get(onboarding_id)
        if not record:
            return None
        record.update(
            verification_invite_hash=token_hash,
            verification_invite_context=context,
            verification_invite_expires_at=(
                datetime.now(UTC) + timedelta(minutes=lifetime_minutes)
            ).isoformat(),
            verification_invite_used=False,
        )
        _save_records()
    return token


def get_verification_invite(token: str) -> dict[str, Any] | None:
    """Return an active invitation without consuming it."""
    with _lock:
        return _find_verification_invite(token, consume=False)


def consume_verification_invite(token: str) -> dict[str, Any] | None:
    """Atomically consume an active invitation."""
    with _lock:
        return _find_verification_invite(token, consume=True)


def _find_verification_invite(token: str, consume: bool) -> dict[str, Any] | None:
    """Resolve a verification token by hash and optionally mark it used."""
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)
    for record in _records.values():
        stored_hash = record.get("verification_invite_hash")
        if not isinstance(stored_hash, str) or not secrets.compare_digest(stored_hash, token_hash):
            continue
        try:
            expires_at = datetime.fromisoformat(record["verification_invite_expires_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if record.get("verification_invite_used") or expires_at <= now:
            return None
        if consume:
            record["verification_invite_used"] = True
            _save_records()
        return deepcopy(record)
    return None


# Credential outcomes are generated only after a correlated presentation.
def issue_otp(onboarding_id: str, lifetime_minutes: int = 10) -> dict[str, Any] | None:
    """Create an OTP only after the caller has validated the presented credential."""
    with _lock:
        record = _records.get(onboarding_id)
        if not record or record["status"] != "presentation_pending":
            return None
        record.update(
            otp=f"{secrets.randbelow(1_000_000):06d}",
            otp_expires_at=(datetime.now(UTC) + timedelta(minutes=lifetime_minutes)).isoformat(),
            status="verified",
            error=None,
        )
        _save_records()
        return deepcopy(record)


def claims_for_employee(employee: dict[str, Any]) -> dict[str, str]:
    """Map the employee record to the credential contract's issuance claims."""
    return {
        "given_name": employee["given_name"],
        "family_name": employee["family_name"],
        "email": employee["email"],
        "job_title": employee["job_title"],
        "department": employee["department"],
        "employee_id": employee.get("credential_reference", employee["employee_id"]),
    }
