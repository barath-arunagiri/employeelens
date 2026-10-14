"""Strict SQL guard for the agent's free-form run_sql tool.

Replaces the old substring denylist with a real parse (sqlglot):

* exactly one statement, and it must be a single SELECT (WITH is allowed)
* every table must resolve to a workspace.employeelens business table;
  the audit table (change_audit_log) is NOT readable through run_sql —
  audit questions go through the dedicated get_audit_history tool
* a LIMIT is always enforced (added when missing); OFFSET is allowed
* string literals survive intact (parsed with sqlglot's duckdb reader —
  see the note in guard_select for why)
* the returned SQL is fully qualified via the AST — nothing regex-rewrites
  the statement text afterwards

Fails closed: any parse error or unknown table rejects the query.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from src.database.uc_connection import TABLES

# Tables the agent may read through run_sql (business tables only).
# change_audit_log / employee_data_audit are deliberately excluded.
READABLE = {
    key: value for key, value in TABLES.items()
    if key not in ("change_audit_log", "employee_data_audit")
}

# Reverse map: fully-qualified table -> bare name the agent may use.
_QUALIFIED_TO_BARE = {value: key for key, value in READABLE.items()}

_AUDIT_NAMES = {"change_audit_log", "employee_data_audit"}


class UnsafeQuery(ValueError):
    """A query that the guard refuses to run."""


def guard_select(sql: str, default_limit: int = 200) -> str:
    """
    Validate a free-form query and return safe, fully-qualified SQL.

    Raises UnsafeQuery (a ValueError) for anything that is not a single
    read-only SELECT over the allowed business tables.
    """
    if not sql or not sql.strip():
        raise UnsafeQuery("empty query")

    # Parse with the duckdb reader: sqlglot's spark/databricks readers
    # mis-lex doubled-quote literals ('O''Brien') into two adjacent strings
    # (rendered as CONCAT('O','Brien')), silently changing query semantics.
    # The duckdb reader keeps them as one literal; the regenerated statement
    # uses backslash escaping ('O\'Brien'), which Databricks SQL accepts.
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except Exception as exc:  # noqa: BLE001
        raise UnsafeQuery(f"could not parse SQL ({exc})") from None

    if len(statements) != 1:
        raise UnsafeQuery("exactly one statement is allowed")

    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise UnsafeQuery("only a single SELECT statement is allowed")

    # CTE names are query-local; references to them are not tables.
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}

    # Materialize the table list: qualifying mutates the AST while we walk.
    for table in list(tree.find_all(exp.Table)):
        bare = table.name
        qualified = table.sql(dialect="databricks").lower()

        # Audit table: never readable through free-form SQL.
        if bare in _AUDIT_NAMES or qualified.endswith("change_audit_log"):
            raise UnsafeQuery(
                "the audit table is not readable through run_sql; "
                "use the get_audit_history tool for change history"
            )

        # CTE reference (no database part): skip.
        if not table.db and bare in cte_names:
            continue

        # Fully-qualified form: workspace.employeelens.<table>.
        if qualified in _QUALIFIED_TO_BARE:
            continue

        # Bare allowlisted name: qualify and rename it to the physical table
        # through the AST (e.g. employees -> workspace.employeelens.employee_master).
        if not table.db and bare in READABLE:
            catalog, schema, physical = READABLE[bare].split(".")
            table.set("catalog", exp.to_identifier(catalog))
            table.set("db", exp.to_identifier(schema))
            table.set("this", exp.to_identifier(physical))
            continue

        raise UnsafeQuery(
            f"table '{bare or qualified}' is not readable; allowed tables: "
            + ", ".join(sorted(READABLE))
        )

    # Always enforce a LIMIT.
    if not tree.args.get("limit"):
        tree = tree.limit(default_limit)

    return tree.sql(dialect="databricks")
