"""Microsoft Entra Verified ID employee onboarding demo.

Author: Kevin Tigges
Date: 2026-09-22
Demo only; not authorized by Microsoft and not for production use.
"""

from __future__ import annotations

import base64
import hmac
import io
import secrets
from datetime import UTC, datetime
from typing import Any

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import load_config, validate_runtime_config
from app.employees import (
	claims_for_employee,
	consume_issuance_invite,
	consume_verification_invite,
	create_issuance_invite,
	create_onboarding,
	create_verification_invite,
	find_latest_by_entra_user_id,
	find_latest_issued_by_entra_user_id,
	find_by_state,
	get_onboarding,
	get_issuance_invite,
	get_verification_invite,
	issue_otp,
	personal_email_for_user,
	update_onboarding,
)
from app.services.verified_id import VerifiedIdClient, public_base_url
from app.services.graph import GraphClient


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
	"""Create and configure the Flask demo application."""
	app = Flask(__name__)
	config = load_config()
	if test_config:
		config.update(test_config)
	app.config["DEMO_CONFIG"] = config
	app.config["SECRET_KEY"] = config.get("flaskSecretKey") or secrets.token_hex(32)
	app.jinja_env.globals["organization_name"] = config["organizationName"]
	app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

	# Employee selection and existing-credential entry points.
	@app.get("/")
	def index():
		"""Render the live Entra employee picker."""
		demo_users, directory_error = _directory_demo_users(config)
		return render_template(
			"index.html",
			missing_config=validate_runtime_config(config),
			demo_employees=demo_users,
			directory_error=directory_error,
			demo_office_location=config["demoOfficeLocation"],
		)

	@app.get("/demo")
	def legacy_demo():
		"""Redirect the former demo route to the employee picker."""
		return redirect(url_for("index"))

	@app.get("/overview")
	def process_visual():
		"""Render the architecture and trust-boundary overview."""
		return render_template("process.html")

	@app.get("/process")
	def legacy_process_visual():
		"""Redirect the former process route to the overview."""
		return redirect(url_for("process_visual"))

	@app.post("/demo/select-employee")
	def select_demo_employee():
		"""Open the latest issued credential record for a selected user."""
		user_id = request.form.get("user_id", "").strip()
		if not user_id:
			abort(400)
		_require_runtime_config(config)
		try:
			user = GraphClient(config).get_user_by_upn(user_id)
			_validate_demo_user(user, config)
		except RuntimeError as exc:
			return _render_index_error(config, str(exc))
		record = find_latest_issued_by_entra_user_id(str(user["id"]))
		if not record:
			return _render_index_error(
				config,
				"No Verified ID issued by this demo was found for the selected employee.",
			)
		resumed_status = record["status"]
		if record.get("approved_at") and resumed_status in {
			"presentation_pending", "presentation_error", "verified", "helpdesk_verified", "facecheck_verified"
		}:
			resumed_status = "credential_issued"
		update_onboarding(
			record["onboarding_id"],
			status=resumed_status,
			entra_account_enabled=bool(user.get("accountEnabled")),
			presentation_state=None,
			presentation_context=None,
			otp=None,
			otp_expires_at=None,
			error=None,
			face_check_score=None,
		)
		return redirect(url_for("onboarding", onboarding_id=record["onboarding_id"]))

	@app.post("/demo/issue-employee")
	def issue_demo_employee():
		"""Start a new credential record for a selected Entra user."""
		user_id = request.form.get("user_id", "").strip()
		if not user_id:
			abort(400)
		_require_runtime_config(config)
		try:
			user = GraphClient(config).get_user_by_upn(user_id)
			_validate_demo_user(user, config)
			personal_email_for_user(user)
		except (RuntimeError, ValueError) as exc:
			app.logger.error("Entra user selection failed user_id=%s error=%s", user_id, exc)
			return _render_index_error(config, str(exc))
		record = create_onboarding(user)
		app.logger.info(
			"Onboarding linked onboarding_id=%s credential_reference=%s user_id=%s",
			record["onboarding_id"],
			record["employee_id"],
			record["entra_user_id"],
		)
		return redirect(url_for("onboarding", onboarding_id=record["onboarding_id"]))

	@app.get("/onboarding/<onboarding_id>")
	def onboarding(onboarding_id: str):
		"""Render one onboarding record and its next permitted action."""
		record = get_onboarding(onboarding_id)
		if not record:
			abort(404)
		return render_template(
			"onboarding.html",
			employee=record,
			missing_config=validate_runtime_config(config),
		)

	# Credential issuance uses a short-lived email invitation and wallet handoff.
	@app.post("/onboarding/<onboarding_id>/issue")
	def start_issuance(onboarding_id: str):
		"""Create and optionally email a single-use issuance invitation."""
		record = get_onboarding(onboarding_id)
		if not record:
			abort(404)
		if record["status"] not in {"created", "issuance_error", "issuance_invited"}:
			return redirect(url_for("onboarding", onboarding_id=onboarding_id))
		_require_runtime_config(config)
		token = create_issuance_invite(onboarding_id, lifetime_minutes=15)
		if not token:
			abort(404)
		invitation_url = (
			public_base_url(request.url_root, config.get("publicBaseUrl", ""))
			+ f"issue/{token}"
		)
		if config.get("onboardingEmailEnabled"):
			try:
				GraphClient(config).send_onboarding_email(record["personal_email"], invitation_url)
			except RuntimeError as exc:
				update_onboarding(onboarding_id, status="issuance_error", error=str(exc))
				return redirect(url_for("onboarding", onboarding_id=onboarding_id))
		update_onboarding(onboarding_id, status="issuance_invited", error=None)
		return render_template(
			"issuance_invitation_created.html",
			employee=record,
			invitation_url=invitation_url,
			email_sent=bool(config.get("onboardingEmailEnabled")),
		)

	@app.get("/issue/<token>")
	def issuance_invite(token: str):
		"""Preview a valid issuance invitation without consuming it."""
		record = get_issuance_invite(token)
		if not record:
			return render_template("issuance_invite.html", unavailable=True), 410
		return render_template("issuance_invite.html", employee=record, token=token, unavailable=False)

	@app.post("/issue/<token>/start")
	def start_invited_issuance(token: str):
		"""Validate the photo, consume the invitation, and start issuance."""
		record = get_issuance_invite(token)
		if not record:
			return render_template("issuance_invite.html", unavailable=True), 410
		try:
			photo_claim = _encoded_jpeg_claim(request.files.get("employee_photo"))
		except ValueError as exc:
			return render_template(
				"issuance_invite.html", employee=record, token=token, unavailable=False, error=str(exc)
			), 400
		record = consume_issuance_invite(token)
		if not record or record["status"] != "issuance_invited":
			return render_template("issuance_invite.html", unavailable=True), 410
		_require_runtime_config(config)
		return _create_issuance_response(record, photo_claim)

	@app.get("/issue/complete")
	def issuance_complete():
		"""Render the employee-facing issuance completion page."""
		return render_template("issuance_complete.html")

	def _create_issuance_response(record: dict[str, Any], photo_claim: str):
		"""Create the Verified ID issuance request and render its handoff."""
		onboarding_id = record["onboarding_id"]
		state = secrets.token_urlsafe(32)
		payload = {
			"authority": config["DidAuthority"],
			"registration": {"clientName": config["clientName"]},
			"callback": {
				"url": _callback_url(config),
				"state": state,
				"headers": {"api-key": config["apiKey"]},
			},
			"type": config["CredentialType"],
			"manifest": config["CredentialManifest"],
			"claims": {**claims_for_employee(record), "photo": photo_claim},
		}
		pin_length = config.get("issuancePinCodeLength", 0)
		pin = None
		if pin_length:
			pin = "".join(secrets.choice("0123456789") for _ in range(pin_length))
			payload["pin"] = {"value": pin, "length": pin_length}
		try:
			response = VerifiedIdClient(config).create_issuance_request(payload)
		except RuntimeError as exc:
			update_onboarding(onboarding_id, status="issuance_error", error=str(exc))
			return render_template("issuance_invite.html", unavailable=True, request_failed=True), 502
		update_onboarding(
			onboarding_id,
			status="issuance_pending",
			issuance_state=state,
			issuance_request_id=response.get("requestId"),
			face_check_capable=True,
			face_check_score=None,
			error=None,
		)
		return render_template(
			"request.html",
			employee=record,
			action="Issue employee credential",
			message="Scan with Microsoft Authenticator and accept the employee credential.",
			request_url=response["url"],
			qr_code=response.get("qrCode"),
			pin=pin,
			status_url=url_for("onboarding_status", onboarding_id=onboarding_id),
			return_url=url_for("issuance_complete"),
		)

	@app.post("/onboarding/<onboarding_id>/approve")
	def approve_issuance(onboarding_id: str):
		"""Record explicit operator approval after successful issuance."""
		record = get_onboarding(onboarding_id)
		if not record:
			abort(404)
		if record["status"] != "credential_issued_pending_approval":
			abort(409, description="The credential must be issued before it can be approved.")
		update_onboarding(
			onboarding_id,
			status="credential_issued" if record.get("entra_account_enabled") else "approved",
			approved_at=datetime.now(UTC).isoformat(),
			error=None,
		)
		return redirect(url_for("onboarding", onboarding_id=onboarding_id))

	@app.post("/onboarding/<onboarding_id>/enable-user")
	def enable_linked_user(onboarding_id: str):
		"""Enable the exact linked Entra account after operator approval."""
		record = get_onboarding(onboarding_id)
		if not record:
			abort(404)
		if record["status"] != "approved" or not record.get("approved_at"):
			abort(409, description="Approve the issued credential before enabling the account.")
		if record.get("entra_account_enabled"):
			update_onboarding(onboarding_id, status="credential_issued", error=None)
			return redirect(url_for("onboarding", onboarding_id=onboarding_id))
		try:
			graph = GraphClient(config)
			user = graph.get_user_by_upn(record["entra_user_id"])
			if str(user.get("officeLocation") or "").casefold() != config["demoOfficeLocation"].casefold():
				raise RuntimeError(
					f"The Entra user no longer has officeLocation set to {config['demoOfficeLocation']}."
				)
			graph.enable_user(record["entra_user_id"])
		except RuntimeError as exc:
			update_onboarding(onboarding_id, error=str(exc))
		else:
			update_onboarding(
				onboarding_id, status="credential_issued", entra_account_enabled=True, error=None
			)
		return redirect(url_for("onboarding", onboarding_id=onboarding_id))

	# Presentation routes support onboarding, help-desk, and Face Check contexts.
	@app.post("/onboarding/<onboarding_id>/verify")
	def start_presentation(onboarding_id: str):
		"""Start a presentation request in the selected verification context."""
		record = get_onboarding(onboarding_id)
		if not record:
			abort(404)
		if record["status"] not in {"credential_issued", "presentation_error", "helpdesk_verified", "facecheck_verified"}:
			return redirect(url_for("onboarding", onboarding_id=onboarding_id))
		_require_runtime_config(config)
		presentation_context = request.form.get("verification_context", "onboarding")
		if presentation_context not in {"onboarding", "helpdesk", "facecheck"}:
			abort(400, description="Unsupported verification context.")
		if presentation_context == "facecheck" and not record.get("face_check_capable"):
			abort(400, description="This credential was issued without a Face Check photo.")
		return _create_presentation_response(record, presentation_context)

	@app.post("/onboarding/<onboarding_id>/invite")
	def create_presentation_invite(onboarding_id: str):
		"""Create a single-use remote presentation invitation."""
		record = get_onboarding(onboarding_id)
		if not record:
			abort(404)
		if record["status"] not in {"credential_issued", "presentation_error", "helpdesk_verified", "facecheck_verified"}:
			return redirect(url_for("onboarding", onboarding_id=onboarding_id))
		presentation_context = request.form.get("verification_context", "onboarding")
		if presentation_context not in {"onboarding", "helpdesk"}:
			abort(400, description="Unsupported invitation context.")
		token = create_verification_invite(onboarding_id, presentation_context, lifetime_minutes=15)
		if not token:
			abort(404)
		invitation_url = (
			public_base_url(request.url_root, config.get("publicBaseUrl", ""))
			+ f"verify/{token}"
		)
		return render_template(
			"invitation_created.html",
			employee=record,
			invitation_url=invitation_url,
			presentation_context=presentation_context,
		)

	@app.get("/verify/<token>")
	def verification_invite(token: str):
		"""Preview a valid presentation invitation without consuming it."""
		record = get_verification_invite(token)
		if not record:
			return render_template("verification_invite.html", unavailable=True), 410
		return render_template(
			"verification_invite.html",
			employee=record,
			presentation_context=record["verification_invite_context"],
			token=token,
			unavailable=False,
		)

	@app.post("/verify/<token>/start")
	def start_invited_verification(token: str):
		"""Consume a presentation invitation and create the wallet request."""
		record = consume_verification_invite(token)
		if not record:
			return render_template("verification_invite.html", unavailable=True), 410
		if record["status"] not in {"credential_issued", "presentation_error", "helpdesk_verified", "facecheck_verified"}:
			return render_template("verification_invite.html", unavailable=True), 410
		_require_runtime_config(config)
		return _create_presentation_response(record, record["verification_invite_context"])

	def _create_presentation_response(record: dict[str, Any], presentation_context: str):
		"""Create a context-specific presentation request and render its handoff."""
		onboarding_id = record["onboarding_id"]
		presentation_purpose = {
			"helpdesk": "Verify that you are the employee associated with this help-desk request",
			"facecheck": "Verify your employee credential with a live Microsoft Entra Face Check",
		}.get(presentation_context, config["purpose"])
		requested_credential: dict[str, Any] = {
			"type": config["CredentialType"],
			"purpose": presentation_purpose,
			"acceptedIssuers": config["acceptedIssuersList"],
		}
		if presentation_context == "facecheck":
			requested_credential["configuration"] = {
				"validation": {
					"allowRevoked": False,
					"validateLinkedDomain": True,
					"faceCheck": {
						"sourcePhotoClaimName": "photo",
						"matchConfidenceThreshold": config["faceCheckMatchConfidenceThreshold"],
					},
				}
			}

		state = secrets.token_urlsafe(32)
		app.logger.info("Creating presentation request onboarding_id=%s", onboarding_id)
		payload = {
			"authority": config["DidAuthority"],
			"registration": {"clientName": config["clientName"]},
			"callback": {
				"url": _callback_url(config),
				"state": state,
				"headers": {"api-key": config["apiKey"]},
			},
			"includeQRCode": True,
			"includeReceipt": presentation_context != "facecheck",
			"requestedCredentials": [requested_credential],
		}
		try:
			response = VerifiedIdClient(config).create_presentation_request(payload)
		except RuntimeError as exc:
			app.logger.error("Presentation request failed onboarding_id=%s error=%s", onboarding_id, exc)
			update_onboarding(onboarding_id, status="presentation_error", error=str(exc))
			return redirect(url_for("onboarding", onboarding_id=onboarding_id))

		update_onboarding(
			onboarding_id,
			status="presentation_pending",
			presentation_state=state,
			presentation_request_id=response.get("requestId"),
			presentation_context=presentation_context,
			last_callback_status=None,
			last_callback_at=None,
			last_callback_request_id=None,
			last_callback_error_code=None,
			last_callback_error_message=None,
			error=None,
		)
		app.logger.info(
			"Presentation request ready onboarding_id=%s request_id=%s",
			onboarding_id,
			response.get("requestId", "not returned"),
		)
		return render_template(
			"request.html",
			employee=record,
			action="Verify employee identity",
			message=(
				"Scan and share the employee credential to verify the caller for this help-desk request."
				if presentation_context == "helpdesk"
				else "Scan, complete the live Face Check, and share the employee credential."
				if presentation_context == "facecheck"
				else "Scan and share the employee credential to unlock the temporary password."
			),
			request_url=response["url"],
			qr_code=response.get("qrCode"),
			pin=None,
			status_url=url_for("onboarding_status", onboarding_id=onboarding_id),
			return_url=url_for("onboarding", onboarding_id=onboarding_id),
		)

	# Callback and polling endpoints correlate asynchronous wallet operations.
	@app.post("/api/verifiedid/callback")
	def verified_id_callback():
		"""Validate and apply a correlated Verified ID callback event."""
		supplied_key = request.headers.get("api-key", "")
		if not hmac.compare_digest(supplied_key, str(config["apiKey"])):
			app.logger.warning("Rejected Verified ID callback with invalid API key")
			abort(401)

		body = request.get_json(silent=True) or {}
		state = body.get("state", "")
		record = find_by_state(state) if state else None
		if not record:
			app.logger.warning("Rejected Verified ID callback with unknown state")
			abort(404)

		request_status = body.get("requestStatus", "")
		callback_error = body.get("error") if isinstance(body.get("error"), dict) else {}
		callback_request_id = body.get("requestId")
		update_onboarding(
			record["onboarding_id"],
			last_callback_status=request_status or "missing",
			last_callback_at=datetime.now(UTC).isoformat(),
			last_callback_request_id=callback_request_id,
			last_callback_error_code=callback_error.get("code"),
			last_callback_error_message=callback_error.get("message"),
		)
		app.logger.info(
			"Verified ID callback onboarding_id=%s request_id=%s status=%s error_code=%s",
			record["onboarding_id"],
			callback_request_id or "not returned",
			request_status or "missing",
			callback_error.get("code") or "none",
		)
		if request_status == "issuance_successful" and state == record.get("issuance_state"):
			update_onboarding(
				record["onboarding_id"],
				status="credential_issued_pending_approval",
				issuance_succeeded_at=datetime.now(UTC).isoformat(),
				error=None,
			)
			app.logger.info("Credential issued onboarding_id=%s", record["onboarding_id"])
		elif request_status == "presentation_verified" and state == record.get("presentation_state"):
			if record["status"] != "presentation_pending":
				return jsonify({"acknowledged": True})
			claims = _presentation_claims(body)
			if _claims_match_employee(claims, record):
				app.logger.info("Presented credential matched onboarding_id=%s", record["onboarding_id"])
				if record.get("presentation_context") == "facecheck":
					score = _presentation_face_check_score(body)
					threshold = config["faceCheckMatchConfidenceThreshold"]
					if score is None or score < threshold:
						update_onboarding(
							record["onboarding_id"],
							status="presentation_error",
							face_check_score=None,
							error="Microsoft Entra Face Check did not return a passing confidence score.",
						)
					else:
						update_onboarding(
							record["onboarding_id"],
							status="facecheck_verified",
							face_check_score=round(score, 2),
							otp=None,
							otp_expires_at=None,
							access_code_kind="Microsoft Entra Face Check verification",
							error=None,
						)
				elif record.get("presentation_context") == "helpdesk":
					update_onboarding(
						record["onboarding_id"],
						status="helpdesk_verified",
						otp=None,
						otp_expires_at=None,
						access_code_kind="Help-desk caller verification",
						error=None,
					)
					app.logger.info("Help-desk caller verified onboarding_id=%s", record["onboarding_id"])
				elif config.get("accessMode") == "graphTap":
					try:
						if not record.get("entra_user_id"):
							raise RuntimeError("This onboarding run has no provisioned Entra user.")
						tap = GraphClient(config).create_temporary_access_pass(record["entra_user_id"])
						update_onboarding(
							record["onboarding_id"],
							otp=tap["temporaryAccessPass"],
							otp_expires_at=tap.get("lifetimeInMinutes", config["tapLifetimeMinutes"]),
							access_code_kind="Temporary Access Pass",
							status="verified",
							error=None,
						)
						app.logger.info(
							"Temporary Access Pass created onboarding_id=%s lifetime_minutes=%s",
							record["onboarding_id"],
							tap.get("lifetimeInMinutes", config["tapLifetimeMinutes"]),
						)
					except RuntimeError as exc:
						app.logger.error("TAP creation failed onboarding_id=%s error=%s", record["onboarding_id"], exc)
						update_onboarding(
							record["onboarding_id"],
							status="presentation_error",
							error=f"Credential verified, but Temporary Access Pass creation failed: {exc}",
						)
				else:
					issue_otp(record["onboarding_id"])
			else:
				app.logger.warning("Presented credential did not match onboarding_id=%s", record["onboarding_id"])
				update_onboarding(
					record["onboarding_id"],
					status="presentation_error",
					error="The presented credential does not match this employee onboarding record.",
				)
		elif request_status.endswith("_error") or body.get("error"):
			phase = "issuance" if state == record.get("issuance_state") else "presentation"
			app.logger.error(
				"Verified ID callback error onboarding_id=%s phase=%s status=%s",
				record["onboarding_id"],
				phase,
				request_status,
			)
			update_onboarding(
				record["onboarding_id"],
				status=f"{phase}_error",
				error=body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else str(body.get("error") or request_status),
			)
		return jsonify({"acknowledged": True})

	@app.get("/api/onboarding/<onboarding_id>/status")
	def onboarding_status(onboarding_id: str):
		"""Return safe polling fields for one active browser workflow."""
		record = get_onboarding(onboarding_id)
		if not record:
			abort(404)
		return jsonify({
			"status": record["status"],
			"error": record.get("error"),
			"requestId": record.get("presentation_request_id") or record.get("issuance_request_id"),
			"lastCallbackStatus": record.get("last_callback_status"),
			"lastCallbackAt": record.get("last_callback_at"),
			"callbackErrorCode": record.get("last_callback_error_code"),
			"callbackErrorMessage": record.get("last_callback_error_message"),
		})

	def _callback_url(runtime_config: dict[str, Any]) -> str:
		"""Build the public callback URL supplied to the Request Service."""
		base = public_base_url(request.url_root, runtime_config.get("publicBaseUrl", ""))
		return f"{base}api/verifiedid/callback"

	return app


