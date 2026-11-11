from __future__ import annotations

import unittest
from unittest.mock import patch

from src.database import repository


class CertificationProposalExecutionTests(unittest.TestCase):
    def test_insert_binds_verification_status_as_string(self):
        proposal = {
            "operation": "INSERT",
            "table": "employee_certifications",
            "employee_id": "2020119",
            "field": "certification",
            "new_value": "Databricks Certified Data Engineer Professional",
            "verified": "false",
            "requires_confirmation": True,
        }

        with (
            patch.object(repository, "_employee_exists", return_value=True),
            patch.object(repository.uc, "execute") as execute,
            patch.object(
                repository.uc,
                "fetchval",
                return_value=proposal["new_value"],
            ),
            patch.object(repository, "_audit"),
        ):
            result = repository.execute_proposal(proposal, confirmed_by="admin")

        params = execute.call_args.args[1]
        self.assertEqual(params[4], "false")
        self.assertIsInstance(params[4], str)
        self.assertEqual(result["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
