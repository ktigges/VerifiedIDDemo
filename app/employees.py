"""In-memory employee onboarding records for the demo."""

from __future__ import annotations

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


def _load_records() -> dict[str, dict[str, Any]]:
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
    temporary_path = _state_path.with_suffix(f".tmp-{os.getpid()}")
    temporary_path.write_text(json.dumps(_records), encoding="utf-8")
    temporary_path.chmod(0o600)
    os.replace(temporary_path, _state_path)


_records = _load_records()


def create_onboarding(user: dict[str, Any]) -> dict[str, Any]:
    """Create an onboarding transaction linked to a pre-created Entra user."""
    onboarding_id = str(uuid.uuid4())
    credential_reference = f"EMP-{uuid.uuid4().hex[:8].upper()}"
    record: dict[str, Any] = {
        "onboarding_id": onboarding_id,
        "demo_employee": True,
        "employee_id": credential_reference,
        "credential_reference": credential_reference,
        "given_name": str(user["givenName"]).strip(),
        "family_name": str(user["surname"]).strip(),
        "email": str(user["userPrincipalName"]).strip().lower(),
        "job_title": str(user["jobTitle"]).strip(),
        "department": str(user["department"]).strip(),
        "status": "created",
        "entra_user_id": str(user["id"]),
        "entra_account_enabled": bool(user.get("accountEnabled")),
        "issuance_state": None,
        "presentation_state": None,
        "presentation_context": None,
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


def get_onboarding(onboarding_id: str) -> dict[str, Any] | None:
    with _lock:
        record = _records.get(onboarding_id)
        return deepcopy(record) if record else None


def list_demo_employees() -> list[dict[str, Any]]:
    """Return only demo-created employees known to have received a credential."""
    credential_statuses = {
        "credential_issued",
        "presentation_pending",
        "presentation_error",
        "verified",
        "helpdesk_verified",
        "facecheck_verified",
    }
    with _lock:
        employees = [
            deepcopy(record)
            for record in _records.values()
            if record.get("demo_employee") is True
            and record.get("status") in credential_statuses
            and record.get("entra_user_id")
        ]
    return sorted(employees, key=lambda employee: employee.get("created_at", ""), reverse=True)


def find_by_state(state: str) -> dict[str, Any] | None:
    with _lock:
        for record in _records.values():
            if state in {record.get("issuance_state"), record.get("presentation_state")}:
                return deepcopy(record)
    return None


def update_onboarding(onboarding_id: str, **changes: Any) -> dict[str, Any] | None:
    with _lock:
        record = _records.get(onboarding_id)
        if not record:
            return None
        record.update(changes)
        _save_records()
        return deepcopy(record)


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
