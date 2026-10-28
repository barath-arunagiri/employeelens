from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.streamlit_config import apply_databricks_secrets


class StreamlitConfigTests(unittest.TestCase):
    def test_maps_databricks_secrets_to_sdk_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            apply_databricks_secrets({
                "databricks": {
                    "host": "https://workspace.example.com",
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "sql_warehouse_id": "warehouse-id",
                    "model": "databricks-gpt-oss-120b",
                    "data_management_enabled": False,
                    "data_management_admins": "",
                }
            })

            self.assertEqual(os.environ["DATABRICKS_HOST"],
                             "https://workspace.example.com")
            self.assertEqual(os.environ["DATABRICKS_CLIENT_ID"], "client-id")
            self.assertEqual(os.environ["DATABRICKS_CLIENT_SECRET"],
                             "client-secret")
            self.assertEqual(os.environ["DATABRICKS_SQL_WAREHOUSE_ID"],
                             "warehouse-id")
            self.assertEqual(os.environ["DATABRICKS_MODEL"],
                             "databricks-gpt-oss-120b")
            self.assertEqual(os.environ["DATA_MANAGEMENT_ENABLED"], "False")
            self.assertEqual(os.environ["DATA_MANAGEMENT_ADMINS"], "")

    def test_environment_values_take_precedence(self):
        with patch.dict(os.environ, {"DATABRICKS_HOST": "https://env.example.com"},
                        clear=True):
            apply_databricks_secrets({
                "databricks": {"host": "https://secret.example.com"}
            })
            self.assertEqual(os.environ["DATABRICKS_HOST"],
                             "https://env.example.com")

    def test_rejects_non_table_databricks_secrets(self):
        with self.assertRaisesRegex(ValueError, r"\[databricks\]"):
            apply_databricks_secrets({"databricks": "not a table"})


if __name__ == "__main__":
    unittest.main()
