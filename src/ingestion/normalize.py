"""
Employeelens — stage 1: clean and resolve.

Reads the two raw HR spreadsheets and writes normalised relational CSVs
to data/clean/. Deterministic: same input always gives the same output.

Nothing here guesses. When a name, manager or skill cannot be resolved
from the reference crosswalks, the row is written to a report in
data/reports/ for a human to decide, and left out of the clean data.
That is the whole point of this stage — a wrong automatic match puts
one person's skills on another person's profile.

Run:  python -m src.ingestion.normalize
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

import pandas as pd

from src.config import (
    CLEAN_DIR,
    TRUST_CERTIFICATIONS,
    DETAILS_XLSX,
    OCCUPATIONALS_XLSX,
    REFERENCE_DIR,
    REPORTS_DIR,
    VALID_EMAIL_DOMAIN,
)

# ---------------------------------------------------------------- helpers

def norm_key(value) -> str:
    """Lowercase letters only — for matching names across the two sheets."""
    return re.sub(r"[^a-z]", "", str(value).lower())


def clean_text(value) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def title_name(value) -> str:
    """Fix ALL CAPS and stray spacing without mangling names like McX."""
    name = clean_text(value)
    if not name:
        return ""
    if name.isupper() or name.islower():
        name = name.title()
    return name


def load_reference(filename: str) -> pd.DataFrame:
    path = REFERENCE_DIR / filename
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, dtype=str).fillna("")
    return df


def write_report(name: str, rows: list[dict]) -> None:
    path = REPORTS_DIR / name
    if rows:
        pd.DataFrame(rows).to_csv(path, index=False)
    elif path.exists():
        path.unlink()


# ------------------------------------------------------- employee master

def build_employees() -> tuple[pd.DataFrame, list[dict], pd.DataFrame]:
    raw = pd.read_excel(DETAILS_XLSX, dtype=str)
    raw.columns = [c.strip().lower() for c in raw.columns]
    email_corrections = load_reference("email_corrections.csv")
    corrected_emails = {
        clean_text(row["employee_id"]): clean_text(row["email"]).lower()
        for _, row in email_corrections.iterrows()
        if row.get("employee_id") and row.get("email")
    }
    existing_path = CLEAN_DIR / "employees.csv"
    existing = pd.read_csv(existing_path, dtype=str).fillna("") if existing_path.exists() else pd.DataFrame()
    existing_by_id = (existing.set_index("employee_id").to_dict("index")
                      if "employee_id" in existing.columns else {})

    records, manager_links, issues = [], [], []
    seen: dict[str, dict] = {}

    for i, row in raw.iterrows():
        emp_id = clean_text(row.get("employee id"))
        name = title_name(row.get("employee names"))
        email = clean_text(row.get("mail id")).lower()
        email = corrected_emails.get(emp_id, email)
        previous = existing_by_id.get(emp_id, {})
        position = clean_text(row.get("position")) or clean_text(previous.get("position"))
        designation = (clean_text(row.get("designation")) or
                   clean_text(row.get("desigination")) or
                   clean_text(previous.get("designation")))
        team = clean_text(row.get("team")) or clean_text(previous.get("team"))
        raw_manager = clean_text(row.get("reporting person"))

        if not emp_id or not name:
            issues.append({"row": i + 2, "issue": "missing employee id or name",
                           "value": f"{emp_id} / {name}"})
            continue

        if email and not email.endswith(VALID_EMAIL_DOMAIN):
            issues.append({"row": i + 2, "issue": "email domain is not the company domain",
                           "value": email})
        if len(emp_id) != 7:
            issues.append({"row": i + 2, "issue": "employee id is not 7 digits",
                           "value": emp_id})

        if emp_id in seen:
            prev = seen[emp_id]
            if prev["raw_manager"] != raw_manager:
                issues.append({"row": i + 2,
                               "issue": "duplicate employee id with a different manager",
                               "value": f"{emp_id} {name}: '{prev['raw_manager']}' vs '{raw_manager}'"})
            else:
                issues.append({"row": i + 2, "issue": "duplicate employee id",
                               "value": f"{emp_id} {name}"})
        else:
            seen[emp_id] = {"raw_manager": raw_manager}
            records.append({"employee_id": emp_id, "name": name, "email": email,
                            "position": position, "designation": designation, "team": team})

        # every spelling of a manager is kept, including duplicate rows,
        # because two rows for one person often means two real managers
        for part in re.split(r"[/,]", raw_manager):
            part = clean_text(part)
            if part:
                manager_links.append({"employee_id": emp_id, "raw_manager": part})

    employees = pd.DataFrame(records).drop_duplicates("employee_id")
    return employees, issues, pd.DataFrame(manager_links).drop_duplicates()


def resolve_managers(links: pd.DataFrame, employees: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    crosswalk = load_reference("manager_crosswalk.csv")
    mapping, external = {}, set()
    if not crosswalk.empty:
        for _, r in crosswalk.iterrows():
            key = norm_key(r["raw_manager"])
            if r.get("employee_id") in set(employees["employee_id"]):
                mapping[key] = r["employee_id"]
            elif not r.get("employee_id"):
                external.add(key)

    # fall back to an exact name match against the employee master
    by_name = {norm_key(r["name"]): r["employee_id"] for _, r in employees.iterrows()}

    resolved, unresolved = [], []
    for _, r in links.iterrows():
        key = norm_key(r["raw_manager"])
        reporting_id = mapping.get(key) or by_name.get(key)
        if reporting_id:
            resolved.append({"employee_id": r["employee_id"], "reporting_id": reporting_id})
        elif key in external:
            resolved.append({"employee_id": r["employee_id"], "reporting_id": ""})
        else:
            candidates = difflib.get_close_matches(key, list(by_name), n=2, cutoff=0.75)
            unresolved.append({
                "raw_manager": r["raw_manager"],
                "for_employee": r["employee_id"],
                "suggested_employee_id": by_name[candidates[0]] if candidates else "",
                "suggested_name": next((n for n in employees["name"]
                                        if norm_key(n) == candidates[0]), "") if candidates else "",
            })

    out = pd.DataFrame(resolved)
    if not out.empty:
        out = out[out["reporting_id"] != ""].drop_duplicates()
    return out, unresolved


# ---------------------------------------------------- occupationals sheet

CATEGORY_HINTS = [
    (r"cert", "Certification"),
    (r"program|languag|front\s*end|back\s*end|mobile app", "Language"),
    (r"data\s*base|db", "Database"),
    (r"visuali|bi tool|bi\b", "BI"),
    (r"cloud|server", "Cloud"),
    (r"ai framework|ai platform|technical skill", "AI"),
    (r"version control", "Tool"),
    (r"security|firewall|networking", "Security"),
    (r"operating system", "OS"),
    (r"design|graphic|interface|wireframe", "Design"),
    (r"tool|platform|crm|erp|cms|labs", "Tool"),
]

# bare category words that appear as their own token once a cell is split;
# they are labels, not skills
NOISE = {"", "-", "na", "n/a", "none", "etc", "basics", "application", "data engineer",
         "designing", "desinging", "cloud", "database", "databases", "tools", "tool",
         "visualization", "visulization", "programming", "programming language",
         "programming languages", "language", "languages", "platform", "platforms",
         "technical skills", "certification", "certifications", "1", "2", "3", "4", "5"}


def category_from_label(label: str) -> str:
    low = label.lower()
    for pattern, category in CATEGORY_HINTS:
        if re.search(pattern, low):
            return category
    return ""


def split_segments(blob: str) -> list[tuple[str, str]]:
    """
    The SKILLS cells look like:
        'Language : SQL,Pyspark\\nDatabase : My SQL, Sybase'
    and sometimes use runs of spaces instead of newlines, or a bare label
    on its own line. Returns (label, values) pairs; label is '' when the
    segment has no prefix, and values is '' for a bare label.
    """
    text = str(blob).replace("\\n", "\n")
    text = re.sub(r"\s{3,}", "\n", text)                       # long space runs are line breaks
    text = re.sub(r"\s*-->\s*", " : ", text)                   # 'Frontend --> HTML'
    text = re.sub(r"^\s*\d+\.\s*", "", text, flags=re.MULTILINE)

    segments = []
    for line in text.split("\n"):
        line = clean_text(line)
        if not line:
            continue
        # colon needs no surrounding space: 'Cloud platform :AzureDatabricks'
        parts = re.split(r"\s*:\s*", line, maxsplit=1)
        if len(parts) == 2 and len(parts[0]) < 45:
            segments.append((parts[0], parts[1]))
            continue
        # hyphen only splits when spaced, or when the left side reads like a
        # label. Protects 'scikit-learn' from becoming 'scikit' + 'learn'.
        m = re.match(r"^(.{2,45}?)\s*[-–]\s*(.+)$", line)
        if m and (" " in m.group(1) or category_from_label(m.group(1))):
            segments.append((m.group(1), m.group(2)))
            continue
        segments.append(("", line))
    return segments


def parse_tokens(values: str) -> list[tuple[str, str]]:
    """
    Split a list of skills into (token, qualifier) pairs.
    Handles commas, semicolons, ampersands, and the unbalanced brackets
    that appear once a comma splits 'Front End ( HTML, CSS)' in half.
    '(Basics)' becomes a qualifier rather than part of the name.
    """
    out = []
    for chunk in re.split(r"[,;]", values):
        qualifier = ""
        bracket = re.search(r"\(([^)]*)\)", chunk)
        if bracket:
            inner = clean_text(bracket.group(1))
            if re.search(r"basic|beginner|cover", inner, re.I):
                qualifier = inner
                chunk = re.sub(r"\([^)]*\)", "", chunk)
            else:
                chunk = chunk.replace("(", "&").replace(")", " ")
        chunk = chunk.replace("(", "&").replace(")", " ")
        for token in re.split(r"\s*&\s*|\s+and\s+", chunk):
            token = clean_text(token).strip(".&-")
            if not token or token.lower() in NOISE or len(token) < 2:
                continue
            out.append((token, qualifier))
    return out


def match_skill(token: str, alias_map: dict) -> list[tuple[str, str]]:
    """
    Resolve a token to canonical skills. Tries the whole token first, then
    falls back to greedy longest-match over its words, which is what rescues
    space-separated lists like 'Python SQL' or 'Azure fundamentals Databricks'.
    """
    hit = alias_map.get(norm_key(token))
    if hit:
        return [hit]

    words = token.split()
    if len(words) < 2:
        return []

    found, i = [], 0
    while i < len(words):
        for span in range(min(4, len(words) - i), 0, -1):
            candidate = " ".join(words[i:i + span])
            hit = alias_map.get(norm_key(candidate))
            if hit:
                found.append(hit)
                i += span
                break
        else:
            i += 1
    return found


# a certification cell shared by more than this many people is almost
# certainly a fill-down rather than a fact about each of them
FILLDOWN_THRESHOLD = 6


def certification_column(raw: pd.DataFrame) -> tuple[str, set[str]]:
    """
    Find the certifications column whatever it is spelled as ('Cerifications'
    in the current export) and work out which cell values look copy-pasted.
    """
    column = next((c for c in raw.columns if c.startswith("ceri") or c.startswith("certi")), "")
    if not column:
        return "", set()
    if TRUST_CERTIFICATIONS:
        return column, set()
    counts = raw[column].dropna().astype(str).str.strip().value_counts()
    suspect = {value for value, n in counts.items()
               if n >= FILLDOWN_THRESHOLD and len(re.split(r"\\n|\n", value)) > 1}
    return column, suspect


def build_occupationals(employees: pd.DataFrame):
    raw = pd.read_excel(OCCUPATIONALS_XLSX, dtype=str)
    raw.columns = [c.strip().lower() for c in raw.columns]
    cert_column, filldown_values = certification_column(raw)

    crosswalk = load_reference("name_crosswalk.csv")
    name_map = {}
    if not crosswalk.empty:
        for _, r in crosswalk.iterrows():
            if r.get("employee_id"):
                name_map[norm_key(r["raw_name"])] = r["employee_id"]

    by_name = {norm_key(r["name"]): r["employee_id"] for _, r in employees.iterrows()}
    valid_ids = set(employees["employee_id"])

    skill_alias = load_reference("skill_aliases.csv")
    alias_map = {norm_key(r["alias"]): (r["canonical_skill"], r["category"])
                 for _, r in skill_alias.iterrows()} if not skill_alias.empty else {}

    cert_alias = load_reference("cert_aliases.csv")
    cert_map = {norm_key(r["alias"]): (r["canonical_certification"], r["vendor"])
                for _, r in cert_alias.iterrows()} if not cert_alias.empty else {}

    project_alias = load_reference("project_aliases.csv")
    project_map = {norm_key(r["alias"]): (r["canonical_project"], r["client"])
                   for _, r in project_alias.iterrows()} if not project_alias.empty else {}

    emp_skills, emp_projects, emp_certs = [], [], []
    unresolved_names, unknown_skills, unknown_projects = [], [], []

    for _, row in raw.iterrows():
        raw_name = clean_text(row.get("name"))
        key = norm_key(raw_name)
        source_id = clean_text(row.get("employee id"))
        emp_id = (source_id if source_id in valid_ids else "") or name_map.get(key) or by_name.get(key)

        if not emp_id or emp_id not in valid_ids:
            candidates = difflib.get_close_matches(key, list(by_name), n=3, cutoff=0.6)
            unresolved_names.append({
                "raw_name": raw_name,
                "suggested_employee_id": by_name[candidates[0]] if candidates else "",
                "suggestions": " | ".join(
                    next(n for n in employees["name"] if norm_key(n) == c) for c in candidates),
            })
            continue

        # ---- projects and support projects
        last_project = ""
        for project_field, project_cell in (("main", row.get("projects")), ("support", row.get("support project"))):
            if pd.isna(project_cell):
                continue
            project_type = project_field
            for line in str(project_cell).replace("\\n", "\n").split("\n"):
                line = clean_text(line)
                if not line or line in {"-", "nan"}:
                    continue
                line = re.sub(r"\s+for\s+act\s+power$", "", line, flags=re.I)
                head, _, tail = line.partition(" - ")
                canon = project_map.get(norm_key(head))

                if canon:
                    emp_projects.append({"employee_id": emp_id, "project": canon[0],
                                         "client": canon[1], "technologies": clean_text(tail),
                                         "project_type": project_type})
                    last_project = canon[0]
                else:
                    # one line may name several projects: 'DataPatron, Employee Portal'
                    pieces = [p for p in (clean_text(x) for x in line.split(",")) if p]
                    matched = [(p, project_map[norm_key(p)]) for p in pieces
                               if norm_key(p) in project_map]
                    if matched:
                        for _, hit in matched:
                            emp_projects.append({"employee_id": emp_id, "project": hit[0],
                                                 "client": hit[1], "technologies": "",
                                                 "project_type": project_type})
                        last_project = matched[-1][1][0]
                    elif last_project:
                        # a wrapped continuation of the previous line's tech list
                        tail = line
                    else:
                        unknown_projects.append({"employee_id": emp_id, "raw_project": line})

                # technologies named next to a project are real skills too
                for token, qualifier in parse_tokens(tail):
                    for skill, skill_category in match_skill(token, alias_map):
                        emp_skills.append({"employee_id": emp_id, "skill": skill,
                                           "category": skill_category, "qualifier": qualifier,
                                           "source": "project"})

        # ---- certifications column (new in the second export)
        if cert_column:
            cell = row.get(cert_column)
            if not pd.isna(cell):
                cell_text = str(cell).strip()
                suspect = cell_text in filldown_values
                for line in re.split(r"\\n|\n", cell_text):
                    line = clean_text(line)
                    if not line or line.lower() in NOISE:
                        continue
                    hit = cert_map.get(norm_key(line))
                    emp_certs.append({
                        "employee_id": emp_id,
                        "certification": hit[0] if hit else line,
                        "vendor": hit[1] if hit else "",
                        "source": "certifications_column",
                        "verified": "false" if suspect else "true",
                        "raw_text": line,
                    })

        # ---- skills and certifications
        blob = row.get("skills")
        if pd.isna(blob):
            continue
        carried = ""
        for label, values in split_segments(blob):
            category = category_from_label(label)
            # a bare label on its own line applies to the lines that follow
            if not values:
                carried = category or carried
                continue
            category = category or carried

            for token, qualifier in parse_tokens(values):
                if category == "Certification" or re.search(r"certif", token, re.I):
                    stripped = clean_text(re.sub(r"certif\w*", "", token, flags=re.I)) or token
                    hit = cert_map.get(norm_key(stripped)) or cert_map.get(norm_key(token))
                    emp_certs.append({
                        "employee_id": emp_id,
                        "certification": hit[0] if hit else stripped,
                        "vendor": hit[1] if hit else "",
                        "source": "skills_text",
                        "verified": "true",          # the person wrote it in their own skills
                        "raw_text": token,
                    })
                    continue
                hits = match_skill(token, alias_map)
                for skill, skill_category in hits:
                    emp_skills.append({"employee_id": emp_id, "skill": skill,
                                       "category": skill_category or category,
                                       "qualifier": qualifier, "source": "skills"})
                if not hits:
                    unknown_skills.append({"employee_id": emp_id, "raw_skill": token,
                                           "label": label or carried})

    skills_df = pd.DataFrame(emp_skills)
    if not skills_df.empty:
        skills_df = (skills_df.sort_values("qualifier", ascending=False)
                     .drop_duplicates(["employee_id", "skill"], keep="last")
                     .sort_values(["employee_id", "category", "skill"]))
    projects_df = pd.DataFrame(emp_projects).drop_duplicates(["employee_id", "project", "project_type"]) \
        if emp_projects else pd.DataFrame(columns=["employee_id", "project", "client", "technologies", "project_type"])
    # a human confirmation in data/reference/verified_certifications.csv always wins
    overrides = load_reference("verified_certifications.csv")
    confirmed = {(r["employee_id"], norm_key(r["certification"]))
                 for _, r in overrides.iterrows()} if not overrides.empty else set()
    for record in emp_certs:
        if (record["employee_id"], norm_key(record["certification"])) in confirmed:
            record["verified"] = "true"
            record["source"] = "human_confirmed"
    # confirmations for certs absent from the export still count
    existing = {(r["employee_id"], norm_key(r["certification"])) for r in emp_certs}
    if not overrides.empty:
        for _, r in overrides.iterrows():
            key = (r["employee_id"], norm_key(r["certification"]))
            if key not in existing and r["employee_id"] in valid_ids:
                emp_certs.append({"employee_id": r["employee_id"],
                                  "certification": r["certification"], "vendor": "",
                                  "source": "human_confirmed", "verified": "true",
                                  "raw_text": r.get("note", "")})

    cert_columns = ["employee_id", "certification", "vendor", "source", "verified", "raw_text"]
    if emp_certs:
        certs_df = (pd.DataFrame(emp_certs)
                    .sort_values("verified", ascending=False)          # 'true' sorts before 'false'
                    .drop_duplicates(["employee_id", "certification"], keep="first")
                    .sort_values(["employee_id", "certification"]))[cert_columns]
    else:
        certs_df = pd.DataFrame(columns=cert_columns)

    reports = {
        "unresolved_names.csv": unresolved_names,
        "unknown_skills.csv": sorted(unknown_skills, key=lambda r: r["raw_skill"].lower()),
        "unknown_projects.csv": unknown_projects,
        "suspect_certifications.csv": (
            certs_df[certs_df["verified"] == "false"]
            .merge(employees, on="employee_id")[["employee_id", "name", "certification"]]
            .to_dict("records") if not certs_df.empty else []
        ),
    }
    return skills_df, projects_df, certs_df, reports


# ------------------------------------------------------------------ main

def main() -> None:
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    employees, issues, manager_links = build_employees()
    managers, unresolved_managers = resolve_managers(manager_links, employees)
    skills, projects, certs, reports = build_occupationals(employees)

    employees.to_csv(CLEAN_DIR / "employees.csv", index=False)
    managers.to_csv(CLEAN_DIR / "employee_managers.csv", index=False)
    skills.to_csv(CLEAN_DIR / "employee_skills.csv", index=False)
    projects.to_csv(CLEAN_DIR / "employee_projects.csv", index=False)
    certs.to_csv(CLEAN_DIR / "employee_certifications.csv", index=False)

    write_report("data_issues.csv", issues)
    write_report("unresolved_managers.csv", unresolved_managers)
    for name, rows in reports.items():
        write_report(name, rows)

    covered = skills["employee_id"].nunique() if not skills.empty else 0
    print("Employeelens — clean and resolve")
    print("-" * 46)
    print(f"Employees loaded           : {len(employees)}")
    print(f"Reporting links resolved   : {len(managers)}")
    print(f"Employees with skills      : {covered} of {len(employees)}")
    print(f"Skill records              : {len(skills)}")
    print(f"Distinct skills            : {skills['skill'].nunique() if not skills.empty else 0}")
    print(f"Project assignments        : {len(projects)}")
    print(f"Distinct projects          : {projects['project'].nunique() if not projects.empty else 0}")
    verified = int((certs["verified"] == "true").sum()) if not certs.empty else 0
    print(f"Certification records      : {len(certs)}  ({verified} verified, "
          f"{len(certs) - verified} unverified)")
    print()
    print("Needs your review in data/reports/")
    print("-" * 46)
    print(f"Data issues                : {len(issues)}")
    print(f"Unresolved names           : {len(reports['unresolved_names.csv'])}")
    print(f"Unresolved managers        : {len(unresolved_managers)}")
    print(f"Unrecognised skill tokens  : {len(reports['unknown_skills.csv'])}")
    print(f"Unrecognised projects      : {len(reports['unknown_projects.csv'])}")
    print(f"Suspect certifications     : {len(reports['suspect_certifications.csv'])}"
          "  <- identical block repeated across many people")
    print()
    print("Fix these by editing the CSVs in data/reference/ and running this again.")


if __name__ == "__main__":
    main()
