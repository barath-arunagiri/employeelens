from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from src import auth


class AdminAuthTests(unittest.TestCase):
    def test_non_company_email_never_queries_database(self):
        with patch.object(auth, "_get_uc_connection") as get_connection:
            self.assertFalse(auth.authenticate("person@example.com", "2020128"))
        get_connection.assert_not_called()

    def test_active_admin_and_matching_employee_id_authenticates(self):
        with patch.object(auth, "_get_uc_connection") as get_connection:
            get_connection.return_value.fetchval.return_value = "2020128"
            result = auth.authenticate(" MIRTHULAR@datapattern.ai ", "2020128")
        self.assertTrue(result)
        query, params = get_connection.return_value.fetchval.call_args.args
        self.assertIn("workspace.employeelens.app_admin_access", query)
        self.assertIn("a.active = true", query)
        self.assertEqual(params, ["mirthular@datapattern.ai", "2020128"])

    def test_employee_id_for_another_email_does_not_authenticate(self):
        with patch.object(auth, "_get_uc_connection") as get_connection:
            get_connection.return_value.fetchval.return_value = None
            self.assertFalse(
                auth.authenticate("mirthular@datapattern.ai", "2020999")
            )

    def test_unmatched_database_value_does_not_authenticate(self):
        with patch.object(auth, "_get_uc_connection") as get_connection:
            get_connection.return_value.fetchval.return_value = "2020129"
            self.assertFalse(
                auth.authenticate("mirthular@datapattern.ai", "2020128")
            )

    def test_grant_requires_active_actor_and_matching_employee(self):
        database = Mock()
        with patch.object(auth, "_get_uc_connection", return_value=database):
            with patch.object(auth, "is_admin", side_effect=[True, True]):
                database.fetchval.return_value = "2020200"
                auth.grant_admin_access(
                    "baratha@datapattern.ai",
                    "New.User@datapattern.ai",
                    "2020200",
                )

        params = database.execute.call_args.args[1]
        self.assertEqual(params, ["new.user@datapattern.ai",
                                  "baratha@datapattern.ai"])
        self.assertIn("MERGE INTO workspace.employeelens.app_admin_access",
                      database.execute.call_args.args[0])

    def test_grant_rejects_non_admin_actor(self):
        with (
            patch.object(auth, "is_admin", return_value=False),
            patch.object(auth, "_get_uc_connection") as get_connection,
            self.assertRaisesRegex(PermissionError, "Only an active admin"),
        ):
            auth.grant_admin_access(
                "outsider@datapattern.ai",
                "new.user@datapattern.ai",
                "2020200",
            )
        get_connection.assert_not_called()

    def test_revoke_cannot_remove_last_active_admin(self):
        database = Mock()
        database.fetchval.side_effect = [
            "baratha@datapattern.ai",
            "1",
        ]
        with (
            patch.object(auth, "_get_uc_connection", return_value=database),
            patch.object(auth, "is_admin", return_value=True),
            self.assertRaisesRegex(ValueError, "last active admin"),
        ):
            auth.revoke_admin_access(
                "baratha@datapattern.ai", "baratha@datapattern.ai"
            )
        database.execute.assert_not_called()

    def test_revoke_marks_access_inactive(self):
        database = Mock()
        database.fetchval.side_effect = [
            "baratha@datapattern.ai",
            "2",
        ]
        with (
            patch.object(auth, "_get_uc_connection", return_value=database),
            patch.object(auth, "is_admin", side_effect=[True, False]),
        ):
            auth.revoke_admin_access(
                "baratha@datapattern.ai", "mirthular@datapattern.ai"
            )

        query, params = database.execute.call_args.args
        self.assertIn("SET active = false", query)
        self.assertEqual(params, [
            "baratha@datapattern.ai",
            "mirthular@datapattern.ai",
        ])


if __name__ == "__main__":
    unittest.main()
