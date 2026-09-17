"""
Employeelens — stage 2: load the clean CSVs into DuckDB.

Rebuilds the database from scratch every run, so it is always a pure
function of data/clean/. Never edit the database by hand: fix the
reference CSVs, re-run normalize, re-run this.

Run:  python -m src.ingestion.load_duckdb
"""
from __future__ import annotations

import duckdb

from src.config import CLEAN_DIR, DB_PATH

SCHEMA = """
DROP TABLE IF EXISTS employee_certifications;
DROP TABLE IF EXISTS employee_skills;
DROP TABLE IF EXISTS employee_projects;
DROP TABLE IF EXISTS employee_managers;
DROP TABLE IF EXISTS employees;
DROP TABLE IF EXISTS employee_data_audit;

CREATE TABLE employees (
    employee_id VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL,
    email       VARCHAR,
    position    VARCHAR,
    team        VARCHAR,
    department  VARCHAR,
    designation VARCHAR,
    location    VARCHAR,
    employment_status VARCHAR,
    joining_date VARCHAR
);

CREATE TABLE employee_managers (
    employee_id  VARCHAR NOT NULL,
    reporting_id VARCHAR NOT NULL
);

CREATE TABLE employee_projects (
    employee_id  VARCHAR NOT NULL,
    project      VARCHAR NOT NULL,
    client       VARCHAR,
    technologies VARCHAR,
    role         VARCHAR,
    project_type VARCHAR DEFAULT 'main'
);

CREATE TABLE employee_skills (
    employee_id VARCHAR NOT NULL,
    skill       VARCHAR NOT NULL,
    category    VARCHAR,
    qualifier   VARCHAR,
    source      VARCHAR
);

CREATE TABLE employee_certifications (
    employee_id   VARCHAR NOT NULL,
    certification VARCHAR NOT NULL,
    vendor        VARCHAR,
    source        VARCHAR,   -- skills_text | certifications_column
    verified      VARCHAR,   -- 'false' when it came from a repeated fill-down block
    raw_text      VARCHAR
);

CREATE TABLE employee_data_audit (
    timestamp     TIMESTAMP NOT NULL,
    operation     VARCHAR NOT NULL,
    employee_id   VARCHAR,
    table_name    VARCHAR NOT NULL,
    field_name    VARCHAR,
    old_value     VARCHAR,
    new_value     VARCHAR,
    status        VARCHAR NOT NULL,
    error_message VARCHAR
);
"""

LOADS = [
    ("employees", "employees.csv"),
    ("employee_managers", "employee_managers.csv"),
    ("employee_projects", "employee_projects.csv"),
    ("employee_skills", "employee_skills.csv"),
    ("employee_certifications", "employee_certifications.csv"),
]

# every one of these must return zero rows or the load is not trustworthy
INTEGRITY_CHECKS = {
    "skills pointing at an unknown employee":
        "SELECT COUNT(*) FROM employee_skills s "
        "LEFT JOIN employees e USING (employee_id) WHERE e.employee_id IS NULL",
    "projects pointing at an unknown employee":
        "SELECT COUNT(*) FROM employee_projects p "
        "LEFT JOIN employees e USING (employee_id) WHERE e.employee_id IS NULL",
    "certifications pointing at an unknown employee":
        "SELECT COUNT(*) FROM employee_certifications c "
        "LEFT JOIN employees e USING (employee_id) WHERE e.employee_id IS NULL",
    "reporting links pointing at an unknown reporting person":
        "SELECT COUNT(*) FROM employee_managers m "
        "LEFT JOIN employees e ON m.reporting_id = e.employee_id WHERE e.employee_id IS NULL",
    "people reporting to themselves":
        "SELECT COUNT(*) FROM employee_managers WHERE employee_id = reporting_id",
    "duplicate skill rows":
        "SELECT COUNT(*) FROM (SELECT employee_id, skill FROM employee_skills "
        "GROUP BY 1, 2 HAVING COUNT(*) > 1)",
}


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA)

    print("Employeelens — load into DuckDB")
    print("-" * 46)
    for table, filename in LOADS:
        path = CLEAN_DIR / filename
        if not path.exists():
            print(f"{table:<26}: MISSING {filename} — run normalize first")
            continue
        source = f"read_csv_auto('{path}', header=true, all_varchar=true)"
        columns = {
            "employees": "employee_id, name, email, position, designation, team",
            "employee_projects": "employee_id, project, client, technologies, project_type",
        }.get(table)
        if columns:
            con.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {source}")
        else:
            con.execute(f"INSERT INTO {table} SELECT * FROM {source}")
        count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:<26}: {count} rows")

    print()
    print("Integrity checks")
    print("-" * 46)
    failed = 0
    for label, sql in INTEGRITY_CHECKS.items():
        bad = con.execute(sql).fetchone()[0]
        status = "ok" if bad == 0 else f"FAIL ({bad})"
        if bad:
            failed += 1
        print(f"{label:<48}{status}")

    print()
    coverage = con.execute("""
        SELECT COUNT(*) FILTER (WHERE s.employee_id IS NOT NULL) AS with_skills,
               COUNT(*) AS total
        FROM employees e
        LEFT JOIN (SELECT DISTINCT employee_id FROM employee_skills) s USING (employee_id)
    """).fetchone()
    print(f"Skill coverage: {coverage[0]} of {coverage[1]} employees "
          f"({coverage[0] * 100 // coverage[1]}%)")
    certs = con.execute(
        "SELECT verified, COUNT(*) FROM employee_certifications GROUP BY 1 ORDER BY 1"
    ).fetchall()
    for flag, n in certs:
        label = "verified" if flag == "true" else "UNVERIFIED (fill-down block)"
        print(f"Certifications {label}: {n}")
    print(f"Database: {DB_PATH}")
    if failed:
        print(f"\n{failed} integrity check(s) failed — fix before using the agent.")
    con.close()


if __name__ == "__main__":
    main()
