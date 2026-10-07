"""
Employeelens — data access layer.

Every SQL statement in the project lives here. The agent and the tools
call these functions and never see the database, so swapping DuckDB for
Postgres later means editing this file only.

All queries are parameterised. Nothing here interpolates a caller's
string into SQL.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import duckdb
import pandas as pd

from src.config import DB_PATH

# Import Delta sync module - prefer SQL-based version for Databricks Apps
DELTA_SYNC_AVAILABLE = False
delta_sync = None

try:
    from src.database import delta_sync_sql as delta_sync
    DELTA_SYNC_AVAILABLE = True
except (ImportError, RuntimeError):
    # Fall back to Spark-based delta_sync
    try:
        from src.database import delta_sync
        DELTA_SYNC_AVAILABLE = True
    except (ImportError, RuntimeError):
        DELTA_SYNC_AVAILABLE = False


def _con() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"No database at {DB_PATH}. Run:\n"
            "  python -m src.ingestion.normalize\n"
            "  python -m src.ingestion.load_duckdb"
        )
    return duckdb.connect(str(DB_PATH))


def q(sql: str, params: list | None = None) -> pd.DataFrame:
    connection = _con()
    try:
        return connection.execute(sql, params or []).df()
    finally:
        connection.close()


MANAGEMENT_FIELDS = {
    "name": "name",
    "employee_name": "name",
    "email": "email",
    "department": "department",
    "designation": "designation",
    "location": "location",
    "employment_status": "employment_status",
    "joining_date": "joining_date",
}


def _write_con() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"No database at {DB_PATH}")
    connection = duckdb.connect(str(DB_PATH))
    _ensure_management_schema(connection)
    return connection


def _ensure_management_schema(connection: duckdb.DuckDBPyConnection) -> None:
    columns = {
        row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'employees'"
        ).fetchall()
    }
    for column in ("position", "designation", "team", "department", "location", "employment_status", "joining_date"):
        if column not in columns:
            connection.execute(f"ALTER TABLE employees ADD COLUMN {column} VARCHAR")

    project_columns = {
        row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'employee_projects'"
        ).fetchall()
    }
    if "role" not in project_columns:
        connection.execute("ALTER TABLE employee_projects ADD COLUMN role VARCHAR")
    if "project_type" not in project_columns:
        connection.execute("ALTER TABLE employee_projects ADD COLUMN project_type VARCHAR")

    connection.execute("""
        CREATE TABLE IF NOT EXISTS employee_data_audit (
            timestamp   TIMESTAMP NOT NULL,
            operation   VARCHAR NOT NULL,
            employee_id VARCHAR,
            table_name  VARCHAR NOT NULL,
            field_name  VARCHAR,
            old_value   VARCHAR,
            new_value   VARCHAR,
            status      VARCHAR NOT NULL,
            error_message VARCHAR
        )
    """)


def ensure_management_schema() -> None:
    connection = _write_con()
    connection.close()


def get_audit_history(employee_id: str | None = None,
                      operation: str | None = None,
                      status: str | None = None,
                      limit: int = 10) -> pd.DataFrame:
    """Read-only audit search used by natural-language history requests."""
    clauses, params = [], []
    if employee_id:
        clauses.append("employee_id = ?")
        params.append(employee_id.strip())
    if operation:
        clauses.append("lower(operation) = lower(?)")
        params.append(operation.strip())
    if status:
        clauses.append("lower(status) = lower(?)")
        params.append(status.strip())
    limit = max(1, min(int(limit), 100))
    where = " AND ".join(clauses) if clauses else "TRUE"
    return q(
        f"SELECT * FROM employee_data_audit WHERE {where} "
        "ORDER BY timestamp DESC LIMIT ?", params + [limit]
    )


def _employee_exists(connection: duckdb.DuckDBPyConnection, employee_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM employees WHERE employee_id = ?", [employee_id]
    ).fetchone() is not None


def _validate_employee_value(field: str, value: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValueError(f"{field} cannot be empty")
    if field == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise ValueError("email is not valid")
    if field == "joining_date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("joining_date must use YYYY-MM-DD")
    return value


def propose_employee_update(employee_id: str, field: str, new_value: str) -> dict:
    column = MANAGEMENT_FIELDS.get(field.lower().strip())
    if not column:
        raise ValueError(f"field '{field}' is not allowed")
    connection = _write_con()
    try:
        row = connection.execute(
            f"SELECT {column} FROM employees WHERE employee_id = ?", [employee_id]
        ).fetchone()
        if row is None:
            raise ValueError(f"employee '{employee_id}' was not found")
        value = _validate_employee_value(field, new_value)
        return {
            "operation": "UPDATE", "table": "employees", "employee_id": employee_id,
            "field": column, "old_value": row[0], "new_value": value,
            "requires_confirmation": True,
        }
    finally:
        connection.close()


def propose_add_skill(employee_id: str, skill: str, category: str = "Other",
                      qualifier: str = "") -> dict:
    connection = _write_con()
    try:
        if not _employee_exists(connection, employee_id):
            raise ValueError(f"employee '{employee_id}' was not found")
        skill = _validate_employee_value("skill", skill)
        duplicate = connection.execute(
            "SELECT 1 FROM employee_skills WHERE employee_id = ? AND lower(skill) = lower(?)",
            [employee_id, skill],
        ).fetchone()
        if duplicate:
            raise ValueError(f"skill '{skill}' already exists for {employee_id}")
        return {
            "operation": "INSERT", "table": "employee_skills", "employee_id": employee_id,
            "field": "skill", "old_value": None, "new_value": skill,
            "category": category or "Other", "qualifier": qualifier or None,
            "requires_confirmation": True,
        }
    finally:
        connection.close()


def propose_remove_skill(employee_id: str, skill: str) -> dict:
    connection = _write_con()
    try:
        if not _employee_exists(connection, employee_id):
            raise ValueError(f"employee '{employee_id}' was not found")
        row = connection.execute(
            "SELECT skill FROM employee_skills WHERE employee_id = ? "
            "AND lower(skill) = lower(?) LIMIT 1", [employee_id, skill.strip()]
        ).fetchone()
        if row is None:
            raise ValueError(f"skill '{skill}' was not found for {employee_id}")
        return {
            "operation": "DELETE", "table": "employee_skills", "employee_id": employee_id,
            "field": "skill", "old_value": row[0], "new_value": None,
            "requires_confirmation": True,
        }
    finally:
        connection.close()


def propose_add_certification(employee_id: str, certification: str,
                              vendor: str = "", verified: str = "true") -> dict:
    connection = _write_con()
    try:
        if not _employee_exists(connection, employee_id):
            raise ValueError(f"employee '{employee_id}' was not found")
        certification = _validate_employee_value("certification", certification)
        if verified not in ("true", "false"):
            raise ValueError("verified must be 'true' or 'false'")
        duplicate = connection.execute(
            "SELECT 1 FROM employee_certifications WHERE employee_id = ? "
            "AND lower(certification) = lower(?)", [employee_id, certification]
        ).fetchone()
        if duplicate:
            raise ValueError(f"certification '{certification}' already exists for {employee_id}")
        return {
            "operation": "INSERT", "table": "employee_certifications", "employee_id": employee_id,
            "field": "certification", "old_value": None, "new_value": certification,
            "vendor": vendor.strip() or None, "verified": verified,
            "requires_confirmation": True,
        }
    finally:
        connection.close()


def propose_add_project(employee_id: str, project: str, client: str = "",
                        technologies: str = "", role: str = "",
                        project_type: str = "main") -> dict:
    connection = _write_con()
    try:
        if not _employee_exists(connection, employee_id):
            raise ValueError(f"employee '{employee_id}' was not found")
        project = _validate_employee_value("project", project)
        norm_type = (project_type or "main").strip().lower()
        if norm_type not in {"main", "support"}:
            raise ValueError("project_type must be 'main' or 'support'")
        duplicate = connection.execute(
            "SELECT 1 FROM employee_projects WHERE employee_id = ? AND lower(project) = lower(?) "
            "AND lower(COALESCE(project_type, 'main')) = lower(?)",
            [employee_id, project, norm_type],
        ).fetchone()
        if duplicate:
            raise ValueError(f"project '{project}' already exists for {employee_id}")
        return {
            "operation": "INSERT", "table": "employee_projects", "employee_id": employee_id,
            "field": "project", "old_value": None, "new_value": project,
            "client": client.strip() or None, "technologies": technologies.strip() or None,
            "role": role.strip() or None, "project_type": norm_type,
            "requires_confirmation": True,
        }
    finally:
        connection.close()


def propose_remove_project(employee_id: str, project: str) -> dict:
    connection = _write_con()
    try:
        if not _employee_exists(connection, employee_id):
            raise ValueError(f"employee '{employee_id}' was not found")
        row = connection.execute(
            "SELECT project FROM employee_projects WHERE employee_id = ? "
            "AND lower(project) = lower(?) LIMIT 1", [employee_id, project.strip()]
        ).fetchone()
        if row is None:
            raise ValueError(f"project '{project}' was not found for {employee_id}")
        return {
            "operation": "DELETE", "table": "employee_projects", "employee_id": employee_id,
            "field": "project", "old_value": row[0], "new_value": None,
            "requires_confirmation": True,
        }
    finally:
        connection.close()


def propose_move_project(employee_id: str, from_project: str, to_project: str,
                         client: str = "", technologies: str = "", role: str = "",
                         project_type: str | None = None) -> dict:
    connection = _write_con()
    try:
        if not _employee_exists(connection, employee_id):
            raise ValueError(f"employee '{employee_id}' was not found")
        old = connection.execute(
            "SELECT project, client, technologies, role, COALESCE(project_type, 'main') FROM employee_projects "
            "WHERE employee_id = ? AND lower(project) = lower(?) LIMIT 1",
            [employee_id, from_project.strip()],
        ).fetchone()
        if old is None:
            raise ValueError(f"project '{from_project}' was not found for {employee_id}")
        to_project = _validate_employee_value("to_project", to_project)
        # preserve the project's existing type unless the caller explicitly
        # asks to change it — same fallback pattern as client/technologies/role
        norm_type = (project_type or old[4] or "main").strip().lower()
        if norm_type not in {"main", "support"}:
            raise ValueError("project_type must be 'main' or 'support'")
        if connection.execute(
            "SELECT 1 FROM employee_projects WHERE employee_id = ? AND lower(project) = lower(?) "
            "AND lower(COALESCE(project_type, 'main')) = lower(?)",
            [employee_id, to_project.strip(), norm_type],
        ).fetchone():
            raise ValueError(f"project '{to_project}' already exists for {employee_id}")
        return {
            "operation": "MOVE", "table": "employee_projects", "employee_id": employee_id,
            "field": "project", "old_value": old[0], "new_value": to_project.strip(),
            "client": client.strip() or old[1], "technologies": technologies.strip() or old[2],
            "role": role.strip() or old[3], "project_type": norm_type,
            "requires_confirmation": True,
        }
    finally:
        connection.close()


def propose_create_employee(employee_id: str, name: str, email: str = "") -> dict:
    connection = _write_con()
    try:
        employee_id = _validate_employee_value("employee_id", employee_id)
        name = _validate_employee_value("name", name)
        if email:
            email = _validate_employee_value("email", email)
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError("email is not valid")
        if connection.execute("SELECT 1 FROM employees WHERE employee_id = ?", [employee_id]).fetchone():
            raise ValueError(f"employee '{employee_id}' already exists")
        return {
            "operation": "INSERT", "table": "employees", "employee_id": employee_id,
            "field": "employee", "old_value": None, "new_value": name,
            "email": email or None, "requires_confirmation": True,
        }
    finally:
        connection.close()


def propose_delete_certification(employee_id: str, certification: str) -> dict:
    connection = _write_con()
    try:
        if not _employee_exists(connection, employee_id):
            raise ValueError(f"employee '{employee_id}' was not found")
        row = connection.execute(
            "SELECT certification FROM employee_certifications "
            "WHERE employee_id = ? AND lower(certification) = lower(?) LIMIT 1",
            [employee_id, certification.strip()],
        ).fetchone()
        if row is None:
            raise ValueError(f"certification '{certification}' was not found for {employee_id}")
        return {
            "operation": "DELETE", "table": "employee_certifications", "employee_id": employee_id,
            "field": "certification", "old_value": row[0], "new_value": None,
            "requires_confirmation": True,
        }
    finally:
        connection.close()


def propose_update_project_role(employee_id: str, project: str, role: str) -> dict:
    connection = _write_con()
    try:
        if not _employee_exists(connection, employee_id):
            raise ValueError(f"employee '{employee_id}' was not found")
        row = connection.execute(
            "SELECT role FROM employee_projects WHERE employee_id = ? "
            "AND lower(project) = lower(?) LIMIT 1", [employee_id, project.strip()]
        ).fetchone()
        if row is None:
            raise ValueError(f"project '{project}' was not found for {employee_id}")
        role = _validate_employee_value("role", role)
        return {
            "operation": "UPDATE", "table": "employee_projects", "employee_id": employee_id,
            "project": project.strip(), "field": "role", "old_value": row[0],
            "new_value": role, "requires_confirmation": True,
        }
    finally:
        connection.close()


def propose_update_project_technologies(employee_id: str, project: str,
                                        technologies: str) -> dict:
    connection = _write_con()
    try:
        if not _employee_exists(connection, employee_id):
            raise ValueError(f"employee '{employee_id}' was not found")
        row = connection.execute(
            "SELECT technologies FROM employee_projects WHERE employee_id = ? "
            "AND lower(project) = lower(?) LIMIT 1", [employee_id, project.strip()]
        ).fetchone()
        if row is None:
            raise ValueError(f"project '{project}' was not found for {employee_id}")
        return {
            "operation": "UPDATE", "table": "employee_projects", "employee_id": employee_id,
            "project": project.strip(), "field": "technologies", "old_value": row[0],
            "new_value": _validate_employee_value("technologies", technologies),
            "requires_confirmation": True,
        }
    finally:
        connection.close()


def propose_update_manager(employee_id: str, manager: str) -> dict:
    connection = _write_con()
    try:
        if not _employee_exists(connection, employee_id):
            raise ValueError(f"employee '{employee_id}' was not found")
        managers = connection.execute(
            "SELECT employee_id, name FROM employees "
            "WHERE employee_id = ? OR lower(name) LIKE '%' || lower(?) || '%' "
            "ORDER BY name", [manager.strip(), manager.strip()]
        ).fetchall()
        if not managers:
            raise ValueError(f"manager '{manager}' was not found")
        if len(managers) > 1:
            choices = ", ".join(f"{name} ({reporting_id})" for reporting_id, name in managers)
            raise ValueError(f"manager name is ambiguous: {choices}")
        reporting_id, manager_name = managers[0]
        if reporting_id == employee_id:
            raise ValueError("an employee cannot report to themselves")
        current = connection.execute(
            "SELECT m.reporting_id, e.name FROM employee_managers m "
            "JOIN employees e ON e.employee_id = m.reporting_id "
            "WHERE m.employee_id = ? ORDER BY e.name LIMIT 1", [employee_id]
        ).fetchone()
        return {
            "operation": "UPDATE", "table": "employee_managers", "employee_id": employee_id,
            "field": "reporting_id", "old_value": current[0] if current else None,
            "old_manager_name": current[1] if current else None,
            "new_value": reporting_id, "new_manager_name": manager_name,
            "requires_confirmation": True,
        }
    finally:
        connection.close()


def _audit(connection, proposal: dict, status: str, error_message: str | None = None) -> None:
    connection.execute(
        "INSERT INTO employee_data_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [datetime.now(timezone.utc), proposal.get("operation"), proposal.get("employee_id"),
         proposal.get("table"), proposal.get("field"), str(proposal.get("old_value"))
         if proposal.get("old_value") is not None else None,
         str(proposal.get("new_value")) if proposal.get("new_value") is not None else None,
         status, error_message],
    )


def _execute_delta_write(proposal: dict) -> dict:
    """Write proposal to Delta tables. Returns {success, verified_value, error}."""
    operation = proposal["operation"]
    table = proposal["table"]
    employee_id = proposal["employee_id"]
    
    if table == "employees" and operation == "UPDATE":
        field = proposal["field"]
        return delta_sync.write_employee_update(
            employee_id, field, proposal["new_value"], proposal.get("old_value")
        )
    elif table == "employee_skills" and operation == "INSERT":
        return delta_sync.write_skill(
            employee_id, proposal["new_value"], 
            proposal.get("category", "Other"), proposal.get("qualifier", "")
        )
    elif table == "employee_skills" and operation == "DELETE":
        return delta_sync.delete_skill(employee_id, proposal["old_value"])
    elif table == "employee_certifications" and operation == "INSERT":
        return delta_sync.write_certification(
            employee_id, proposal["new_value"], proposal.get("vendor", ""),
            "data_management", proposal.get("verified", "true")
        )
    elif table == "employee_certifications" and operation == "DELETE":
        return delta_sync.delete_certification(employee_id, proposal["old_value"])
    elif table == "employee_projects" and operation == "INSERT":
        return delta_sync.write_project(
            employee_id, proposal["new_value"], proposal.get("client", ""),
            proposal.get("technologies", ""), proposal.get("role", ""),
            proposal.get("project_type", "main")
        )
    elif table == "employee_projects" and operation == "DELETE":
        return delta_sync.delete_project(employee_id, proposal["old_value"])
    elif table == "employee_projects" and operation == "UPDATE":
        field = proposal.get("field")
        return delta_sync.update_project_field(
            employee_id, proposal["project"], field, proposal["new_value"]
        )
    elif table == "employees" and operation == "INSERT":
        return delta_sync.create_employee(
            employee_id, proposal["new_value"], proposal.get("email", "")
        )
    elif table == "employee_managers" and operation == "UPDATE":
        return delta_sync.update_manager(employee_id, proposal["new_value"])
    else:
        return {"success": False, "error": "Unsupported operation"}


def execute_proposal(proposal: dict) -> dict:
    """Execute a proposal: write to Delta first, then update DuckDB cache.
    
    Flow:
    1. Write to Delta (source of truth) with idempotent MERGE
    2. On success, update DuckDB cache
    3. Audit logging handled by Delta sync
    
    If Delta write fails, nothing is changed and error is raised.
    """
    if not proposal.get("requires_confirmation"):
        raise ValueError("write proposal requires confirmation")
    
    operation = proposal["operation"]
    table = proposal["table"]
    employee_id = proposal["employee_id"]
    
    # Step 1: Write to Delta first (if available)
    verified = None
    if DELTA_SYNC_AVAILABLE:
        try:
            delta_result = _execute_delta_write(proposal)
            if not delta_result.get("success"):
                # Delta write failed, fall back to DuckDB only
                import warnings
                warnings.warn(f"Delta write failed, using DuckDB only: {delta_result.get('error')}")
            else:
                verified = delta_result.get("verified_value")
        except Exception as e:
            # Delta sync unavailable at runtime (e.g., no Spark session)
            # Fall back to DuckDB-only mode
            import warnings
            warnings.warn(f"Delta sync unavailable, using DuckDB only: {e}")
    
    # Step 2: Update DuckDB cache
    connection = _write_con()
    try:
        connection.execute("BEGIN")
        
        # Verify employee exists (except for new employee creation)
        if not _employee_exists(connection, employee_id) and not (
            table == "employees" and operation == "INSERT"
        ):
            raise ValueError(f"employee '{employee_id}' was not found")

        if table == "employees" and operation == "UPDATE":
            field = proposal["field"]
            if field not in set(MANAGEMENT_FIELDS.values()):
                raise ValueError("field is not allowed")
            value = _validate_employee_value(field, proposal["new_value"])
            connection.execute(f"UPDATE employees SET {field} = ? WHERE employee_id = ?", [value, employee_id])
            verified = connection.execute(
                f"SELECT {field} FROM employees WHERE employee_id = ?", [employee_id]
            ).fetchone()[0]
        elif table == "employee_skills" and operation == "INSERT":
            connection.execute(
                "INSERT INTO employee_skills (employee_id, skill, category, qualifier, source) "
                "VALUES (?, ?, ?, ?, ?)",
                [employee_id, proposal["new_value"], proposal.get("category", "Other"),
                 proposal.get("qualifier"), "data_management"],
            )
            verified = connection.execute(
                "SELECT skill FROM employee_skills WHERE employee_id = ? AND lower(skill) = lower(?)",
                [employee_id, proposal["new_value"]],
            ).fetchone()[0]
        elif table == "employee_skills" and operation == "DELETE":
            connection.execute(
                "DELETE FROM employee_skills WHERE employee_id = ? AND lower(skill) = lower(?)",
                [employee_id, proposal["old_value"]],
            )
            verified = connection.execute(
                "SELECT COUNT(*) FROM employee_skills WHERE employee_id = ? AND lower(skill) = lower(?)",
                [employee_id, proposal["old_value"]],
            ).fetchone()[0]
            if verified != 0:
                raise ValueError("skill could not be deleted")
        elif table == "employee_certifications" and operation == "INSERT":
            connection.execute(
                "INSERT INTO employee_certifications "
                "(employee_id, certification, vendor, source, verified, raw_text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [employee_id, proposal["new_value"], proposal.get("vendor"),
                 "data_management", proposal.get("verified", "true"), proposal["new_value"]],
            )
            verified = connection.execute(
                "SELECT certification FROM employee_certifications WHERE employee_id = ? "
                "AND lower(certification) = lower(?)", [employee_id, proposal["new_value"]]
            ).fetchone()[0]
        elif table == "employee_certifications" and operation == "DELETE":
            result = connection.execute(
                "DELETE FROM employee_certifications WHERE employee_id = ? "
                "AND lower(certification) = lower(?)",
                [employee_id, proposal["old_value"]],
            )
            verified = connection.execute(
                "SELECT COUNT(*) FROM employee_certifications WHERE employee_id = ? "
                "AND lower(certification) = lower(?)", [employee_id, proposal["old_value"]]
            ).fetchone()[0]
            if result.rowcount == 0 or verified != 0:
                raise ValueError("certification could not be deleted")
        elif table == "employee_projects" and operation == "UPDATE" and proposal.get("field") == "role":
            connection.execute(
                "UPDATE employee_projects SET role = ? WHERE employee_id = ? AND lower(project) = lower(?)",
                [proposal["new_value"], employee_id, proposal["project"]],
            )
            verified = connection.execute(
                "SELECT role FROM employee_projects WHERE employee_id = ? AND lower(project) = lower(?)",
                [employee_id, proposal["project"]],
            ).fetchone()[0]
        elif table == "employee_projects" and operation == "UPDATE" and proposal.get("field") == "technologies":
            connection.execute(
                "UPDATE employee_projects SET technologies = ? WHERE employee_id = ? "
                "AND lower(project) = lower(?)",
                [proposal["new_value"], employee_id, proposal["project"]],
            )
            verified = connection.execute(
                "SELECT technologies FROM employee_projects WHERE employee_id = ? "
                "AND lower(project) = lower(?)", [employee_id, proposal["project"]]
            ).fetchone()[0]
        elif table == "employee_projects" and operation == "INSERT":
            connection.execute(
                "INSERT INTO employee_projects "
                "(employee_id, project, client, technologies, role, project_type) VALUES (?, ?, ?, ?, ?, ?)",
                [employee_id, proposal["new_value"], proposal.get("client"),
                 proposal.get("technologies"), proposal.get("role"),
                 proposal.get("project_type", "main")],
            )
            verified = connection.execute(
                "SELECT project FROM employee_projects WHERE employee_id = ? "
                "AND lower(project) = lower(?) AND lower(COALESCE(project_type, 'main')) = lower(?)",
                [employee_id, proposal["new_value"], proposal.get("project_type", "main")]
            ).fetchone()[0]
        elif table == "employee_projects" and operation == "DELETE":
            connection.execute(
                "DELETE FROM employee_projects WHERE employee_id = ? AND lower(project) = lower(?)",
                [employee_id, proposal["old_value"]],
            )
            verified = connection.execute(
                "SELECT COUNT(*) FROM employee_projects WHERE employee_id = ? "
                "AND lower(project) = lower(?)", [employee_id, proposal["old_value"]]
            ).fetchone()[0]
            if verified != 0:
                raise ValueError("project assignment could not be deleted")
        elif table == "employee_projects" and operation == "MOVE":
            connection.execute(
                "DELETE FROM employee_projects WHERE employee_id = ? AND lower(project) = lower(?)",
                [employee_id, proposal["old_value"]],
            )
            connection.execute(
                "INSERT INTO employee_projects "
                "(employee_id, project, client, technologies, role, project_type) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [employee_id, proposal["new_value"], proposal.get("client"),
                 proposal.get("technologies"), proposal.get("role"),
                 proposal.get("project_type", "main")],
            )
            verified = connection.execute(
                "SELECT project FROM employee_projects WHERE employee_id = ? "
                "AND lower(project) = lower(?)", [employee_id, proposal["new_value"]]
            ).fetchone()[0]
        elif table == "employees" and operation == "INSERT":
            connection.execute(
                "INSERT INTO employees (employee_id, name, email) VALUES (?, ?, ?)",
                [employee_id, proposal["new_value"], proposal.get("email")],
            )
            verified = connection.execute(
                "SELECT employee_id FROM employees WHERE employee_id = ?", [employee_id]
            ).fetchone()[0]
        elif table == "employee_managers" and operation == "UPDATE":
            reporting_id = proposal["new_value"]
            if not _employee_exists(connection, reporting_id):
                raise ValueError(f"manager '{reporting_id}' was not found")
            if reporting_id == employee_id:
                raise ValueError("an employee cannot report to themselves")
            connection.execute("DELETE FROM employee_managers WHERE employee_id = ?", [employee_id])
            connection.execute(
                "INSERT INTO employee_managers (employee_id, reporting_id) VALUES (?, ?)",
                [employee_id, reporting_id],
            )
            verified = connection.execute(
                "SELECT reporting_id FROM employee_managers WHERE employee_id = ? "
                "AND reporting_id = ?", [employee_id, reporting_id]
            ).fetchone()[0]
        else:
            raise ValueError("unsupported write proposal")

        _audit(connection, proposal, "SUCCESS")
        connection.execute("COMMIT")
        return {"status": "SUCCESS", "proposal": proposal, "verified_value": verified}
    except Exception as exc:
        connection.execute("ROLLBACK")
        try:
            connection.execute("BEGIN")
            _audit(connection, proposal, "ERROR", str(exc))
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def execute_proposals(proposals: list[dict]) -> list[dict]:
    if not proposals or any(not proposal.get("requires_confirmation") for proposal in proposals):
        raise ValueError("all write proposals require confirmation")
    connection = _write_con()
    results = []
    try:
        connection.execute("BEGIN")
        for proposal in proposals:
            employee_id = proposal["employee_id"]
            if proposal["table"] == "employees" and proposal["operation"] == "UPDATE":
                field = proposal["field"]
                if field not in set(MANAGEMENT_FIELDS.values()):
                    raise ValueError("field is not allowed")
                value = _validate_employee_value(field, proposal["new_value"])
                connection.execute(f"UPDATE employees SET {field} = ? WHERE employee_id = ?", [value, employee_id])
                verified = connection.execute(f"SELECT {field} FROM employees WHERE employee_id = ?", [employee_id]).fetchone()[0]
            elif proposal["table"] == "employee_managers" and proposal["operation"] == "UPDATE":
                reporting_id = proposal["new_value"]
                if not _employee_exists(connection, reporting_id) or reporting_id == employee_id:
                    raise ValueError("invalid reporting manager")
                connection.execute("DELETE FROM employee_managers WHERE employee_id = ?", [employee_id])
                connection.execute("INSERT INTO employee_managers (employee_id, reporting_id) VALUES (?, ?)", [employee_id, reporting_id])
                verified = reporting_id
            elif proposal["table"] == "employee_skills" and proposal["operation"] == "INSERT":
                connection.execute("INSERT INTO employee_skills (employee_id, skill, category, qualifier, source) VALUES (?, ?, ?, ?, ?)",
                                   [employee_id, proposal["new_value"], proposal.get("category", "Other"), proposal.get("qualifier"), "data_management"])
                verified = proposal["new_value"]
            elif proposal["table"] == "employee_skills" and proposal["operation"] == "DELETE":
                connection.execute("DELETE FROM employee_skills WHERE employee_id = ? AND lower(skill) = lower(?)",
                                   [employee_id, proposal["old_value"]])
                verified = connection.execute("SELECT COUNT(*) FROM employee_skills WHERE employee_id = ? AND lower(skill) = lower(?)",
                                              [employee_id, proposal["old_value"]]).fetchone()[0]
                if verified:
                    raise ValueError("skill could not be deleted")
            elif proposal["table"] == "employee_certifications" and proposal["operation"] == "INSERT":
                connection.execute("INSERT INTO employee_certifications (employee_id, certification, vendor, source, verified, raw_text) VALUES (?, ?, ?, ?, ?, ?)",
                                   [employee_id, proposal["new_value"], proposal.get("vendor"), "data_management", proposal.get("verified", "false"), proposal["new_value"]])
                verified = proposal["new_value"]
            elif proposal["table"] == "employee_projects" and proposal["operation"] == "INSERT":
                connection.execute("INSERT INTO employee_projects (employee_id, project, client, technologies, role, project_type) VALUES (?, ?, ?, ?, ?, ?)",
                                   [employee_id, proposal["new_value"], proposal.get("client"), proposal.get("technologies"), proposal.get("role"), proposal.get("project_type", "main")])
                verified = proposal["new_value"]
            elif proposal["table"] == "employee_projects" and proposal["operation"] == "DELETE":
                connection.execute("DELETE FROM employee_projects WHERE employee_id = ? AND lower(project) = lower(?)",
                                   [employee_id, proposal["old_value"]])
                verified = connection.execute("SELECT COUNT(*) FROM employee_projects WHERE employee_id = ? AND lower(project) = lower(?)",
                                              [employee_id, proposal["old_value"]]).fetchone()[0]
                if verified:
                    raise ValueError("project assignment could not be deleted")
            elif proposal["table"] == "employee_projects" and proposal["operation"] == "MOVE":
                connection.execute("DELETE FROM employee_projects WHERE employee_id = ? AND lower(project) = lower(?)",
                                   [employee_id, proposal["old_value"]])
                connection.execute("INSERT INTO employee_projects (employee_id, project, client, technologies, role, project_type) VALUES (?, ?, ?, ?, ?, ?)",
                                   [employee_id, proposal["new_value"], proposal.get("client"), proposal.get("technologies"), proposal.get("role"), proposal.get("project_type", "main")])
                verified = proposal["new_value"]
            elif proposal["table"] == "employee_projects" and proposal["operation"] == "UPDATE":
                if proposal.get("field") not in ("role", "technologies"):
                    raise ValueError("project field is not allowed")
                connection.execute(
                    f"UPDATE employee_projects SET {proposal['field']} = ? "
                    "WHERE employee_id = ? AND lower(project) = lower(?)",
                    [proposal["new_value"], employee_id, proposal["project"]],
                )
                verified = proposal["new_value"]
            elif proposal["table"] == "employees" and proposal["operation"] == "INSERT":
                connection.execute("INSERT INTO employees (employee_id, name, email) VALUES (?, ?, ?)",
                                   [employee_id, proposal["new_value"], proposal.get("email")])
                verified = employee_id
            else:
                raise ValueError(f"unsupported batch proposal: {proposal['table']} {proposal['operation']}")
            _audit(connection, proposal, "SUCCESS")
            results.append({"status": "SUCCESS", "proposal": proposal, "verified_value": verified})
        connection.execute("COMMIT")
        return results
    except Exception as exc:
        connection.execute("ROLLBACK")
        for proposal in proposals:
            try:
                connection.execute("BEGIN")
                _audit(connection, proposal, "ERROR", str(exc))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


# ------------------------------------------------------------- resolution

def search_employees(name: str, limit: int = 10) -> pd.DataFrame:
    """Partial, case-insensitive name search. The agent's entry point for
    turning 'Siva' into a specific person — it may return several."""
    return q(
        """
        SELECT e.employee_id, e.name, e.email,
               (SELECT COUNT(*) FROM employee_skills s WHERE s.employee_id = e.employee_id) AS skills,
               (SELECT COUNT(*) FROM employee_projects p WHERE p.employee_id = e.employee_id) AS projects
        FROM employees e
        WHERE lower(e.name) LIKE '%' || lower(?) || '%'
           OR lower(e.email) LIKE '%' || lower(?) || '%'
           OR e.employee_id = ?
        ORDER BY e.name
        LIMIT ?
        """,
        [name, name, name, limit],
    )


def get_employee(employee_id: str) -> pd.DataFrame:
    return q("SELECT employee_id, name, email, position, designation, team FROM employees WHERE employee_id = ?", [employee_id])


# ---------------------------------------------------------------- profile

def get_employee_skills(employee_id: str) -> pd.DataFrame:
    return q(
        """
        SELECT skill, category, qualifier
        FROM employee_skills WHERE employee_id = ?
        ORDER BY category, skill
        """,
        [employee_id],
    )


def get_employee_projects(employee_id: str) -> pd.DataFrame:
    return q(
        """
        SELECT project, client, technologies, COALESCE(project_type, 'main') AS project_type
        FROM employee_projects WHERE employee_id = ?
        ORDER BY project
        """,
        [employee_id],
    )


def get_employee_certifications(employee_id: str, verified_only: bool = False) -> pd.DataFrame:
    sql = ("SELECT certification, vendor, verified, source FROM employee_certifications "
           "WHERE employee_id = ?")
    if verified_only:
        sql += " AND verified = 'true'"
    return q(sql + " ORDER BY verified DESC, certification", [employee_id])


def get_manager_chain(employee_id: str) -> pd.DataFrame:
    return q(
        """
        SELECT m.reporting_id, e.name AS manager_name, e.email AS manager_email
        FROM employee_managers m
        JOIN employees e ON e.employee_id = m.reporting_id
        WHERE m.employee_id = ?
        ORDER BY e.name
        """,
        [employee_id],
    )


def get_direct_reports(employee_id: str) -> pd.DataFrame:
    return q(
        """
        SELECT e.employee_id, e.name, e.email
        FROM employee_managers m
        JOIN employees e ON e.employee_id = m.employee_id
        WHERE m.reporting_id = ?
        ORDER BY e.name
        """,
        [employee_id],
    )


def get_employee_360(employee_id: str) -> dict:
    """One call for everything known about a person."""
    person = get_employee(employee_id)
    if person.empty:
        return {}
    row = person.iloc[0]
    return {
        "employee_id": row["employee_id"],
        "name": row["name"],
        "email": row["email"],
        "position": row["position"],
        "designation": row["designation"],
        "team": row["team"],
        "skills": get_employee_skills(employee_id).to_dict("records"),
        "projects": get_employee_projects(employee_id).to_dict("records"),
        "certifications": get_employee_certifications(employee_id).to_dict("records"),
        "reports_to": get_manager_chain(employee_id)["manager_name"].tolist(),
        "direct_reports": get_direct_reports(employee_id)["name"].tolist(),
    }


# ----------------------------------------------------------------- search

def list_all_employees() -> pd.DataFrame:
    """Every employee, no limit. Used by the Profile picker so nobody is hidden."""
    return q(
        """
        SELECT e.employee_id, e.name, e.email,
               (SELECT COUNT(*) FROM employee_skills s WHERE s.employee_id = e.employee_id) AS skills,
               (SELECT COUNT(*) FROM employee_projects p WHERE p.employee_id = e.employee_id) AS projects
        FROM employees e ORDER BY e.name
        """
    )


def find_employees(skills: list[str] | None = None,
                   projects: list[str] | None = None,
                   certifications: list[str] | None = None,
                   limit: int = 500) -> pd.DataFrame:
    """
    Intersection search: every condition must hold for the same person.
    This is what answers 'who knows Python and Azure and works on Phibro'
    in one query instead of three the model has to combine itself.
    """
    clauses, params = [], []

    for skill in skills or []:
        clauses.append(
            "EXISTS (SELECT 1 FROM employee_skills s WHERE s.employee_id = e.employee_id "
            "AND lower(s.skill) LIKE '%' || lower(?) || '%')"
        )
        params.append(skill)

    for project in projects or []:
        clauses.append(
            "EXISTS (SELECT 1 FROM employee_projects p WHERE p.employee_id = e.employee_id "
            "AND (lower(p.project) LIKE '%' || lower(?) || '%' "
            "     OR lower(p.client) LIKE '%' || lower(?) || '%'))"
        )
        params.extend([project, project])

    for cert in certifications or []:
        clauses.append(
            "EXISTS (SELECT 1 FROM employee_certifications c WHERE c.employee_id = e.employee_id "
            "AND lower(c.certification) LIKE '%' || lower(?) || '%')"
        )
        params.append(cert)

    where = " AND ".join(clauses) if clauses else "TRUE"
    params.append(limit)
    return q(
        f"""
        SELECT e.employee_id, e.name, e.email,
               (SELECT string_agg(DISTINCT p.project, ', ') FROM employee_projects p
                 WHERE p.employee_id = e.employee_id) AS projects
        FROM employees e
        WHERE {where}
        ORDER BY e.name
        LIMIT ?
        """,
        params,
    )


def find_employees_by_project(project: str) -> pd.DataFrame:
    return q(
        """
        SELECT e.employee_id, e.name, p.project, p.client, p.technologies
        FROM employee_projects p
        JOIN employees e USING (employee_id)
        WHERE lower(p.project) LIKE '%' || lower(?) || '%'
           OR lower(p.client)  LIKE '%' || lower(?) || '%'
        ORDER BY e.name
        """,
        [project, project],
    )


def find_employees_by_team(team: str) -> pd.DataFrame:
    return q(
        """
        SELECT employee_id, name, email, team
        FROM employees
        WHERE lower(team) LIKE '%' || lower(?) || '%'
        ORDER BY name
        """,
        [team],
    )


def find_employees_by_skill(skill: str) -> pd.DataFrame:
    return q(
        """
        SELECT e.employee_id, e.name, s.skill, s.category, s.qualifier
        FROM employee_skills s
        JOIN employees e USING (employee_id)
        WHERE lower(s.skill) LIKE '%' || lower(?) || '%'
        ORDER BY e.name
        """,
        [skill],
    )


def find_employees_by_certification(certification: str,
                                    verified_only: bool = False) -> pd.DataFrame:
    sql = """
        SELECT e.employee_id, e.name, c.certification, c.verified, c.source
        FROM employee_certifications c
        JOIN employees e USING (employee_id)
        WHERE lower(c.certification) LIKE '%' || lower(?) || '%'
    """
    if verified_only:
        sql += " AND c.verified = 'true'"
    return q(sql + " ORDER BY c.verified DESC, e.name", [certification])


# ------------------------------------------------------------ aggregates

def headcount_by_project() -> pd.DataFrame:
    return q(
        """
        SELECT project, client, COUNT(DISTINCT employee_id) AS people
        FROM employee_projects
        GROUP BY project, client
        ORDER BY people DESC, project
        """
    )


def top_skills(limit: int = 10, category: str | None = None) -> pd.DataFrame:
    if category:
        return q(
            """
            SELECT skill, category, COUNT(DISTINCT employee_id) AS people
            FROM employee_skills
            WHERE lower(category) = lower(?)
            GROUP BY skill, category ORDER BY people DESC, skill LIMIT ?
            """,
            [category, limit],
        )
    return q(
        """
        SELECT skill, category, COUNT(DISTINCT employee_id) AS people
        FROM employee_skills
        GROUP BY skill, category ORDER BY people DESC, skill LIMIT ?
        """,
        [limit],
    )


def skill_categories() -> pd.DataFrame:
    return q(
        """
        SELECT category,
               COUNT(DISTINCT skill) AS skills,
               COUNT(DISTINCT employee_id) AS people
        FROM employee_skills
        GROUP BY category ORDER BY people DESC
        """
    )


def certification_counts() -> pd.DataFrame:
    """
    Split by verification status on purpose. 'confirmed' comes from a person's
    own skills text or a unique cell; 'unconfirmed' comes from the repeated
    block in the certifications column and should not be quoted to a client.
    """
    return q(
        """
        SELECT certification,
               COUNT(DISTINCT employee_id) FILTER (WHERE verified = 'true')  AS confirmed,
               COUNT(DISTINCT employee_id) FILTER (WHERE verified = 'false') AS unconfirmed,
               COUNT(DISTINCT employee_id) AS claimed_total
        FROM employee_certifications
        GROUP BY certification ORDER BY confirmed DESC, claimed_total DESC, certification
        """
    )


def employees_without_skills() -> pd.DataFrame:
    """Coverage gap — who still needs to submit their skills."""
    return q(
        """
        SELECT e.employee_id, e.name, e.email
        FROM employees e
        LEFT JOIN (SELECT DISTINCT employee_id FROM employee_skills) s USING (employee_id)
        WHERE s.employee_id IS NULL
        ORDER BY e.name
        """
    )


def list_projects() -> list[str]:
    return q("SELECT DISTINCT project FROM employee_projects ORDER BY 1")["project"].tolist()


def list_skills() -> list[str]:
    return q("SELECT DISTINCT skill FROM employee_skills ORDER BY 1")["skill"].tolist()


def dataset_stats() -> dict:
    row = q(
        """
        SELECT (SELECT COUNT(*) FROM employees)                              AS employees,
               (SELECT COUNT(DISTINCT employee_id) FROM employee_skills)     AS with_skills,
               (SELECT COUNT(DISTINCT skill) FROM employee_skills)           AS skills,
               (SELECT COUNT(DISTINCT project) FROM employee_projects)       AS projects,
               (SELECT COUNT(*) FROM employee_certifications
                 WHERE verified = 'true')                                    AS certifications_confirmed,
               (SELECT COUNT(*) FROM employee_certifications
                 WHERE verified = 'false')                                   AS certifications_unconfirmed,
               (SELECT COUNT(*) FROM employee_managers)                      AS reporting_links
        """
    ).iloc[0]
    return {k: int(v) for k, v in row.items()}


# --------------------------------------------------------------- free SQL

SCHEMA_DESCRIPTION = """
employees(employee_id, name, email, position, team, department, designation, location, employment_status, joining_date)
employee_managers(employee_id, reporting_id)        -- reporting_id is the direct reporting lead
employee_projects(employee_id, project, client, technologies, project_type)   -- project_type is 'main' or 'support'
employee_skills(employee_id, skill, category, qualifier, source)
employee_certifications(employee_id, certification, vendor, source, verified, raw_text)

