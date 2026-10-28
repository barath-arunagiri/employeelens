"""Bridge Streamlit Community Cloud secrets to Databricks SDK environment config."""
from __future__ import annotations

import os
from collections.abc import Mapping


_DATABRICKS_SECRET_ENV = {
    "host": "DATABRICKS_HOST",
    "client_id": "DATABRICKS_CLIENT_ID",
    "client_secret": "DATABRICKS_CLIENT_SECRET",
    "sql_warehouse_id": "DATABRICKS_SQL_WAREHOUSE_ID",
    "model": "DATABRICKS_MODEL",
    "data_management_enabled": "DATA_MANAGEMENT_ENABLED",
    "data_management_admins": "DATA_MANAGEMENT_ADMINS",
}


def apply_databricks_secrets(secrets: Mapping) -> None:
    """Expose the [databricks] Streamlit secrets to SDKs that read environment variables."""
    settings = secrets.get("databricks", {})
    if not isinstance(settings, Mapping):
        raise ValueError("Streamlit [databricks] secrets must be a TOML table.")

    for secret_name, environment_name in _DATABRICKS_SECRET_ENV.items():
        value = settings.get(secret_name)
        if value is not None and not os.environ.get(environment_name):
            os.environ[environment_name] = str(value)