def _directory_demo_users(config: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
	"""List in-scope Entra users and attach retained credential status."""
	if validate_runtime_config(config):
		return [], None
	try:
		users = GraphClient(config).list_users_by_office_location(config["demoOfficeLocation"])
	except RuntimeError as exc:
		return [], str(exc)
	result = []
	for user in users:
		if str(user.get("officeLocation") or "").casefold() != config["demoOfficeLocation"].casefold():
			continue
		user_id = str(user.get("id") or "")
		if not user_id:
			continue
		record = find_latest_by_entra_user_id(user_id)
		issued_record = find_latest_issued_by_entra_user_id(user_id)
		entry = dict(user)
		entry["onboarding_id"] = record["onboarding_id"] if record else None
		entry["credential_status"] = record["status"] if record else None
		entry["credential_issued"] = issued_record is not None
		entry["credential_reference"] = record.get("employee_id") if record else None
		result.append(entry)
	return result, None


def _validate_demo_user(user: dict[str, Any], config: dict[str, Any]) -> None:
	"""Enforce the directory attributes required by this demo workflow."""
	if str(user.get("officeLocation") or "").casefold() != config["demoOfficeLocation"].casefold():
		raise RuntimeError(
			f"The Entra user must have officeLocation set to {config['demoOfficeLocation']}."
		)
	missing_profile_fields = [
		field
		for field in ("givenName", "surname", "jobTitle", "department")
		if not str(user.get(field) or "").strip()
	]
	if missing_profile_fields:
		raise RuntimeError(
			"The Entra user is missing required employment fields: "
			+ ", ".join(missing_profile_fields)
		)


def _render_index_error(config: dict[str, Any], error: str):
	"""Render the employee picker with a workflow error."""
	demo_users, directory_error = _directory_demo_users(config)
	return render_template(
		"index.html",
		missing_config=validate_runtime_config(config),
		demo_employees=demo_users,
		directory_error=directory_error,
		demo_office_location=config["demoOfficeLocation"],
		error=error,
	), 409


def _require_runtime_config(config: dict[str, Any]) -> None:
	"""Reject live operations until all required settings are configured."""
	missing = validate_runtime_config(config)
	if missing:
		abort(503, description=f"Missing Verified ID configuration: {', '.join(missing)}")


def _presentation_claims(body: dict[str, Any]) -> dict[str, Any]:
	"""Extract the first verified credential's claims from a callback."""
	credentials = body.get("verifiedCredentialsData") or []
	if not credentials or not isinstance(credentials[0], dict):
		return {}
	claims = credentials[0].get("claims") or {}
	return claims if isinstance(claims, dict) else {}


def _presentation_face_check_score(body: dict[str, Any]) -> float | None:
	"""Extract a numeric Face Check confidence score when present."""
	credentials = body.get("verifiedCredentialsData") or []
	if not credentials or not isinstance(credentials[0], dict):
		return None
	face_check = credentials[0].get("faceCheck") or {}
	score = face_check.get("matchConfidenceScore") if isinstance(face_check, dict) else None
	if isinstance(score, bool) or not isinstance(score, (int, float)):
		return None
	return float(score)


def _encoded_jpeg_claim(photo: Any) -> str:
	"""Normalize a phone JPEG and return its raw Base64 issuance value."""
	if photo is None or not getattr(photo, "filename", ""):
		raise ValueError("Choose an HR-approved JPEG employee photo before creating the issuance QR.")
	photo_bytes = photo.stream.read(10_000_001)
	if len(photo_bytes) > 10_000_000:
		raise ValueError("The uploaded employee photo must be no larger than 10 MB.")
	try:
		with Image.open(io.BytesIO(photo_bytes)) as image:
			if image.format != "JPEG":
				raise ValueError("The employee photo must be a JPEG file.")
			image.load()
			image = ImageOps.exif_transpose(image)
			width, height = image.size
			if width < 200 or height < 200:
				raise ValueError("The employee photo must be at least 200 by 200 pixels.")
			image = image.convert("RGB")
			image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
			encoded_photo = b""
			for quality in (85, 75, 65, 55, 45):
				output = io.BytesIO()
				image.save(output, format="JPEG", quality=quality, optimize=True)
				encoded_photo = output.getvalue()
				if len(encoded_photo) <= 1_000_000:
					break
	except (UnidentifiedImageError, OSError):
		raise ValueError("The employee photo is not a valid JPEG file.") from None
	if len(encoded_photo) > 1_000_000:
		raise ValueError("The employee photo could not be compressed below 1 MB.")
	return base64.b64encode(encoded_photo).decode("ascii")


def _claims_match_employee(claims: dict[str, Any], employee: dict[str, Any]) -> bool:
	"""Constant-time match credential identity claims to the private record."""
	employee_id = claims.get("employeeId") or claims.get("employee_id")
	email = claims.get("email")
	return (
		isinstance(employee_id, str)
		and isinstance(email, str)
		and hmac.compare_digest(employee_id, employee["employee_id"])
		and hmac.compare_digest(email.lower(), employee["email"].lower())
	)
