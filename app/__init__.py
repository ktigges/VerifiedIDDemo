"""Microsoft Entra Verified ID employee onboarding demo."""

from __future__ import annotations

import base64
import hmac
import io
import secrets
from typing import Any
from urllib.parse import quote

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from PIL import Image, UnidentifiedImageError
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import load_config, validate_runtime_config
from app.employees import (
	claims_for_employee,
	create_onboarding,
	find_by_state,
	get_onboarding,
	issue_otp,
	list_demo_employees,
	update_onboarding,
)
from app.services.verified_id import VerifiedIdClient, public_base_url
from app.services.graph import GraphClient


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
	app = Flask(__name__)
	config = load_config()
	if test_config:
		config.update(test_config)
	app.config["DEMO_CONFIG"] = config
	app.config["SECRET_KEY"] = config.get("flaskSecretKey") or secrets.token_hex(32)
	app.jinja_env.globals["organization_name"] = config["organizationName"]
	app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

	@app.get("/")
	def index():
		return render_template(
			"index.html",
			missing_config=validate_runtime_config(config),
			demo_employees=list_demo_employees(),
		)

	@app.get("/demo")
	def legacy_demo():
		return redirect(url_for("index"))

	@app.get("/overview")
	def process_visual():
		return render_template("process.html")

	@app.get("/process")
	def legacy_process_visual():
		return redirect(url_for("process_visual"))

	@app.post("/demo/select-employee")
	def select_demo_employee():
		onboarding_id = request.form.get("onboarding_id", "")
		record = get_onboarding(onboarding_id)
		available_ids = {employee["onboarding_id"] for employee in list_demo_employees()}
		if not record or onboarding_id not in available_ids:
			abort(404)
		update_onboarding(
			onboarding_id,
			status="credential_issued",
			presentation_state=None,
			presentation_context=None,
			otp=None,
			otp_expires_at=None,
			error=None,
			face_check_score=None,
		)
		return redirect(url_for("onboarding", onboarding_id=onboarding_id))

	@app.post("/onboarding")
	def new_onboarding():
		user_principal_name = request.form.get("user_principal_name", "").strip().lower()
		values = {"user_principal_name": user_principal_name}
		if not user_principal_name or "@" not in user_principal_name:
			return render_template(
				"index.html",
				missing_config=validate_runtime_config(config),
				demo_employees=list_demo_employees(),
				error="Enter the exact UPN of a pre-created Entra user.",
				values=values,
			), 400
		_require_runtime_config(config)
		try:
			user = GraphClient(config).get_user_by_upn(user_principal_name)
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
		except RuntimeError as exc:
			app.logger.error("Entra user lookup failed upn=%s error=%s", user_principal_name, exc)
			return render_template(
				"index.html",
				missing_config=validate_runtime_config(config),
				demo_employees=list_demo_employees(),
				error=str(exc),
				values=values,
			), 400
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
		record = get_onboarding(onboarding_id)
		if not record:
			abort(404)
		return render_template(
			"onboarding.html",
			employee=record,
			missing_config=validate_runtime_config(config),
		)

	@app.post("/onboarding/<onboarding_id>/issue")
	def start_issuance(onboarding_id: str):
		record = get_onboarding(onboarding_id)
		if not record:
			abort(404)
		if record["status"] not in {"created", "issuance_error"}:
			return redirect(url_for("onboarding", onboarding_id=onboarding_id))
		_require_runtime_config(config)
		try:
			photo_claim = _encoded_jpeg_claim(request.files.get("employee_photo"))
		except ValueError as exc:
			update_onboarding(onboarding_id, status="issuance_error", error=str(exc), face_check_capable=False)
			return redirect(url_for("onboarding", onboarding_id=onboarding_id))

		state = secrets.token_urlsafe(32)
		callback_url = _callback_url(config)
		app.logger.info(
			"Creating issuance request onboarding_id=%s employee_id=%s callback_url=%s",
			onboarding_id,
			record["employee_id"],
			callback_url,
		)
		payload = {
			"authority": config["DidAuthority"],
			"registration": {"clientName": config["clientName"]},
			"callback": {
				"url": callback_url,
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
			app.logger.error("Issuance request failed onboarding_id=%s error=%s", onboarding_id, exc)
			update_onboarding(onboarding_id, status="issuance_error", error=str(exc))
			return redirect(url_for("onboarding", onboarding_id=onboarding_id))

		update_onboarding(
			onboarding_id,
			status="issuance_pending",
			issuance_state=state,
			face_check_capable=True,
			face_check_score=None,
			error=None,
		)
		app.logger.info(
			"Issuance request ready onboarding_id=%s request_id=%s",
			onboarding_id,
			response.get("requestId", "not returned"),
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
			return_url=url_for("onboarding", onboarding_id=onboarding_id),
		)

	@app.post("/onboarding/<onboarding_id>/verify")
	def start_presentation(onboarding_id: str):
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
			presentation_context=presentation_context,
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

	@app.post("/api/verifiedid/callback")
	def verified_id_callback():
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
		app.logger.info(
			"Verified ID callback onboarding_id=%s status=%s",
			record["onboarding_id"],
			request_status or "missing",
		)
		if request_status == "issuance_successful" and state == record.get("issuance_state"):
			update_onboarding(record["onboarding_id"], status="credential_issued", error=None)
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
		record = get_onboarding(onboarding_id)
		if not record:
			abort(404)
		return jsonify({"status": record["status"], "error": record.get("error")})

	def _callback_url(runtime_config: dict[str, Any]) -> str:
		base = public_base_url(request.url_root, runtime_config.get("publicBaseUrl", ""))
		return f"{base}api/verifiedid/callback"

	return app


def _require_runtime_config(config: dict[str, Any]) -> None:
	missing = validate_runtime_config(config)
	if missing:
		abort(503, description=f"Missing Verified ID configuration: {', '.join(missing)}")


def _presentation_claims(body: dict[str, Any]) -> dict[str, Any]:
	credentials = body.get("verifiedCredentialsData") or []
	if not credentials or not isinstance(credentials[0], dict):
		return {}
	claims = credentials[0].get("claims") or {}
	return claims if isinstance(claims, dict) else {}


def _presentation_face_check_score(body: dict[str, Any]) -> float | None:
	credentials = body.get("verifiedCredentialsData") or []
	if not credentials or not isinstance(credentials[0], dict):
		return None
	face_check = credentials[0].get("faceCheck") or {}
	score = face_check.get("matchConfidenceScore") if isinstance(face_check, dict) else None
	if isinstance(score, bool) or not isinstance(score, (int, float)):
		return None
	return float(score)


def _encoded_jpeg_claim(photo: Any) -> str:
	if photo is None or not getattr(photo, "filename", ""):
		raise ValueError("Choose an HR-approved JPEG employee photo before creating the issuance QR.")
	photo_bytes = photo.stream.read(1_000_001)
	if len(photo_bytes) > 1_000_000:
		raise ValueError("The employee photo must be no larger than 1 MB.")
	try:
		with Image.open(io.BytesIO(photo_bytes)) as image:
			width, height = image.size
			if image.format != "JPEG":
				raise ValueError("The employee photo must be a JPEG file.")
			image.verify()
	except (UnidentifiedImageError, OSError):
		raise ValueError("The employee photo is not a valid JPEG file.") from None
	if width < 200 or height < 200:
		raise ValueError("The employee photo must be at least 200 by 200 pixels.")
	return quote(base64.b64encode(photo_bytes).decode("ascii"), safe="")


def _claims_match_employee(claims: dict[str, Any], employee: dict[str, Any]) -> bool:
	employee_id = claims.get("employeeId") or claims.get("employee_id")
	email = claims.get("email")
	return (
		isinstance(employee_id, str)
		and isinstance(email, str)
		and hmac.compare_digest(employee_id, employee["employee_id"])
		and hmac.compare_digest(email.lower(), employee["email"].lower())
	)
