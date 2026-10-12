"""Unity Catalog connection layer.

Replaces DuckDB entirely. All reads and writes go directly to
workspace.employeelens.* Delta tables via Databricks SDK statement execution.

No DuckDB, no CSV, no caching — Unity Catalog is the single source of truth.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd

try:
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

# Unity Catalog table names
UC = "workspace.employeelens"
TABLES = {
    "employees": f"{UC}.employee_master",
    "employee_skills": f"{UC}.employee_skills",
    "employee_projects": f"{UC}.employee_projects",
    "employee_certifications": f"{UC}.employee_certifications",
    "employee_managers": f"{UC}.employee_managers",
    "change_audit_log": f"{UC}.change_audit_log",
    "employee_data_audit": f"{UC}.change_audit_log",
}

_warehouse_id: Optional[str] = None
_client = None


def _get_client():
    """Reuse one WorkspaceClient across queries. Creating a fresh client for
    every single statement forced a new OAuth token exchange per query, which
    multiplied the latency of multi-query operations (e.g. a 360 profile)."""
    global _client
    if _client is None:
        _client = WorkspaceClient()
    return _client


def _get_warehouse_id() -> str:
    global _warehouse_id
    if _warehouse_id:
        return _warehouse_id
    # Prefer the warehouse configured in app.yaml via DATABRICKS_SQL_WAREHOUSE_ID
    # (falls back to the first warehouse in the workspace if unset).
    _warehouse_id = os.environ.get("DATABRICKS_SQL_WAREHOUSE_ID") or None
    if not _warehouse_id:
        w = _get_client()
        warehouses = list(w.warehouses.list())
        if not warehouses:
            raise RuntimeError("No SQL warehouse available")
        _warehouse_id = warehouses[0].id
    return _warehouse_id


_TERMINAL_STATES = (
    StatementState.SUCCEEDED,
    StatementState.FAILED,
    StatementState.CANCELED,
)


def _run_statement(sql: str):
    """Execute a SQL statement, tolerating serverless warehouse cold-start.

    A stopped serverless warehouse auto-starts on the first statement, which can
    push the response past the sync wait window - it then comes back in a
    non-terminal state (PENDING/RUNNING) with no error. The statement keeps
    running server-side, so we poll it via get_statement rather than
    resubmitting (a resubmitted INSERT/UPDATE could apply twice).
    """
    w = _get_client()
    response = w.statement_execution.execute_statement(
        warehouse_id=_get_warehouse_id(),
        statement=sql,
        wait_timeout="50s",
    )
    deadline = time.monotonic() + 60
    while (response.status.state not in _TERMINAL_STATES
           and time.monotonic() < deadline):
        time.sleep(3)
        response = w.statement_execution.get_statement(response.statement_id)
    return response


def _escape(value) -> str:
    """Safely escape a Python value for SQL."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value).replace("'", "''")
    return f"'{s}'"


def _build_sql(sql: str, params: list | None = None) -> str:
    """Replace ? placeholders with escaped values."""
    if not params:
        return sql
    result = sql
    for p in params:
        result = result.replace("?", _escape(p), 1)
    return result


def _fix_table_names(sql: str) -> str:
    """Replace bare table names with full UC paths.
    
    Note: Query aliases should NOT use table names to avoid conflicts.
    """
    for short, full in TABLES.items():
        # Match word boundary + table name + word boundary
        sql = re.sub(rf'\b{re.escape(short)}\b', full, sql)
    return sql


