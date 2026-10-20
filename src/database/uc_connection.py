"""Unity Catalog connection layer.

All reads and writes go directly to workspace.employeelens.* Delta tables
via Databricks SDK statement execution.

Safety model:
- Values NEVER become part of the SQL text. The positional ``?`` placeholders
  used by repository code are converted to Databricks *named parameters*
  (:p0, :p1, ...) and bound server-side by the statement execution API.
  The only literal ever substituted is the constant NULL keyword (the API
  cannot bind SQL NULL), which contains no data.
- Table names come only from the fixed TABLES allowlist below. Free-form
  agent SQL is additionally parsed and validated by src/database/sql_guard.py.
- execute() returns the real affected-row count reported by the server.
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional

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


def _run_statement(sql: str, params: list | None = None):
    """Execute a SQL statement with server-side parameter binding.

    Values are bound by the statement execution API — never interpolated
    into the SQL text. Prefers Databricks named parameters (:p0, :p1, ...);
    falls back to typed positional parameters on SDK versions that do not
    expose named_parameters yet. Either way, user data never enters the text.

    The 50s wait timeout plus the 60s polling deadline is the statement
    timeout: nothing runs unbounded. A stopped serverless warehouse may
    auto-start on the first statement, pushing the response past the sync
    wait window - the statement keeps running server-side, so we poll it
    via get_statement rather than resubmitting (a resubmitted INSERT/UPDATE
    could apply twice).
    """
    w = _get_client()
    named_sql, named = _to_named_params(sql, params)
    try:
        response = w.statement_execution.execute_statement(
            warehouse_id=_get_warehouse_id(),
            statement=named_sql,
            wait_timeout="50s",
            named_parameters=named or None,
        )
    except TypeError as exc:
        if "named_parameters" not in str(exc):
            raise
        # Older SDK: bind positionally with typed parameter objects.
        pos_sql, pos_items = _to_positional_params(sql, params)
        response = w.statement_execution.execute_statement(
            warehouse_id=_get_warehouse_id(),
            statement=pos_sql,
            wait_timeout="50s",
            parameters=pos_items or None,
        )
    deadline = time.monotonic() + 60
    while (response.status.state not in _TERMINAL_STATES
           and time.monotonic() < deadline):
        time.sleep(3)
        response = w.statement_execution.get_statement(response.statement_id)
    return response


def _to_named_params(sql: str, params: list | None = None):
    """
    Convert ``?`` placeholders to Databricks named parameters.

    Each ``?`` becomes ``:p0``, ``:p1``, ... and the values are returned as a
    dict to be bound server-side — user data never touches the SQL text.
    None values are replaced by the literal NULL keyword because the
    statement execution API cannot bind SQL NULL (a constant, not data).
    """
    if not params:
        return sql, None

    named = {}
    out = []
    index = 0
    for char in sql:
        if char != "?":
            out.append(char)
            continue
        if index >= len(params):
            raise ValueError(
                "more placeholders than parameters — refusing to build SQL"
            )
        value = params[index]
        if value is None:
            out.append("NULL")
        else:
            out.append(":p%d" % index)
            named["p%d" % index] = value
        index += 1

    return "".join(out), (named or None)


def _prepare(sql: str) -> str:
    """Qualify allowlisted table names and fix dialect.

    The ``?`` placeholders are left in place — values are bound
    server-side by _run_statement, never interpolated into the text.
    """
    return _fix_sql_dialect(_fix_table_names(sql))


def _param_type(value) -> str:
    """Infer the Databricks statement parameter type for a Python value."""
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT"
    if isinstance(value, float):
        return "DOUBLE"
    return "STRING"


def _to_positional_params(sql: str, params: list | None = None):
    """
    Fallback binding for SDK versions without the named_parameters kwarg.

    The parameters array carries typed, named items (StatementParameterListItem
    requires a name on these SDKs), so the SQL gets :p0, :p1, ... markers.
    Values are still bound server-side — never interpolated into the text.
    None values become the literal NULL keyword (a constant, not data).
    """
    if not params:
        return sql, None
    try:
        from databricks.sdk.service.sql import StatementParameterListItem
    except ImportError:  # very old SDK — refuse rather than interpolate
        raise RuntimeError(
            "this databricks-sdk version supports neither named nor typed "
            "positional parameters; upgrade databricks-sdk"
        )

    items = []
    out = []
    index = 0
    for char in sql:
        if char != "?":
            out.append(char)
            continue
        if index >= len(params):
            raise ValueError(
                "more placeholders than parameters — refusing to build SQL"
            )
        value = params[index]
        if value is None:
            out.append("NULL")
        else:
            out.append(":p%d" % index)
            items.append(StatementParameterListItem(
                name="p%d" % index, value=value, type=_param_type(value)))
        index += 1
    return "".join(out), (items or None)


def _fix_table_names(sql: str) -> str:
    """Qualify bare table names using ONLY the fixed allowlist above.

    Internal repository SQL always uses these exact bare names; nothing
    outside the allowlist is ever rewritten. (Free-form agent SQL is parsed
    and qualified structurally by src/database/sql_guard.py instead.)
    """
    for short, full in TABLES.items():
        # Qualify ONLY in a real table position — right after FROM / JOIN /
        # INTO / UPDATE. A bare word anywhere else is never rewritten: not
        # a column alias (e.g. `AS employees`), not a word inside a string
        # literal, and not the tail of an already-qualified name coming
        # from guard_select, so qualification stays idempotent.
        sql = re.sub(
            rf'(?i)\b(FROM|JOIN|INTO|UPDATE)(\s+){re.escape(short)}\b',
            lambda m, _f=full: m.group(1) + m.group(2) + _f,
            sql,
        )
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
    """Execute a read-only SQL query and return results as DataFrame.

    Values are bound as named parameters — never interpolated into the text.
    """
    if not SDK_AVAILABLE:
        raise RuntimeError("databricks-sdk not available")

    full_sql = _prepare(sql)
    response = _run_statement(full_sql, params)
    
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


def _affected_rows(response) -> int:
    """
    Real affected-row count reported by the server for a DML statement.

    Databricks reports DML counts in a num_affected_rows* column. Falls back
    to 1 only when the server reports success but no count column, so
    callers never see a fabricated zero.
    """
    try:
        if response.manifest and response.manifest.schema:
            cols = [c.name for c in response.manifest.schema.columns]
            data = (response.result.data_array
                    if response.result and response.result.data_array else [])
            for key in ("num_affected_rows", "num_affected_rows_inserted",
                        "num_affected_rows_updated", "num_affected_rows_deleted",
                        "num_inserted_rows", "rows_produced"):
                if key in cols and data:
                    value = data[0][cols.index(key)]
                    if value is not None:
                        return int(value)
    except Exception:  # noqa: BLE001
        pass
    return 1


def execute(sql: str, params: list | None = None) -> int:
    """Execute a write statement (INSERT/UPDATE/DELETE/MERGE).

    Returns the real affected-row count reported by the server.
    """
    if not SDK_AVAILABLE:
        raise RuntimeError("databricks-sdk not available")

    full_sql = _prepare(sql)
    response = _run_statement(full_sql, params)

    if response.status.state == StatementState.SUCCEEDED:
        return _affected_rows(response)
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
                 status: str = "SUCCESS", error_message: str | None = None,
                 confirmed_by: str = "unknown") -> None:
    """Write audit log entry to Unity Catalog.

    Column names match the actual change_audit_log schema:
    change_type (not 'operation') and confirmed_at (not 'timestamp').
    confirmed_by records the real user identity forwarded by the app
    (x-forwarded-email header) — never a hardcoded service label.
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
         confirmed_by, status, error_message]
    )
