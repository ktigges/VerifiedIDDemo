"""Verify the Flask onboarding workflows with mocked Microsoft services."""

from __future__ import annotations

import base64
import unittest
import re
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

import app.employees as employee_store
from app import create_app
from app.employees import get_onboarding, update_onboarding


class OnboardingFlowTests(unittest.TestCase):
    """Run browser-level issuance and presentation flows against isolated state."""

    def setUp(self):
        # Redirect persistent workflow state so tests never read or alter the demo file.
        temporary_directory = TemporaryDirectory()
        original_state_path = employee_store._state_path
        original_records = employee_store._records
        employee_store._state_path = Path(temporary_directory.name) / "demo-state.json"
        employee_store._records = {}

        def restore_employee_store():
            employee_store._state_path = original_state_path
            employee_store._records = original_records
            temporary_directory.cleanup()

        self.addCleanup(restore_employee_store)
        # These inert values satisfy runtime validation without authenticating anywhere.
        self.app = create_app(
            {
                "TESTING": True,
                "entraTenantId": "tenant-id",
                "entraClientId": "client-id",
                "entraClientSecret": "client-secret",
                "DidAuthority": "did:web:issuer.example",
                "CredentialManifest": "https://verifiedid.did.msidentity.com/v1.0/tenant-id/verifiableCredentials/contracts/VerifiedEmployeeCard",
                "CredentialType": "VerifiedEmployeeCard",
                "acceptedIssuersList": ["did:web:issuer.example"],
                "organizationName": "Example Organization",
                "apiKey": "callback-secret",
                "publicBaseUrl": "https://demo.example",
                "accessMode": "demoOtp",
                "faceCheckMatchConfidenceThreshold": 70,
                "demoOfficeLocation": "VIDDEMO",
                "onboardingEmailEnabled": True,
                "onboardingSenderUpn": "admin@example.com",
            }
        )
        self.client = self.app.test_client()
        # Replace Graph at the Flask boundary; tests control every directory response.
        self.graph_patcher = patch("app.GraphClient")
        self.graph_client_class = self.graph_patcher.start()
        self.addCleanup(self.graph_patcher.stop)
        self.graph_client = self.graph_client_class.return_value

        def get_user_by_upn(identifier):
            upn = identifier.removeprefix("object-")
            return {
                "id": f"object-{upn}",
                "userPrincipalName": upn,
                "otherMails": ["new.hire.personal@example.net"],
                "givenName": "New",
                "surname": "Hire",
                "jobTitle": "Engineer",
                "department": "Technology",
                "officeLocation": "VIDDEMO",
                "accountEnabled": False,
            }

        self.graph_client.get_user_by_upn.side_effect = get_user_by_upn
        self.graph_client.list_users_by_office_location.return_value = [
            get_user_by_upn("new.hire@example.com")
        ]

    # Common helpers drive the same HTTP routes used by the browser.
    def _create_employee(self, upn: str = "new.hire@example.com") -> str:
        response = self.client.post(
            "/demo/issue-employee",
            data={"user_id": f"object-{upn}"},
        )
        self.assertEqual(response.status_code, 302)
        return response.headers["Location"].rsplit("/", 1)[-1]

    def _send_issuance_invite(self, onboarding_id: str) -> str:
        self.graph_client.send_onboarding_email.reset_mock()
        response = self.client.post(f"/onboarding/{onboarding_id}/issue")
        self.assertEqual(response.status_code, 200)
        recipient, invitation_url = self.graph_client.send_onboarding_email.call_args.args
        self.assertEqual(recipient, "new.hire.personal@example.net")
        return invitation_url.rsplit("/", 1)[-1]

    @staticmethod
    def _employee_photo():
        photo = BytesIO()
        Image.new("RGB", (240, 240), "#486b7a").save(photo, format="JPEG")
        photo.seek(0)
        return photo, "employee.jpg"

    # Entry pages, employee scope, and account lifecycle behavior.
    def test_customer_explainer_precedes_demo(self):
        demo = self.client.get("/")
        explainer = self.client.get("/overview")
        legacy_demo = self.client.get("/demo")
        legacy_process = self.client.get("/process")

        self.assertEqual(demo.status_code, 200)
        self.assertIn(b"Issue a Verified ID", demo.data)
        self.assertIn(b"help-desk identity verification", demo.data)
        self.assertEqual(explainer.status_code, 200)
        self.assertIn(b"Knowing personal information is not proof of identity", explainer.data)
        self.assertIn(b"How decentralized identity works", explainer.data)
        self.assertIn(b"Help-desk caller verification", explainer.data)
        self.assertIn(b"Customer-operated issuer and verifier", explainer.data)
        self.assertIn(b"Two different face controls", explainer.data)
        self.assertEqual(explainer.data.count(b"data-page-button="), 6)
        self.assertIn(b'href="/"', explainer.data)
        self.assertEqual(legacy_demo.status_code, 302)
        self.assertEqual(legacy_demo.headers["Location"], "/")
        self.assertEqual(legacy_process.status_code, 302)
        self.assertEqual(legacy_process.headers["Location"], "/overview")

    def test_picker_shows_only_real_viddemo_users_and_resumes_credential(self):
        onboarding_id = self._create_employee()
        excluded_id = self._create_employee("stale.local@example.com")
        update_onboarding(
            onboarding_id,
            status="helpdesk_verified",
            approved_at="2026-01-01T00:00:00+00:00",
            presentation_state="old-presentation",
            presentation_context="helpdesk",
            otp="old-code",
        )
        update_onboarding(excluded_id, status="credential_issued", demo_employee=False)

        picker = self.client.get("/")
        self.assertIn(b"new.hire@example.com", picker.data)
        self.assertIn(b"Verified ID issued", picker.data)
        self.assertIn(b"Issue Verified ID", picker.data)
        self.assertNotIn(b"Existing user principal name", picker.data)
        self.assertNotIn(b"stale.local@example.com", picker.data)
        self.assertNotIn(excluded_id.encode(), picker.data)

        response = self.client.post(
            "/demo/select-employee",
            data={"user_id": "object-new.hire@example.com"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/onboarding/{onboarding_id}"))
        selected = get_onboarding(onboarding_id)
        self.assertEqual(selected["status"], "credential_issued")
        self.assertIsNone(selected["presentation_state"])
        self.assertIsNone(selected["otp"])
        choices = self.client.get(response.headers["Location"])
        self.assertIn(b"Continue onboarding", choices.data)
        self.assertIn(b"Verify help-desk caller", choices.data)

    def test_open_employee_rejects_user_without_locally_issued_credential(self):
        response = self.client.post(
            "/demo/select-employee",
            data={"user_id": "object-new.hire@example.com"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn(b"No Verified ID issued by this demo", response.data)
        self.assertIsNone(employee_store.find_latest_by_entra_user_id("object-new.hire@example.com"))

    def test_issue_accepts_first_other_mail_without_classifying_it(self):
        self.graph_client.get_user_by_upn.side_effect = lambda identifier: {
            "id": identifier,
            "userPrincipalName": "new.hire@example.com",
            "otherMails": ["new.hire@example.com"],
            "givenName": "New",
            "surname": "Hire",
            "jobTitle": "Engineer",
            "department": "Technology",
            "officeLocation": "VIDDEMO",
            "accountEnabled": False,
        }

        response = self.client.post(
            "/demo/issue-employee",
            data={"user_id": "object-new.hire@example.com"},
        )

        self.assertEqual(response.status_code, 302)
        onboarding_id = response.headers["Location"].rsplit("/", 1)[-1]
        self.assertEqual(get_onboarding(onboarding_id)["personal_email"], "new.hire@example.com")

    def test_newer_incomplete_attempt_does_not_hide_issued_credential(self):
        issued_id = self._create_employee()
        update_onboarding(
            issued_id,
            status="credential_issued",
            issuance_succeeded_at="2026-09-10T12:00:00+00:00",
        )
        incomplete_id = self._create_employee()

        picker = self.client.get("/")
        opened = self.client.post(
            "/demo/select-employee",
            data={"user_id": "object-new.hire@example.com"},
        )

        self.assertIn(b"Verified ID issued", picker.data)
        self.assertEqual(opened.status_code, 302)
        self.assertTrue(opened.headers["Location"].endswith(f"/onboarding/{issued_id}"))
        self.assertNotEqual(issued_id, incomplete_id)

    def test_account_can_be_enabled_only_after_operator_approval(self):
        onboarding_id = self._create_employee()

        too_early = self.client.post(f"/onboarding/{onboarding_id}/enable-user")
        self.assertEqual(too_early.status_code, 409)
        self.graph_client.enable_user.assert_not_called()

        update_onboarding(onboarding_id, status="credential_issued_pending_approval")
        still_too_early = self.client.post(f"/onboarding/{onboarding_id}/enable-user")
        self.assertEqual(still_too_early.status_code, 409)

        approved = self.client.post(f"/onboarding/{onboarding_id}/approve")
        self.assertEqual(approved.status_code, 302)
        self.assertIsNotNone(get_onboarding(onboarding_id)["approved_at"])

        enabled = self.client.post(f"/onboarding/{onboarding_id}/enable-user")

        self.assertEqual(enabled.status_code, 302)
        self.graph_client.enable_user.assert_called_once_with("object-new.hire@example.com")
        self.assertTrue(get_onboarding(onboarding_id)["entra_account_enabled"])
        self.assertEqual(get_onboarding(onboarding_id)["status"], "credential_issued")

    def test_already_enabled_account_advances_after_approval(self):
        onboarding_id = self._create_employee()
        update_onboarding(
            onboarding_id,
            status="credential_issued_pending_approval",
            entra_account_enabled=True,
        )

        approved = self.client.post(f"/onboarding/{onboarding_id}/approve")

        self.assertEqual(approved.status_code, 302)
        self.graph_client.enable_user.assert_not_called()
        self.assertEqual(get_onboarding(onboarding_id)["status"], "credential_issued")

    def test_repeated_linking_generates_distinct_credential_references(self):
        first_id = self._create_employee()
        second_id = self._create_employee()

        self.assertNotEqual(first_id, second_id)
        self.assertNotEqual(
            get_onboarding(first_id)["employee_id"],
            get_onboarding(second_id)["employee_id"],
        )
        self.assertEqual(get_onboarding(first_id)["email"], get_onboarding(second_id)["email"])
        self.assertTrue(get_onboarding(first_id)["email"].endswith("@example.com"))
        self.assertEqual(
            get_onboarding(first_id)["entra_user_id"],
            get_onboarding(second_id)["entra_user_id"],
        )
        self.graph_client.create_user.assert_not_called()

    # Issuance callbacks and claim matching gate every access-code result.
    @patch("app.VerifiedIdClient")
    def test_access_code_requires_matching_verified_credential(self, verified_id_client):
        client = verified_id_client.return_value
        client.create_issuance_request.return_value = {"url": "openid-initiate-issuance://request"}
        client.create_presentation_request.return_value = {"url": "openid-vc://request"}
        onboarding_id = self._create_employee()
        employee = get_onboarding(onboarding_id)

        token = self._send_issuance_invite(onboarding_id)
        self.assertNotIn(token, employee_store._state_path.read_text(encoding="utf-8"))
        preview = self.client.get(f"/issue/{token}")
        self.assertEqual(preview.status_code, 200)
        issuance_response = self.client.post(
            f"/issue/{token}/start",
            data={"employee_photo": self._employee_photo()},
            content_type="multipart/form-data",
        )
        self.assertEqual(issuance_response.status_code, 200)
        self.assertEqual(self.client.get(f"/issue/{token}").status_code, 410)
        issuance_payload = client.create_issuance_request.call_args.args[0]
        self.assertIn("manifest", issuance_payload)
        self.assertNotIn("manifestUrl", issuance_payload)
        self.assertEqual(issuance_payload["claims"]["employee_id"], employee["employee_id"])
        self.assertTrue(issuance_payload["claims"]["photo"].startswith("/9j/"))
        self.assertTrue(get_onboarding(onboarding_id)["face_check_capable"])

        employee = get_onboarding(onboarding_id)
        self.client.post(
            "/api/verifiedid/callback",
            headers={"api-key": "callback-secret"},
            json={"state": employee["issuance_state"], "requestStatus": "issuance_successful"},
        )
        self.assertEqual(get_onboarding(onboarding_id)["status"], "credential_issued_pending_approval")
        blocked = self.client.post(f"/onboarding/{onboarding_id}/enable-user")
        self.assertEqual(blocked.status_code, 409)
        self.client.post(f"/onboarding/{onboarding_id}/approve")
        self.client.post(f"/onboarding/{onboarding_id}/enable-user")
        self.assertEqual(get_onboarding(onboarding_id)["status"], "credential_issued")

        presentation_response = self.client.post(f"/onboarding/{onboarding_id}/verify")
        self.assertEqual(presentation_response.status_code, 200)
        employee = get_onboarding(onboarding_id)
        mismatch_response = self.client.post(
            "/api/verifiedid/callback",
            headers={"api-key": "callback-secret"},
            json={
                "state": employee["presentation_state"],
                "requestStatus": "presentation_verified",
                "verifiedCredentialsData": [
                    {"claims": {"employeeId": "EMP-WRONG", "email": employee["email"]}}
                ],
            },
        )
        self.assertEqual(mismatch_response.status_code, 200)
        self.assertEqual(get_onboarding(onboarding_id)["status"], "presentation_error")
        self.assertIsNone(get_onboarding(onboarding_id)["otp"])

        self.client.post(f"/onboarding/{onboarding_id}/verify")
        employee = get_onboarding(onboarding_id)
        verified_callback = {
            "state": employee["presentation_state"],
            "requestStatus": "presentation_verified",
            "verifiedCredentialsData": [
                {"claims": {"employeeId": employee["employee_id"], "email": employee["email"]}}
            ],
        }
        verified_response = self.client.post(
            "/api/verifiedid/callback",
            headers={"api-key": "callback-secret"},
            json=verified_callback,
        )
        self.assertEqual(verified_response.status_code, 200)
        verified = get_onboarding(onboarding_id)
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(len(verified["otp"]), 6)

        original_otp = verified["otp"]
        self.client.post(
            "/api/verifiedid/callback",
            headers={"api-key": "callback-secret"},
            json=verified_callback,
        )
        self.assertEqual(get_onboarding(onboarding_id)["otp"], original_otp)

    # Photo tests keep image validation and normalization inside the issuance boundary.
    @patch("app.VerifiedIdClient")
    def test_issuance_rejects_invalid_employee_photo(self, verified_id_client):
        onboarding_id = self._create_employee()
        token = self._send_issuance_invite(onboarding_id)

        response = self.client.post(
            f"/issue/{token}/start",
            data={"employee_photo": (BytesIO(b"not-a-jpeg"), "employee.jpg")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 400)
        employee = get_onboarding(onboarding_id)
        self.assertEqual(employee["status"], "issuance_invited")
        self.assertIn(b"valid JPEG", response.data)
        self.assertFalse(employee["face_check_capable"])
        verified_id_client.return_value.create_issuance_request.assert_not_called()

        retry = self.client.get(f"/issue/{token}")
        self.assertEqual(retry.status_code, 200)

    @patch("app.VerifiedIdClient")
    def test_issuance_compresses_large_employee_photo(self, verified_id_client):
        verified_id_client.return_value.create_issuance_request.return_value = {
            "url": "openid-initiate-issuance://request"
        }
        onboarding_id = self._create_employee()
        token = self._send_issuance_invite(onboarding_id)
        photo = BytesIO()
        Image.effect_noise((3000, 3000), 100).convert("RGB").save(photo, format="JPEG", quality=95)
        self.assertGreater(photo.tell(), 1_000_000)
        photo.seek(0)

        response = self.client.post(
            f"/issue/{token}/start",
            data={"employee_photo": (photo, "phone-photo.jpg")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        payload = verified_id_client.return_value.create_issuance_request.call_args.args[0]
        encoded_photo = base64.b64decode(payload["claims"]["photo"])
        self.assertLessEqual(len(encoded_photo), 1_000_000)
        with Image.open(BytesIO(encoded_photo)) as compressed:
            self.assertEqual(compressed.format, "JPEG")
            self.assertGreaterEqual(min(compressed.size), 200)

    # Presentation contexts intentionally produce different downstream actions.
    def test_verified_credential_creates_real_tap_for_provisioned_user(self):
        self.app.config["DEMO_CONFIG"]["accessMode"] = "graphTap"
        self.graph_client.create_temporary_access_pass.return_value = {
            "temporaryAccessPass": "REAL-TAP-CODE",
            "lifetimeInMinutes": 60,
        }
        onboarding_id = self._create_employee()
        employee = get_onboarding(onboarding_id)
        presentation_state = f"presentation-{onboarding_id}"
        update_onboarding(
            onboarding_id,
            status="presentation_pending",
            presentation_state=presentation_state,
        )

        response = self.client.post(
            "/api/verifiedid/callback",
            headers={"api-key": "callback-secret"},
            json={
                "state": presentation_state,
                "requestStatus": "presentation_verified",
                "verifiedCredentialsData": [
                    {"claims": {"employeeId": employee["employee_id"], "email": employee["email"]}}
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.graph_client.create_temporary_access_pass.assert_called_once_with(employee["entra_user_id"])
        verified = get_onboarding(onboarding_id)
        self.assertEqual(verified["otp"], "REAL-TAP-CODE")
        self.assertEqual(verified["access_code_kind"], "Temporary Access Pass")

    @patch("app.VerifiedIdClient")
    def test_helpdesk_verification_validates_caller_without_creating_tap(self, verified_id_client):
        verified_id_client.return_value.create_presentation_request.return_value = {
            "url": "openid-vc://helpdesk-request"
        }
        onboarding_id = self._create_employee()
        employee = get_onboarding(onboarding_id)
        update_onboarding(onboarding_id, status="credential_issued")

        choice_page = self.client.get(f"/onboarding/{onboarding_id}")
        self.assertIn(b"Continue onboarding", choice_page.data)
        self.assertIn(b"Verify help-desk caller", choice_page.data)

        response = self.client.post(
            f"/onboarding/{onboarding_id}/verify",
            data={"verification_context": "helpdesk"},
        )

        self.assertEqual(response.status_code, 200)
        payload = verified_id_client.return_value.create_presentation_request.call_args.args[0]
        self.assertIn("help-desk request", payload["requestedCredentials"][0]["purpose"])
        pending = get_onboarding(onboarding_id)
        self.client.post(
            "/api/verifiedid/callback",
            headers={"api-key": "callback-secret"},
            json={
                "state": pending["presentation_state"],
                "requestStatus": "presentation_verified",
                "verifiedCredentialsData": [
                    {"claims": {"employeeId": employee["employee_id"], "email": employee["email"]}}
                ],
            },
        )

        verified = get_onboarding(onboarding_id)
        self.assertEqual(verified["status"], "helpdesk_verified")
        self.assertIsNone(verified["otp"])
        self.graph_client.create_temporary_access_pass.assert_not_called()

        follow_up = self.client.post(f"/onboarding/{onboarding_id}/verify")
        self.assertEqual(follow_up.status_code, 200)
        follow_up_payload = verified_id_client.return_value.create_presentation_request.call_args.args[0]
        self.assertEqual(follow_up_payload["requestedCredentials"][0]["purpose"], self.app.config["DEMO_CONFIG"]["purpose"])

    @patch("app.VerifiedIdClient")
    def test_one_time_helpdesk_link_is_not_consumed_by_get(self, verified_id_client):
        verified_id_client.return_value.create_presentation_request.return_value = {
            "url": "openid-vc://helpdesk-request"
        }
        onboarding_id = self._create_employee()
        update_onboarding(onboarding_id, status="credential_issued")

        created = self.client.post(
            f"/onboarding/{onboarding_id}/invite",
            data={"verification_context": "helpdesk"},
        )
        token_match = re.search(rb"https://demo\.example/verify/([A-Za-z0-9_-]+)", created.data)

        self.assertEqual(created.status_code, 200)
        self.assertIsNotNone(token_match)
        token = token_match.group(1).decode()
        self.assertNotIn(token, employee_store._state_path.read_text(encoding="utf-8"))
        preview = self.client.get(f"/verify/{token}")
        self.assertEqual(preview.status_code, 200)
        verified_id_client.return_value.create_presentation_request.assert_not_called()

        started = self.client.post(f"/verify/{token}/start")
        reused = self.client.post(f"/verify/{token}/start")

        self.assertEqual(started.status_code, 200)
        self.assertEqual(reused.status_code, 410)
        verified_id_client.return_value.create_presentation_request.assert_called_once()
        payload = verified_id_client.return_value.create_presentation_request.call_args.args[0]
        self.assertIn("help-desk request", payload["requestedCredentials"][0]["purpose"])

    @patch("app.VerifiedIdClient")
    def test_face_check_presentation_requires_passing_score_without_creating_tap(self, verified_id_client):
        verified_id_client.return_value.create_presentation_request.return_value = {
            "url": "openid-vc://face-check-request",
            "requestId": "face-check-request-id",
        }
        onboarding_id = self._create_employee()
        employee = get_onboarding(onboarding_id)
        update_onboarding(onboarding_id, status="credential_issued", face_check_capable=True)
        choice_page = self.client.get(f"/onboarding/{onboarding_id}")
        self.assertIn(b"Start Face Check", choice_page.data)

        response = self.client.post(
            f"/onboarding/{onboarding_id}/verify",
            data={"verification_context": "facecheck"},
        )

        self.assertEqual(response.status_code, 200)
        payload = verified_id_client.return_value.create_presentation_request.call_args.args[0]
        self.assertFalse(payload["includeReceipt"])
        validation = payload["requestedCredentials"][0]["configuration"]["validation"]
        self.assertEqual(validation["faceCheck"]["sourcePhotoClaimName"], "photo")
        self.assertEqual(validation["faceCheck"]["matchConfidenceThreshold"], 70)

        pending = get_onboarding(onboarding_id)
        self.assertEqual(pending["presentation_request_id"], "face-check-request-id")
        retrieved = self.client.post(
            "/api/verifiedid/callback",
            headers={"api-key": "callback-secret"},
            json={
                "requestId": "face-check-request-id",
                "state": pending["presentation_state"],
                "requestStatus": "request_retrieved",
            },
        )
        self.assertEqual(retrieved.status_code, 200)
        pending = get_onboarding(onboarding_id)
        self.assertEqual(pending["status"], "presentation_pending")
        self.assertEqual(pending["last_callback_status"], "request_retrieved")
        self.assertEqual(pending["last_callback_request_id"], "face-check-request-id")
        self.assertIsNotNone(pending["last_callback_at"])

        self.client.post(
            "/api/verifiedid/callback",
            headers={"api-key": "callback-secret"},
            json={
                "requestId": "face-check-request-id",
                "state": pending["presentation_state"],
                "requestStatus": "presentation_verified",
                "verifiedCredentialsData": [{
                    "claims": {"employeeId": employee["employee_id"], "email": employee["email"]},
                    "faceCheck": {"matchConfidenceScore": 86.31, "sourcePhotoQuality": "HIGH"},
                }],
            },
        )

        verified = get_onboarding(onboarding_id)
        self.assertEqual(verified["status"], "facecheck_verified")
        self.assertEqual(verified["face_check_score"], 86.31)
        self.assertIsNone(verified["otp"])
        self.graph_client.create_temporary_access_pass.assert_not_called()

        retry = self.client.post(
            f"/onboarding/{onboarding_id}/verify",
            data={"verification_context": "facecheck"},
        )
        self.assertEqual(retry.status_code, 200)
        pending = get_onboarding(onboarding_id)
        self.client.post(
            "/api/verifiedid/callback",
            headers={"api-key": "callback-secret"},
            json={
                "state": pending["presentation_state"],
                "requestStatus": "presentation_verified",
                "verifiedCredentialsData": [{
                    "claims": {"employeeId": employee["employee_id"], "email": employee["email"]}
                }],
            },
        )
        failed = get_onboarding(onboarding_id)
        self.assertEqual(failed["status"], "presentation_error")
        self.assertIn("passing confidence score", failed["error"])

    # The application exposes no account-deletion route or Graph delete operation.
    def test_linked_user_cannot_be_deleted_by_demo(self):
        onboarding_id = self._create_employee()

        response = self.client.post(f"/onboarding/{onboarding_id}/delete-user")
        page = self.client.get(f"/onboarding/{onboarding_id}")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"Delete generated Entra user", page.data)
        self.graph_client.delete_user.assert_not_called()


if __name__ == "__main__":
    unittest.main()