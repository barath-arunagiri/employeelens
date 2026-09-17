"""
Smoke tests for the data layer. These need no API key — they prove the
database and tools are sound before you spend a single model call.

Run:  python -m tests.test_tools
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.tools import DISPATCH, TOOLS, run_tool  # noqa: E402
from src.database import repository as repo            # noqa: E402

CASES = [
    ("dataset_stats", {}),
    ("search_employees", {"name": "Siva"}),
    ("search_employees", {"name": "zzzznotaperson"}),
    ("headcount_by_project", {}),
    ("top_skills", {"limit": 5}),
    ("top_skills", {"limit": 5, "category": "Cloud"}),
    ("skill_categories", {}),
    ("certification_counts", {}),
    ("find_employees", {"skills": ["Python", "Azure"]}),
    ("find_employees", {"skills": ["Python"], "projects": ["Phibro"]}),
    ("find_employees_by_project", {"project": "ACT Power"}),
    ("find_employees_by_team", {"team": "Data Team"}),
    ("find_employees_by_skill", {"skill": "PySpark"}),
    ("find_employees_by_certification", {"certification": "Databricks"}),
    ("employees_without_skills", {}),
]

failures = 0

print("Tool smoke tests")
print("-" * 60)

# every declared tool must have an implementation, and vice versa
declared = {t["name"] for t in TOOLS}
implemented = set(DISPATCH)
if declared != implemented:
    print(f"MISMATCH declared-only={declared - implemented} "
          f"implemented-only={implemented - declared}")
    failures += 1
else:
    print(f"{len(declared)} tools declared and implemented")

for name, args in CASES:
    out = run_tool(name, args)
    if "error" in out:
        print(f"FAIL  {name}({args}) -> {out['error']}")
        failures += 1
    else:
        print(f"ok    {name:<32} {out['row_count']:>4} row(s)")

# the ambiguity guard: a shared first name must return several candidates
siva = run_tool("search_employees", {"name": "Siva"})
if siva["row_count"] < 2:
    print("FAIL  'Siva' should be ambiguous — check the employee data loaded")
    failures += 1
else:
    print(f"ok    ambiguity guard: 'Siva' returns {siva['row_count']} candidates")

# a 360 must join cleanly for someone who has skills
with_skills = repo.find_employees(skills=["Python"]).iloc[0]
profile = repo.get_employee_360(with_skills["employee_id"])
if not profile.get("skills"):
    print("FAIL  employee 360 returned no skills for a Python developer")
    failures += 1
else:
    print(f"ok    360 for {profile['name']}: {len(profile['skills'])} skills, "
          f"{len(profile['projects'])} project(s)")

# project rows must carry a project type to distinguish main vs support work
project_type_cols = repo.q("SELECT project_type FROM employee_projects LIMIT 1")
if project_type_cols.empty:
    print("FAIL  employee_projects is missing project_type for support-vs-main project classification")
    failures += 1
else:
    print("ok    project_type column present for main/support project classification")

print("-" * 60)
print("all good" if not failures else f"{failures} failure(s)")
sys.exit(1 if failures else 0)