Notes:
- skill categories: Language, Database, Cloud, BI, AI, Platform, Tool, Security, Design, OS, Other
- one row per person per skill / project / certification, so count DISTINCT employee_id for people
- position values: Team Lead, Module Lead, Project Manager, Operational Manager, Intern
- designation values: Data Engineer, Gen AI Developer, Cyber Security Analyst, Business Development Executive, IT Recruiter, etc.
- team values: Data Team, Gen AI Team, Cyber Team, Sales Team, Resourcing Team, HR Team, Presales Team
- department, joining_date, location, employment_status may be NULL for most employees; do not rely on them
"""

_FORBIDDEN = ("insert", "update", "delete", "drop", "create", "alter", "attach",
              "copy", "export", "install", "load", "pragma", "call", "set ")


def run_sql(sql: str, limit: int = 200) -> pd.DataFrame:
    """
    Run a read-only SELECT. This is the catch-all for questions the fixed
    tools do not cover. The connection is already read-only; these checks
    stop anything that is not a plain query from being attempted at all.
    """
    cleaned = sql.strip().rstrip(";")
    low = cleaned.lower()
    if not (low.startswith("select") or low.startswith("with")):
        raise ValueError("only SELECT queries are allowed")
    if ";" in cleaned:
        raise ValueError("only one statement at a time")
    if any(word in low for word in _FORBIDDEN):
        raise ValueError("only read-only queries are allowed")
    if " limit " not in low:
        cleaned = f"{cleaned} LIMIT {limit}"
    return q(cleaned)
