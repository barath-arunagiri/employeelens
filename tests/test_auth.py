from __future__ import annotations

import unittest
from unittest.mock import patch

from src import auth


class AdminAuthTests(unittest.TestCase):
    def test_only_allowlisted_emails_query_employee_table(self):
        with patch.object(auth, "_get_uc_connection") as get_connection:
            self.assertFalse(auth.authenticate("other@datapattern.ai", "2020128"))
        get_connection.assert_not_called()

    def test_allowlisted_email_and_matching_employee_id_authenticates(self):
        with patch.object(auth, "_get_uc_connection") as get_connection:
            get_connection.return_value.fetchval.return_value = "2020128"
            result = auth.authenticate(" MIRTHULAR@datapattern.ai ", "2020128")
        self.assertTrue(result)
        get_connection.return_value.fetchval.assert_called_once_with(
            "SELECT employee_id FROM employees "
            "WHERE lower(email) = ? AND employee_id = ?",
            ["mirthular@datapattern.ai", "2020128"],
        )

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


if __name__ == "__main__":
    unittest.main()
