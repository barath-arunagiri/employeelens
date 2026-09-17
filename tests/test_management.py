"""Focused tests for confirmed Data Management writes."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.database import repository as repo  # noqa: E402


def main() -> None:
    original_path = repo.DB_PATH
    with tempfile.TemporaryDirectory() as temp_dir:
        test_db = Path(temp_dir) / "peoplelens.duckdb"
        shutil.copy2(original_path, test_db)
        repo.DB_PATH = test_db
        repo.ensure_management_schema()

        employee_id = repo.list_all_employees().iloc[0]["employee_id"]
        employee = repo.get_employee(employee_id).iloc[0]

        proposal = repo.propose_employee_update(employee_id, "name", "Management Test Name")
        assert proposal["requires_confirmation"] is True
        try:
            repo.execute_proposal({**proposal, "requires_confirmation": False})
        except ValueError:
            pass
        else:
            raise AssertionError("writes must require confirmation")
        assert repo.get_employee(employee_id).iloc[0]["name"] == employee["name"]

        try:
            repo.propose_employee_update(employee_id, "not_a_field", "x")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid fields must be rejected")

        try:
            repo.propose_employee_update(employee_id, "email", "not-an-email")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid email must be rejected")

        result = repo.execute_proposal(proposal)
        assert result["verified_value"] == "Management Test Name"

        skill = "Management Test Skill"
        skill_proposal = repo.propose_add_skill(employee_id, skill, "Tool")
        repo.execute_proposal(skill_proposal)
        assert not repo.get_employee_skills(employee_id).query("skill == @skill").empty
        repo.execute_proposal(repo.propose_remove_skill(employee_id, skill))
        assert repo.get_employee_skills(employee_id).query("skill == @skill").empty

        cert = "Management Test Certification"
        repo.execute_proposal(repo.propose_add_certification(employee_id, cert, "Test Vendor"))
        assert not repo.get_employee_certifications(employee_id).query("certification == @cert").empty

        project = "Management Test Project"
        repo.execute_proposal(repo.propose_add_project(employee_id, project, role="Analyst"))
        assert not repo.get_employee_projects(employee_id).query("project == @project").empty
        repo.execute_proposal(repo.propose_update_project_role(employee_id, project, "Engineer"))
        repo.execute_proposal(repo.propose_remove_project(employee_id, project))
        assert repo.get_employee_projects(employee_id).query("project == @project").empty

        new_employee = "management-test-employee"
        repo.execute_proposal(repo.propose_create_employee(new_employee, "Created Employee", "created@example.com"))
        assert not repo.get_employee(new_employee).empty

        batch = [
            repo.propose_employee_update(new_employee, "department", "Data"),
            repo.propose_add_skill(new_employee, "Batch Skill", "Tool"),
        ]
        repo.execute_proposals(batch)
        assert repo.q("SELECT department FROM employees WHERE employee_id = ?", [new_employee]).iloc[0]["department"] == "Data"
        assert not repo.get_employee_skills(new_employee).query("skill == 'Batch Skill'").empty

        cert_row = repo.q(
            "SELECT certification FROM employee_certifications WHERE employee_id = ? LIMIT 1",
            [employee_id],
        )
        if not cert_row.empty:
            certification = cert_row.iloc[0]["certification"]
            cert_proposal = repo.propose_delete_certification(employee_id, certification)
            repo.execute_proposal(cert_proposal)
            assert repo.q(
                "SELECT 1 FROM employee_certifications WHERE employee_id = ? "
                "AND certification = ?", [employee_id, certification]
            ).empty

        try:
            repo.execute_proposal({
                "operation": "UPDATE", "table": "employees", "employee_id": employee_id,
                "field": "not_allowed", "new_value": "x", "requires_confirmation": True,
            })
        except ValueError:
            pass
        else:
            raise AssertionError("unsupported writes must fail")

        audit_count = repo.q("SELECT COUNT(*) AS n FROM employee_data_audit").iloc[0]["n"]
        assert audit_count >= 2
        print("management tests passed")

    repo.DB_PATH = original_path


if __name__ == "__main__":
    main()
