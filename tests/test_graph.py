"""Verify Graph request construction with mocked HTTP responses."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.services.graph import GraphClient


class GraphClientTests(unittest.TestCase):
    """Check Graph endpoints, payloads, paging, and TAP retry behavior."""

    def setUp(self):
        # Bypass MSAL initialization; every test supplies a fixed local token.
        self.client = object.__new__(GraphClient)
        self.client.config = {"tapLifetimeMinutes": 60}
        self.client._access_token = Mock(return_value="token")

    # Directory reads must resolve exact users and preserve Graph paging semantics.
    @patch("app.services.graph.requests.get")
    def test_get_user_by_upn_resolves_exact_existing_account(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {
            "id": "existing-object-id",
            "userPrincipalName": "new.hire@example.com",
            "givenName": "New",
            "surname": "Hire",
        }

        result = self.client.get_user_by_upn(" new.hire@example.com ")

        self.assertIn("/users/new.hire%40example.com", get.call_args.args[0])
        self.assertIn("id,userPrincipalName", get.call_args.kwargs["params"]["$select"])
        self.assertIn("otherMails", get.call_args.kwargs["params"]["$select"])
        self.assertEqual(result["id"], "existing-object-id")

    @patch("app.services.graph.requests.get")
    def test_list_users_filters_office_location_and_follows_pages(self, get):
        first_page = Mock(status_code=200)
        first_page.json.return_value = {
            "value": [{"id": "user-1", "officeLocation": "VIDDEMO"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=next",
        }
        second_page = Mock(status_code=200)
        second_page.json.return_value = {
            "value": [{"id": "user-2", "officeLocation": "VIDDEMO"}],
        }
        get.side_effect = [first_page, second_page]

        users = self.client.list_users_by_office_location("VIDDEMO")

        self.assertEqual([user["id"] for user in users], ["user-1", "user-2"])
        self.assertEqual(get.call_args_list[0].kwargs["params"]["$filter"], "officeLocation eq 'VIDDEMO'")
        self.assertEqual(get.call_args_list[0].kwargs["params"]["$count"], "true")
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["ConsistencyLevel"], "eventual")
        self.assertIn("otherMails", get.call_args_list[0].kwargs["params"]["$select"])
        self.assertIsNone(get.call_args_list[1].kwargs["params"])

    # Directory writes are limited to enablement and requested mail delivery.
    @patch("app.services.graph.requests.patch")
    def test_enable_user_updates_only_account_enabled(self, patch_request):
        patch_request.return_value.status_code = 204

        self.client.enable_user("existing-object-id")

        self.assertIn("/users/existing-object-id", patch_request.call_args.args[0])
        self.assertEqual(patch_request.call_args.kwargs["json"], {"accountEnabled": True})

    @patch("app.services.graph.requests.post")
    def test_send_onboarding_email_uses_configured_sender_and_personal_recipient(self, post):
        post.return_value.status_code = 202
        self.client.config["onboardingSenderUpn"] = "admin@example.com"

        self.client.send_onboarding_email(
            "new.hire.personal@example.net", "https://demo.example/issue/secret-token"
        )

        self.assertIn("/users/admin%40example.com/sendMail", post.call_args.args[0])
        message = post.call_args.kwargs["json"]["message"]
        self.assertEqual(
            message["toRecipients"][0]["emailAddress"]["address"],
            "new.hire.personal@example.net",
        )
        self.assertIn("https://demo.example/issue/secret-token", message["body"]["content"])

    # TAP tests verify one-time use and the tenant-policy lifetime retry.
    @patch("app.services.graph.requests.post")
    def test_create_tap_uses_returned_user_object_id(self, post):
        post.return_value.status_code = 201
        post.return_value.json.return_value = {
            "temporaryAccessPass": "tap-value",
            "lifetimeInMinutes": 60,
        }

        self.client.create_temporary_access_pass("generated-object-id")

        self.assertIn("/users/generated-object-id/authentication/", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["json"]["isUsableOnce"], True)

    @patch("app.services.graph.requests.post")
    def test_create_tap_retries_with_tenant_policy_lifetime(self, post):
        rejected = Mock(status_code=400)
        rejected.json.return_value = {
            "error": {"message": "Invalid LifetimeInMinutes specified. The valid range between 59 and 59."}
        }
        created = Mock(status_code=201)
        created.json.return_value = {"temporaryAccessPass": "tap-value"}
        post.side_effect = [rejected, created]
        self.client.config["tapLifetimeMinutes"] = 60

        result = self.client.create_temporary_access_pass("generated-object-id")

        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["lifetimeInMinutes"], 60)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["lifetimeInMinutes"], 59)
        self.assertEqual(result["lifetimeInMinutes"], 59)


if __name__ == "__main__":
    unittest.main()