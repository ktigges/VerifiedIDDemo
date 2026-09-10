from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.services.graph import GraphClient


class GraphClientTests(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(GraphClient)
        self.client.config = {"tapLifetimeMinutes": 60}
        self.client._access_token = Mock(return_value="token")

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
        self.assertEqual(result["id"], "existing-object-id")

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