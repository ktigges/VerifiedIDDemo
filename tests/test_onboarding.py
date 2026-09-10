from __future__ import annotations

import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

import app.employees as employee_store
from app import create_app
from app.employees import get_onboarding, update_onboarding


class OnboardingFlowTests(unittest.TestCase):
    def setUp(self):
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
        self.app = create_app(
            {
                "TESTING": True,
                "azTenantId": "tenant-id",
                "azClientId": "client-id",
                "azClientSecret": "client-secret",
                "DidAuthority": "did:web:issuer.example",
                "CredentialManifest": "https://verifiedid.did.msidentity.com/v1.0/tenant-id/verifiableCredentials/contracts/VerifiedEmployeeCard",
                "CredentialType": "VerifiedEmployeeCard",
                "acceptedIssuersList": ["did:web:issuer.example"],
                "organizationName": "Example Organization",
                "apiKey": "callback-secret",
                "publicBaseUrl": "https://demo.example",
                "accessMode": "demoOtp",
                "faceCheckMatchConfidenceThreshold": 70,
            }
        )
        self.client = self.app.test_client()
        self.graph_patcher = patch("app.GraphClient")
        self.graph_client_class = self.graph_patcher.start()
        self.addCleanup(self.graph_patcher.stop)
        self.graph_client = self.graph_client_class.return_value

        def get_user_by_upn(upn):
            return {
                "id": f"object-{upn}",
                "userPrincipalName": upn,
                "givenName": "New",
                "surname": "Hire",
                "jobTitle": "Engineer",
                "department": "Technology",
                "accountEnabled": False,
            }

        self.graph_client.get_user_by_upn.side_effect = get_user_by_upn

    def _create_employee(self, upn: str = "new.hire@example.com") -> str:
        response = self.client.post(
            "/onboarding",
            data={"user_principal_name": upn},
        )
        self.assertEqual(response.status_code, 302)
        return response.headers["Location"].rsplit("/", 1)[-1]

    @staticmethod
    def _employee_photo():
        photo = BytesIO()
        Image.new("RGB", (240, 240), "#486b7a").save(photo, format="JPEG")
        photo.seek(0)
        return photo, "employee.jpg"

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

    def test_demo_employee_picker_skips_to_flow_choices(self):
        onboarding_id = self._create_employee()
        excluded_id = self._create_employee()
        update_onboarding(
            onboarding_id,
            status="helpdesk_verified",
            presentation_state="old-presentation",
            presentation_context="helpdesk",
            otp="old-code",
        )
        update_onboarding(excluded_id, status="credential_issued", demo_employee=False)

        picker = self.client.get("/")
        self.assertIn(onboarding_id.encode(), picker.data)
        self.assertNotIn(excluded_id.encode(), picker.data)
        excluded_response = self.client.post(
            "/demo/select-employee",
            data={"onboarding_id": excluded_id},
        )
        self.assertEqual(excluded_response.status_code, 404)

        response = self.client.post("/demo/select-employee", data={"onboarding_id": onboarding_id})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/onboarding/{onboarding_id}"))
        selected = get_onboarding(onboarding_id)
        self.assertEqual(selected["status"], "credential_issued")
        self.assertIsNone(selected["presentation_state"])
        self.assertIsNone(selected["otp"])
        choices = self.client.get(response.headers["Location"])
        self.assertIn(b"Continue onboarding", choices.data)
        self.assertIn(b"Verify help-desk caller", choices.data)

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

    @patch("app.VerifiedIdClient")
    def test_access_code_requires_matching_verified_credential(self, verified_id_client):
        client = verified_id_client.return_value
        client.create_issuance_request.return_value = {"url": "openid-initiate-issuance://request"}
        client.create_presentation_request.return_value = {"url": "openid-vc://request"}
        onboarding_id = self._create_employee()
        employee = get_onboarding(onboarding_id)

        issuance_response = self.client.post(
            f"/onboarding/{onboarding_id}/issue",
            data={"employee_photo": self._employee_photo()},
            content_type="multipart/form-data",
        )
        self.assertEqual(issuance_response.status_code, 200)
        issuance_payload = client.create_issuance_request.call_args.args[0]
        self.assertIn("manifest", issuance_payload)
        self.assertNotIn("manifestUrl", issuance_payload)
        self.assertEqual(issuance_payload["claims"]["employee_id"], employee["employee_id"])
        self.assertTrue(issuance_payload["claims"]["photo"].startswith("%2F9j%2F"))
        self.assertTrue(get_onboarding(onboarding_id)["face_check_capable"])

        employee = get_onboarding(onboarding_id)
        self.client.post(
            "/api/verifiedid/callback",
            headers={"api-key": "callback-secret"},
            json={"state": employee["issuance_state"], "requestStatus": "issuance_successful"},
        )
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

    @patch("app.VerifiedIdClient")
    def test_issuance_rejects_invalid_employee_photo(self, verified_id_client):
        onboarding_id = self._create_employee()

        response = self.client.post(
            f"/onboarding/{onboarding_id}/issue",
            data={"employee_photo": (BytesIO(b"not-a-jpeg"), "employee.jpg")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        employee = get_onboarding(onboarding_id)
        self.assertEqual(employee["status"], "issuance_error")
        self.assertIn("valid JPEG", employee["error"])
        self.assertFalse(employee["face_check_capable"])
        verified_id_client.return_value.create_issuance_request.assert_not_called()

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
    def test_face_check_presentation_requires_passing_score_without_creating_tap(self, verified_id_client):
        verified_id_client.return_value.create_presentation_request.return_value = {
            "url": "openid-vc://face-check-request"
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
        self.client.post(
            "/api/verifiedid/callback",
            headers={"api-key": "callback-secret"},
            json={
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

    def test_linked_user_cannot_be_deleted_by_demo(self):
        onboarding_id = self._create_employee()

        response = self.client.post(f"/onboarding/{onboarding_id}/delete-user")
        page = self.client.get(f"/onboarding/{onboarding_id}")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"Delete generated Entra user", page.data)
        self.graph_client.delete_user.assert_not_called()


if __name__ == "__main__":
    unittest.main()