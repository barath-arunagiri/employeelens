from __future__ import annotations

import unittest

from src.user_identity import get_verified_user_email


class UserIdentityTests(unittest.TestCase):
    def test_accepts_databricks_apps_forwarded_identity(self):
        self.assertEqual(
            get_verified_user_email(
                {"x-forwarded-email": "admin@example.com"},
                {},
                trust_databricks_headers=True,
            ),
            "admin@example.com",
        )

    def test_rejects_forwarded_headers_unless_trusted_deployment(self):
        self.assertEqual(
            get_verified_user_email(
                {"x-forwarded-email": "admin@example.com"},
                {},
            ),
            "",
        )

    def test_accepts_verified_oidc_identity(self):
        self.assertEqual(
            get_verified_user_email(
                {},
                {
                    "is_logged_in": True,
                    "email_verified": True,
                    "email": "admin@example.com",
                },
            ),
            "admin@example.com",
        )

    def test_rejects_unverified_or_logged_out_oidc_identity(self):
        for user in (
            {"is_logged_in": True, "email": "admin@example.com"},
            {
                "is_logged_in": True,
                "email_verified": False,
                "email": "admin@example.com",
            },
            {
                "is_logged_in": False,
                "email_verified": True,
                "email": "admin@example.com",
            },
        ):
            with self.subTest(user=user):
                self.assertEqual(get_verified_user_email({}, user), "")


if __name__ == "__main__":
    unittest.main()