def _fix_sql_dialect(sql: str) -> str:
    """Convert DuckDB SQL syntax to Databricks SQL."""
    # string_agg(col, ', ') -> concat_ws(',', collect_set(col))
    sql = re.sub(
        r"string_agg\(DISTINCT\s+(.+?),\s*'([^']*)'\)",
        r"concat_ws('\2', collect_set(\1))",
        sql
    )
    # COUNT(DISTINCT col) FILTER (WHERE condition) ->
    # COUNT(DISTINCT CASE WHEN condition THEN col END)
    sql = re.sub(
        r'COUNT\(DISTINCT\s+(\S+)\)\s*FILTER\s*\(WHERE\s+(.+?)\)',
        r'COUNT(DISTINCT CASE WHEN \2 THEN \1 END)',
        sql
    )
    # COUNT(*) FILTER (WHERE condition) ->
    # COUNT(CASE WHEN condition THEN 1 END)
    sql = re.sub(
        r'COUNT\(\*\)\s*FILTER\s*\(WHERE\s+(.+?)\)',
        r'COUNT(CASE WHEN \1 THEN 1 END)',
        sql
    )
    return sql


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    """Execute a read-only SQL query and return results as DataFrame."""
    if not SDK_AVAILABLE:
        raise RuntimeError("databricks-sdk not available")
    
    full_sql = _build_sql(sql, params)
    full_sql = _fix_table_names(full_sql)
    full_sql = _fix_sql_dialect(full_sql)
    
    response = _run_statement(full_sql)
    
    if response.status.state == StatementState.SUCCEEDED:
        if not response.result or not response.result.data_array:
            # Return empty DataFrame with columns from manifest
            cols = []
            if response.manifest and response.manifest.schema:
                cols = [c.name for c in response.manifest.schema.columns]
            return pd.DataFrame(columns=cols)
        
        cols = [c.name for c in response.manifest.schema.columns]
        rows = [list(row) for row in response.result.data_array]
        return pd.DataFrame(rows, columns=cols)
    else:
        error_msg = response.status.error.message if response.status.error else "no error details"
        raise RuntimeError(f"SQL error ({response.status.state}): {error_msg}\nQuery: {full_sql[:200]}")


def execute(sql: str, params: list | None = None) -> int:
    """Execute a write statement (INSERT/UPDATE/DELETE/MERGE). Returns affected rows."""
    if not SDK_AVAILABLE:
        raise RuntimeError("databricks-sdk not available")
    
    full_sql = _build_sql(sql, params)
    full_sql = _fix_table_names(full_sql)
    
    response = _run_statement(full_sql)
    
    if response.status.state == StatementState.SUCCEEDED:
        return 1  # Success
    else:
        error_msg = response.status.error.message if response.status.error else "no error details"
        raise RuntimeError(f"SQL error ({response.status.state}): {error_msg}\nQuery: {full_sql[:200]}")


def fetchone(sql: str, params: list | None = None):
    """Execute query and return first row as tuple, or None."""
    df = query(sql, params)
    if df.empty:
        return None
    return tuple(df.iloc[0])


def fetchval(sql: str, params: list | None = None):
    """Execute query and return single scalar value, or None."""
    row = fetchone(sql, params)
    if row is None:
        return None
    return row[0]


def is_available() -> bool:
    """Check if Unity Catalog is accessible."""
    if not SDK_AVAILABLE:
        return False
    try:
        _get_warehouse_id()
        return True
    except Exception:
        return False


def write_audit(operation: str, employee_id: str, table_name: str,
                 field_name: str | None, old_value, new_value,
                 status: str = "SUCCESS", error_message: str | None = None) -> None:
    """Write audit log entry to Unity Catalog.

    Column names match the actual change_audit_log schema:
    change_type (not 'operation') and confirmed_at (not 'timestamp').
    """
    execute(
        """INSERT INTO change_audit_log
           (employee_id, change_type, table_name, field_name,
            old_value, new_value, confirmed_by, confirmed_at,
            status, error_message)
           VALUES (?, ?, ?, ?, ?, ?, ?, current_timestamp(), ?, ?)""",
        [employee_id, operation, table_name, field_name,
         str(old_value) if old_value is not None else None,
         str(new_value) if new_value is not None else None,
         "data_management", status, error_message]
    )
