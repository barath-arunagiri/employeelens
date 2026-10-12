"""
Employeelens — data access layer.

Every SQL statement in the project lives here. The agent and the tools
call these functions and never see the database, All data lives in Unity Catalog Delta tables.
All queries use the Databricks SDK statement execution API.

All queries are parameterised. Nothing here interpolates a caller's
string into SQL.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pandas as pd

from src.database import uc_connection as uc


def q(sql: str, params: list | None = None) -> pd.DataFrame:
    return uc.query(sql, params)


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


def ensure_management_schema() -> None:
    """No-op — Unity Catalog tables already have all columns."""
    pass


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
        clauses.append("lower(change_type) = lower(?)")
        params.append(operation.strip())
    if status:
        clauses.append("lower(status) = lower(?)")
        params.append(status.strip())
    limit = max(1, min(int(limit), 100))
    where = " AND ".join(clauses) if clauses else "TRUE"
    return q(
        f"SELECT * FROM change_audit_log WHERE {where} "
        "ORDER BY confirmed_at DESC LIMIT ?", params + [limit]
    )


def _employee_exists(employee_id: str) -> bool:
    # fetchone returns None when no row matches; uc.query would return an
    # empty DataFrame, which is never None — that made this check always True.
    return uc.fetchone(
        "SELECT 1 FROM employees WHERE employee_id = ?", [employee_id]
    ) is not None


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
    row = uc.fetchone(
        f"SELECT {column} FROM employees WHERE employee_id = ?", [employee_id]
    )
    if row is None:
        raise ValueError(f"employee '{employee_id}' was not found")
    value = _validate_employee_value(field, new_value)
    return {
        "operation": "UPDATE", "table": "employees", "employee_id": employee_id,
        "field": column, "old_value": row[0], "new_value": value,
        "requires_confirmation": True,
    }


# Categories actually used in employee_skills (verified against live data
# 2026-10-12). The agent must classify into one of these — never invent
# values like 'Technical'.
VALID_SKILL_CATEGORIES = [
    "Language", "Database", "AI", "Tool", "Platform",
    "Security", "Design", "BI", "Cloud", "OS", "Other",
]
_VALID_CATEGORIES_LOWER = {c.lower(): c for c in VALID_SKILL_CATEGORIES}


def _validate_skill_category(category: str | None) -> str:
    """Accept a real category (case-insensitive); never invent one."""
    value = (category or "").strip()
    if not value:
        return "Other"
    canonical = _VALID_CATEGORIES_LOWER.get(value.lower())
    if canonical:
        return canonical
    raise ValueError(
        f"category '{value}' is not used in this data. Valid categories: "
        + ", ".join(VALID_SKILL_CATEGORIES)
    )


def propose_add_skill(employee_id: str, skill: str, category: str = "Other",
                      qualifier: str = "") -> dict:
    if not _employee_exists(employee_id):
        raise ValueError(f"employee '{employee_id}' was not found")
    skill = _validate_employee_value("skill", skill)
    duplicate = uc.fetchone(
        "SELECT 1 FROM employee_skills WHERE employee_id = ? AND lower(skill) = lower(?)",
        [employee_id, skill],
    )
    if duplicate:
        raise ValueError(f"skill '{skill}' already exists for {employee_id}")
    return {
        "operation": "INSERT", "table": "employee_skills", "employee_id": employee_id,
        "field": "skill", "old_value": None, "new_value": skill,
        "category": _validate_skill_category(category), "qualifier": qualifier or None,
        "requires_confirmation": True,
    }


def propose_remove_skill(employee_id: str, skill: str) -> dict:
    if not _employee_exists(employee_id):
        raise ValueError(f"employee '{employee_id}' was not found")
    row = uc.fetchone(
        "SELECT skill FROM employee_skills WHERE employee_id = ? "
        "AND lower(skill) = lower(?) LIMIT 1", [employee_id, skill.strip()]
    )
    if row is None:
        raise ValueError(f"skill '{skill}' was not found for {employee_id}")
    return {
        "operation": "DELETE", "table": "employee_skills", "employee_id": employee_id,
        "field": "skill", "old_value": row[0], "new_value": None,
        "requires_confirmation": True,
    }


def propose_add_certification(employee_id: str, certification: str,
                              vendor: str = "", verified: str = "true") -> dict:
    if not _employee_exists(employee_id):
        raise ValueError(f"employee '{employee_id}' was not found")
    certification = _validate_employee_value("certification", certification)
    if verified not in ("true", "false"):
        raise ValueError("verified must be 'true' or 'false'")
    duplicate = uc.fetchone(
        "SELECT 1 FROM employee_certifications WHERE employee_id = ? "
        "AND lower(certification) = lower(?)", [employee_id, certification]
    )
    if duplicate:
        raise ValueError(f"certification '{certification}' already exists for {employee_id}")
    return {
        "operation": "INSERT", "table": "employee_certifications", "employee_id": employee_id,
        "field": "certification", "old_value": None, "new_value": certification,
        "vendor": vendor.strip() or None, "verified": verified,
        "requires_confirmation": True,
    }


def propose_add_project(employee_id: str, project: str, client: str = "",
                        technologies: str = "", role: str = "",
                        project_type: str = "main") -> dict:
    if not _employee_exists(employee_id):
        raise ValueError(f"employee '{employee_id}' was not found")
        project = _validate_employee_value("project", project)
        norm_type = (project_type or "main").strip().lower()
    if norm_type not in {"main", "support"}:
        raise ValueError("project_type must be 'main' or 'support'")
        duplicate = uc.fetchone(
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


def propose_remove_project(employee_id: str, project: str) -> dict:
    if not _employee_exists(employee_id):
        raise ValueError(f"employee '{employee_id}' was not found")
    row = uc.fetchone(
        "SELECT project FROM employee_projects WHERE employee_id = ? "
        "AND lower(project) = lower(?) LIMIT 1",
        [employee_id, project.strip()]
    )
    if row is None:
        raise ValueError(f"project '{project}' was not found for {employee_id}")
    return {
        "operation": "DELETE", "table": "employee_projects", "employee_id": employee_id,
        "field": "project", "old_value": row[0], "new_value": None,
        "requires_confirmation": True,
    }


def propose_move_project(employee_id: str, from_project: str, to_project: str,
                         client: str = "", technologies: str = "", role: str = "",
                         project_type: str | None = None) -> dict:
    if not _employee_exists(employee_id):
        raise ValueError(f"employee '{employee_id}' was not found")
    old = uc.fetchone(
        "SELECT project, client, technologies, role, COALESCE(project_type, 'main') FROM employee_projects "
            "WHERE employee_id = ? AND lower(project) = lower(?) LIMIT 1",
            [employee_id, from_project.strip()],
        )
    if old is None:
        raise ValueError(f"project '{from_project}' was not found for {employee_id}")
        to_project = _validate_employee_value("to_project", to_project)
        # preserve the project's existing type unless the caller explicitly
        # asks to change it — same fallback pattern as client/technologies/role
        norm_type = (project_type or old[4] or "main").strip().lower()
    if norm_type not in {"main", "support"}:
        raise ValueError("project_type must be 'main' or 'support'")
    if uc.fetchone(
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


def propose_create_employee(employee_id: str, name: str, email: str = "") -> dict:
    employee_id = _validate_employee_value("employee_id", employee_id)
    name = _validate_employee_value("name", name)
    if email:
        email = _validate_employee_value("email", email)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("email is not valid")
    if uc.fetchone("SELECT 1 FROM employees WHERE employee_id = ?",
        [employee_id]
    ):
        raise ValueError(f"employee '{employee_id}' already exists")
    return {
        "operation": "INSERT", "table": "employees", "employee_id": employee_id,
        "field": "employee", "old_value": None, "new_value": name,
        "email": email or None, "requires_confirmation": True,
    }


def propose_delete_employee(employee_id: str) -> dict:
    """Propose permanently deleting an employee and ALL linked records.

    The app's only cascade delete: removes the employee row plus every
    skill, project assignment, certification and both directions of the
    manager links. Irreversible — the Data Management UI must confirm.
    """
    employee_id = _validate_employee_value("employee_id", employee_id)
    if not _employee_exists(employee_id):
        raise ValueError(f"employee '{employee_id}' was not found")

    def _count(sql: str, params: list) -> int:
        return uc.fetchval(sql, params) or 0

    name = uc.fetchval("SELECT name FROM employees WHERE employee_id = ?", [employee_id])
    counts = {
        "skills": _count("SELECT COUNT(*) FROM employee_skills WHERE employee_id = ?", [employee_id]),
        "projects": _count("SELECT COUNT(*) FROM employee_projects WHERE employee_id = ?", [employee_id]),
        "certifications": _count(
            "SELECT COUNT(*) FROM employee_certifications WHERE employee_id = ?", [employee_id]),
        "manager_links": _count(
            "SELECT COUNT(*) FROM employee_managers "
            "WHERE employee_id = ? OR reporting_id = ?", [employee_id, employee_id]),
    }
    return {
        "operation": "DELETE", "table": "employees", "employee_id": employee_id,
        "field": "employee", "old_value": name, "new_value": None,
        "summary": counts,
        "warning": ("Permanently deletes the employee and all linked skills, "
                    "projects, certifications and manager links."),
        "requires_confirmation": True,
    }


def propose_delete_certification(employee_id: str, certification: str) -> dict:
    if not _employee_exists(employee_id):
        raise ValueError(f"employee '{employee_id}' was not found")
    row = uc.fetchone(
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


def propose_update_project_role(employee_id: str, project: str, role: str) -> dict:
    if not _employee_exists(employee_id):
        raise ValueError(f"employee '{employee_id}' was not found")
    row = uc.fetchone(
        "SELECT role FROM employee_projects WHERE employee_id = ? "
            "AND lower(project) = lower(?) LIMIT 1",
            [employee_id, project.strip()]
        )
    if row is None:
        raise ValueError(f"project '{project}' was not found for {employee_id}")
    role = _validate_employee_value("role", role)
    return {
            "operation": "UPDATE", "table": "employee_projects", "employee_id": employee_id,
            "project": project.strip(), "field": "role", "old_value": row[0],
            "new_value": role, "requires_confirmation": True,
        }


def propose_update_project_technologies(employee_id: str, project: str,
                                        technologies: str) -> dict:
    if not _employee_exists(employee_id):
        raise ValueError(f"employee '{employee_id}' was not found")
    row = uc.fetchone(
        "SELECT technologies FROM employee_projects WHERE employee_id = ? "
            "AND lower(project) = lower(?) LIMIT 1",
        [employee_id, project.strip()]
    )
    if row is None:
        raise ValueError(f"project '{project}' was not found for {employee_id}")
    return {
            "operation": "UPDATE", "table": "employee_projects", "employee_id": employee_id,
            "project": project.strip(), "field": "technologies", "old_value": row[0],
            "new_value": _validate_employee_value("technologies", technologies),
            "requires_confirmation": True,
        }


def propose_update_manager(employee_id: str, manager: str) -> dict:
    if not _employee_exists(employee_id):
        raise ValueError(f"employee '{employee_id}' was not found")
    managers = uc.query(
        "SELECT employee_id, name FROM employees "
        "WHERE employee_id = ? OR lower(name) LIKE '%' || lower(?) || '%' "
        "ORDER BY name",
        [manager.strip(), manager.strip()]
    ).values.tolist()
    if not managers:
        raise ValueError(f"manager '{manager}' was not found")
    if len(managers) > 1:
        choices = ", ".join(f"{name} ({reporting_id})" for reporting_id, name in managers)
        raise ValueError(f"manager name is ambiguous: {choices}")
    reporting_id, manager_name = managers[0]
    if reporting_id == employee_id:
        raise ValueError("an employee cannot report to themselves")
    current = uc.fetchone(
        "SELECT m.reporting_id, e.name FROM employee_managers m "
        "JOIN employees e ON e.employee_id = m.reporting_id "
        "WHERE m.employee_id = ? ORDER BY e.name LIMIT 1",
        [employee_id]
    )
    return {
        "operation": "UPDATE", "table": "employee_managers", "employee_id": employee_id,
        "field": "reporting_id", "old_value": current[0] if current else None,
        "old_manager_name": current[1] if current else None,
        "new_value": reporting_id, "new_manager_name": manager_name,
        "requires_confirmation": True,
    }


def _audit(proposal: dict, status: str, error_message: str | None = None) -> None:
    """Write audit entry to Unity Catalog."""
    uc.write_audit(
        operation=proposal.get("operation", ""),
    employee_id=proposal.get("employee_id", ""),
        table_name=proposal.get("table", ""),
        field_name=proposal.get("field"),
        old_value=proposal.get("old_value"),
        new_value=proposal.get("new_value"),
        status=status,
        error_message=error_message,
    )


def execute_proposal(proposal: dict) -> dict:
    """Execute a proposal: write directly to Unity Catalog Delta tables.
    
    No DuckDB, no caching — writes go straight to Delta.
    """
    if not proposal.get("requires_confirmation"):
        raise ValueError("write proposal requires confirmation")
    
    operation = proposal["operation"]
    table = proposal["table"]
    employee_id = proposal["employee_id"]
    
    # Verify employee exists (except for new employee creation)
    if not _employee_exists(employee_id) and not (
        table == "employees" and operation == "INSERT"
    ):
        raise ValueError(f"employee '{employee_id}' was not found")
    
    verified = None
    
    try:
        if table == "employees" and operation == "UPDATE":
            field = proposal["field"]
            if field not in set(MANAGEMENT_FIELDS.values()):
                raise ValueError("field is not allowed")
            value = _validate_employee_value(field, proposal["new_value"])
            uc.execute(f"UPDATE employees SET {field} = ? WHERE employee_id = ?", [value, employee_id])
            verified = uc.fetchval(f"SELECT {field} FROM employees WHERE employee_id = ?", [employee_id])
        
        elif table == "employee_skills" and operation == "INSERT":
            uc.execute(
                "INSERT INTO employee_skills (employee_id, skill, category, qualifier, source) "
                "VALUES (?, ?, ?, ?, ?)",
                [employee_id, proposal["new_value"], proposal.get("category", "Other"),
                 proposal.get("qualifier"), "data_management"],
            )
            verified = uc.fetchval(
                "SELECT skill FROM employee_skills WHERE employee_id = ? AND lower(skill) = lower(?)",
                [employee_id, proposal["new_value"]],
            )
        
        elif table == "employee_skills" and operation == "DELETE":
            uc.execute(
                "DELETE FROM employee_skills WHERE employee_id = ? AND lower(skill) = lower(?)",
                [employee_id, proposal["old_value"]],
            )
            count = int(uc.fetchval(
                "SELECT COUNT(*) FROM employee_skills WHERE employee_id = ? AND lower(skill) = lower(?)",
                [employee_id, proposal["old_value"]],
            ) or 0)  # fetchval returns strings: "0" would be truthy and != 0
            if count != 0:
                raise ValueError("skill could not be deleted")
            verified = 0
        
        elif table == "employee_certifications" and operation == "INSERT":
            verified_value = proposal.get("verified", "true")
            verified_bool = verified_value in (True, "true", "True", 1)
            uc.execute(
                "INSERT INTO employee_certifications "
                "(employee_id, certification, vendor, source, verified, raw_text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [employee_id, proposal["new_value"], proposal.get("vendor"),
                 "data_management", verified_bool, proposal["new_value"]],
            )
            verified = uc.fetchval(
                "SELECT certification FROM employee_certifications WHERE employee_id = ? "
                "AND lower(certification) = lower(?)",
                [employee_id, proposal["new_value"]],
            )
        
        elif table == "employee_certifications" and operation == "DELETE":
            uc.execute(
                "DELETE FROM employee_certifications WHERE employee_id = ? "
                "AND lower(certification) = lower(?)",
                [employee_id, proposal["old_value"]],
            )
            count = int(uc.fetchval(
                "SELECT COUNT(*) FROM employee_certifications WHERE employee_id = ? "
                "AND lower(certification) = lower(?)",
                [employee_id, proposal["old_value"]],
            ) or 0)
            if count != 0:
                raise ValueError("certification could not be deleted")
            verified = 0
        
        elif table == "employee_projects" and operation == "UPDATE" and proposal.get("field") == "role":
            uc.execute(
                "UPDATE employee_projects SET role = ? WHERE employee_id = ? AND lower(project) = lower(?)",
                [proposal["new_value"], employee_id, proposal["project"]],
            )
            verified = uc.fetchval(
                "SELECT role FROM employee_projects WHERE employee_id = ? AND lower(project) = lower(?)",
                [employee_id, proposal["project"]],
            )
        
        elif table == "employee_projects" and operation == "UPDATE" and proposal.get("field") == "technologies":
            uc.execute(
                "UPDATE employee_projects SET technologies = ? WHERE employee_id = ? "
                "AND lower(project) = lower(?)",
                [proposal["new_value"], employee_id, proposal["project"]],
            )
            verified = uc.fetchval(
                "SELECT technologies FROM employee_projects WHERE employee_id = ? "
                "AND lower(project) = lower(?)",
                [employee_id, proposal["project"]],
            )
        
        elif table == "employee_projects" and operation == "INSERT":
            uc.execute(
                "INSERT INTO employee_projects "
                "(employee_id, project, client, technologies, role, project_type) VALUES (?, ?, ?, ?, ?, ?)",
                [employee_id, proposal["new_value"], proposal.get("client"),
                 proposal.get("technologies"), proposal.get("role"),
                 proposal.get("project_type", "main")],
            )
            verified = uc.fetchval(
                "SELECT project FROM employee_projects WHERE employee_id = ? "
                "AND lower(project) = lower(?) AND lower(COALESCE(project_type, 'main')) = lower(?)",
                [employee_id, proposal["new_value"], proposal.get("project_type", "main")],
            )
        
        elif table == "employee_projects" and operation == "DELETE":
            uc.execute(
                "DELETE FROM employee_projects WHERE employee_id = ? AND lower(project) = lower(?)",
                [employee_id, proposal["old_value"]],
            )
            count = int(uc.fetchval(
                "SELECT COUNT(*) FROM employee_projects WHERE employee_id = ? "
                "AND lower(project) = lower(?)",
                [employee_id, proposal["old_value"]],
            ) or 0)
            if count != 0:
                raise ValueError("project assignment could not be deleted")
            verified = 0
        
        elif table == "employee_projects" and operation == "MOVE":
            uc.execute(
                "DELETE FROM employee_projects WHERE employee_id = ? AND lower(project) = lower(?)",
                [employee_id, proposal["old_value"]],
            )
            uc.execute(
                "INSERT INTO employee_projects "
                "(employee_id, project, client, technologies, role, project_type) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [employee_id, proposal["new_value"], proposal.get("client"),
                 proposal.get("technologies"), proposal.get("role"),
                 proposal.get("project_type", "main")],
            )
            verified = uc.fetchval(
                "SELECT project FROM employee_projects WHERE employee_id = ? "
                "AND lower(project) = lower(?)",
                [employee_id, proposal["new_value"]],
            )
        
        elif table == "employees" and operation == "INSERT":
            uc.execute(
                "INSERT INTO employees (employee_id, name, email) VALUES (?, ?, ?)",
                [employee_id, proposal["new_value"], proposal.get("email")],
            )
            verified = uc.fetchval("SELECT employee_id FROM employees WHERE employee_id = ?", [employee_id])
        
        elif table == "employees" and operation == "DELETE":
            # Cascade delete: every linked record goes with the employee.
            uc.execute("DELETE FROM employee_skills WHERE employee_id = ?", [employee_id])
            uc.execute("DELETE FROM employee_projects WHERE employee_id = ?", [employee_id])
            uc.execute("DELETE FROM employee_certifications WHERE employee_id = ?", [employee_id])
            uc.execute(
                "DELETE FROM employee_managers WHERE employee_id = ? OR reporting_id = ?",
                [employee_id, employee_id],
            )
            uc.execute("DELETE FROM employees WHERE employee_id = ?", [employee_id])
            count = int(uc.fetchval("SELECT COUNT(*) FROM employees WHERE employee_id = ?", [employee_id]) or 0)
            if count != 0:
                raise ValueError("employee could not be deleted")
            verified = 0
        
        elif table == "employee_managers" and operation == "UPDATE":
            reporting_id = proposal["new_value"]
            if not _employee_exists(reporting_id):
                raise ValueError(f"manager '{reporting_id}' was not found")
            if reporting_id == employee_id:
                raise ValueError("an employee cannot report to themselves")
            uc.execute("DELETE FROM employee_managers WHERE employee_id = ?", [employee_id])
            uc.execute(
                "INSERT INTO employee_managers (employee_id, reporting_id) VALUES (?, ?)",
                [employee_id, reporting_id],
            )
            verified = uc.fetchval(
                "SELECT reporting_id FROM employee_managers WHERE employee_id = ? AND reporting_id = ?",
                [employee_id, reporting_id],
            )
        
        else:
            raise ValueError("unsupported write proposal")
        
        _audit(proposal, "SUCCESS")
        return {"status": "SUCCESS", "proposal": proposal, "verified_value": verified}
    
    except Exception as exc:
        _audit(proposal, "ERROR", str(exc))
        raise


def execute_proposals(proposals: list[dict]) -> list[dict]:
    """Execute multiple proposals sequentially via Unity Catalog."""
    if not proposals or any(not p.get("requires_confirmation") for p in proposals):
        raise ValueError("all write proposals require confirmation")
    
    results = []
    for proposal in proposals:
        try:
            results.append(execute_proposal(proposal))
        except Exception as exc:  # noqa: BLE001
            # One failed item (e.g. 'already exists') must not abort the
            # rest of the batch — record it and continue.
            results.append(
                {"status": "ERROR", "proposal": proposal, "error": str(exc)}
            )
    return results


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
    """One call for everything known about a person — in ONE SQL round trip.

    The previous version fired 6 sequential statements (one per section),
    each with its own SDK client and token exchange, which made a cold
    profile selection take minutes.
    """
    row = q(
        """
        SELECT e.employee_id, e.name, e.email, e.position, e.designation, e.team,
               (SELECT to_json(collect_list(struct(s.skill AS skill, s.category AS category, s.qualifier AS qualifier)))
                  FROM employee_skills s WHERE s.employee_id = e.employee_id)   AS skills_json,
               (SELECT to_json(collect_list(struct(p.project AS project, p.client AS client,
                        p.technologies AS technologies, COALESCE(p.project_type, 'main') AS project_type)))
                  FROM employee_projects p WHERE p.employee_id = e.employee_id) AS projects_json,
               (SELECT to_json(collect_list(struct(c.certification AS certification, c.vendor AS vendor,
                        c.verified AS verified, c.source AS source)))
                  FROM employee_certifications c WHERE c.employee_id = e.employee_id) AS certifications_json,
               (SELECT collect_list(e2.name)
                  FROM employee_managers m JOIN employees e2 ON e2.employee_id = m.reporting_id
                 WHERE m.employee_id = e.employee_id)                AS reports_to,
               (SELECT collect_list(e3.name)
                  FROM employee_managers m2 JOIN employees e3 ON e3.employee_id = m2.employee_id
                 WHERE m2.reporting_id = e.employee_id)              AS direct_reports
        FROM employees e WHERE e.employee_id = ?
        """,
        [employee_id],
    )
    if row.empty:
        return {}
    r = row.iloc[0]

    def _records(col):
        raw = r[col]
        if raw is None or raw == "" or raw == "[]":
            return []
        if isinstance(raw, str):
            return json.loads(raw)
        return list(raw)

    def _names(col):
        raw = r[col]
        if raw is None or raw == "" or raw == "[]":
            return []
        if isinstance(raw, str):
            return [str(x) for x in json.loads(raw)]
        return [str(x) for x in raw]

    return {
        "employee_id": r["employee_id"],
        "name": r["name"],
        "email": r["email"],
        "position": r["position"],
        "designation": r["designation"],
        "team": r["team"],
        "skills": sorted(_records("skills_json"),
                         key=lambda s: (s.get("category") or "", s.get("skill") or "")),
        "projects": sorted(_records("projects_json"),
                           key=lambda p: p.get("project") or ""),
        "certifications": sorted(_records("certifications_json"),
                                 key=lambda c: (str(c.get("verified")).lower() != "true",
                                                c.get("certification") or "")),
        "reports_to": sorted(_names("reports_to")),
        "direct_reports": sorted(_names("direct_reports")),
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
        SELECT (SELECT COUNT(*) FROM employees)                              AS total_employees,
               (SELECT COUNT(DISTINCT employee_id) FROM employee_skills)     AS employees_with_skills,
               (SELECT COUNT(DISTINCT skill) FROM employee_skills)           AS total_skills,
               (SELECT COUNT(DISTINCT project) FROM employee_projects)       AS total_projects,
               (SELECT COUNT(*) FROM employee_certifications
                 WHERE verified = true OR verified = 'true')                 AS certifications_confirmed,
               (SELECT COUNT(*) FROM employee_certifications
                 WHERE verified = false OR verified = 'false')               AS certifications_unconfirmed,
               (SELECT COUNT(*) FROM employee_managers)                      AS total_reporting_links
        """
    ).iloc[0]
    # Map new column names to expected keys for backwards compatibility
    return {
        'employees': int(row['total_employees']),
        'with_skills': int(row['employees_with_skills']),
        'skills': int(row['total_skills']),
        'projects': int(row['total_projects']),
        'certifications_confirmed': int(row['certifications_confirmed']),
        'certifications_unconfirmed': int(row['certifications_unconfirmed']),
        'reporting_links': int(row['total_reporting_links'])
    }


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
