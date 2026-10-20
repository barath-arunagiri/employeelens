"""
Employeelens — the tools Groq is allowed to call.

The model never writes SQL and never sees the database. It picks a tool
and arguments; this module runs the matching repository function and
returns plain JSON-safe data. Adding a capability means adding a tool
here, which is also the enforcement boundary: anything not listed is
something the agent cannot do.
"""

from __future__ import annotations

from src.database import repository as repo


TOOLS = [
    {
        "name": "search_employees",
        "description": (
            "Find people by partial name, email or employee id. ALWAYS use this first "
            "when the user names a person, because several employees may share a first "
            "name. Returns candidates with their employee_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Partial name, email or employee id",
                },
            },
            "required": ["name"],
        },
    },

    {
        "name": "get_employee_360",
        "description": (
            "Everything known about one person: skills, projects, certifications, who "
            "they report to and their direct reports. Needs an exact employee_id, so "
            "call search_employees first if you only have a name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {
                    "type": "string",
                },
            },
            "required": ["employee_id"],
        },
    },

    {
        "name": "find_employees",
        "description": (
            "The generic people-filter tool: find people by ANY combination of "
            "skills, certifications, projects, teams, positions and manager. "
            "ALL given conditions must hold for the same person ('who knows "
            "Python, works on Phibro, is in the Data Team and reports to "
            "Shyamala'). Skills, certifications, projects, teams and "
            "positions match fuzzily (case-insensitive substring — 'ACT' "
            "matches 'ACT Power'); managers match on the manager's name or "
            "employee_id. Pass project_type when the user distinguishes main "
            "vs support people on a project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "certifications": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "projects": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "teams": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "e.g. ['Data Team']",
                },
                "positions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "e.g. ['Team Lead'], fuzzy match",
                },
                "managers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Manager name or employee_id",
                },
                "project_type": {
                    "type": "string",
                    "enum": ["main", "support"],
                    "description": (
                        "Narrows the projects filter to main or support "
                        "assignments"
                    ),
                },
            },
        },
    },

    {
        "name": "find_employees_by_project",
        "description": (
            "Everyone assigned to a project, matched on project name or client name. "
            "Every assignment has a project_type of 'main' or 'support'. When the user "
            "asks who SUPPORTS a project or who the MAIN/lead people on it are, pass "
            "project_type — do not return both groups and hope the user sorts it out. "
            "Names are fuzzy: 'ACT' matches 'ACT Power'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                },
                "project_type": {
                    "type": "string",
                    "enum": ["main", "support"],
                    "description": (
                        "Optional assignment type. Use 'support' for questions like "
                        "'who is support on <project>' and 'main' for 'who is on "
                        "<project>' in the lead/primary sense. Omit only when the "
                        "user wants everyone regardless of type."
                    ),
                },
            },
            "required": ["project"],
        },
    },

    {
        "name": "find_employees_by_team",
        "description": (
            "Everyone in a given team such as Data Team, Gen AI Team, Cyber Team, "
            "Sales Team, Presales Team or Resourcing Team."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {
                    "type": "string",
                },
            },
            "required": ["team"],
        },
    },

    {
        "name": "find_employees_by_skill",
        "description": (
            "Everyone who has a skill. Matches partially, so 'SQL' also finds 'MySQL'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                },
            },
            "required": ["skill"],
        },
    },

    {
        "name": "find_employees_by_certification",
        "description": (
            "Everyone holding a certification, for example 'Databricks' or 'Data Engineer "
            "Associate'. Each row carries a 'verified' flag: 'false' means the claim came "
            "from a copy-pasted block in the source spreadsheet and is NOT reliable. Set "
            "verified_only to true when the user wants a trustworthy list, for example for "
            "a client proposal or partner-tier count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "certification": {
                    "type": "string",
                },
                "verified_only": {
                    "type": "boolean",
                    "description": (
                        "Set true when only verified certifications should be included. "
                        "Omit when both verified and unverified claims are acceptable."
                    ),
                    "default": False,
                },
            },
            "required": ["certification"],
        },
    },

    {
        "name": "headcount_by_project",
        "description": (
            "How many people are on each project. Use for "
            "'which project has the most people'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    {
        "name": "top_skills",
        "description": (
            "The most widely held skills, counted by number of people. "
            "Optionally restrict to one category such as Language, Database, "
            "Cloud, BI, AI, Security, Design or Tool. "
            "If no category is specified, return the most common skills "
            "across all categories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many skills to return. Default is 10.",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 100,
                },
                "category": {
                    "anyOf": [
                        {
                            "type": "string",
                        },
                        {
                            "type": "null",
                        },
                    ],
                    "description": (
                        "Optional skill category. Use a category such as "
                        "Language, Database, Cloud, BI, AI, Security, Design "
                        "or Tool. Use null or omit this field for all categories."
                    ),
                },
            },
        },
    },

    {
        "name": "skill_categories",
        "description": (
            "A breakdown of how many distinct skills and people sit in each "
            "skill category."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    {
        "name": "certification_counts",
        "description": (
            "Every certification across the organisation, split into 'confirmed' "
            "and 'unconfirmed' counts. Confirmed comes from a person's own skills "
            "entry; unconfirmed comes from a repeated block in the spreadsheet "
            "and is unreliable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    {
        "name": "get_direct_reports",
        "description": (
            "The people who report to a given employee_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {
                    "type": "string",
                },
            },
            "required": ["employee_id"],
        },
    },

    {
        "name": "employees_without_skills",
        "description": (
            "People with no skills recorded yet. Use for coverage questions like "
            "'whose profile is incomplete' or 'who still needs to submit their skills'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    {
        "name": "run_sql",
        "description": (
            "Run a read-only SQL SELECT against the employee database. Use this for any "
            "question the other tools do not cover — cross-tab counts, unusual filters, "
            "people who have one skill but not another, manager rollups, and so on. "
            "Prefer a specific tool when one fits; use this when none does.\n\n"
            "SCHEMA:\n"
            + __import__(
                "src.database.repository",
                fromlist=["x"],
            ).SCHEMA_DESCRIPTION
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A single SELECT statement. Databricks SQL syntax.",
                },
            },
            "required": ["sql"],
        },
    },

    {
        "name": "list_all_employees",
        "description": (
            "Every employee with their skill and project counts. "
            "No limit, nobody hidden."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    {
        "name": "dataset_stats",
        "description": (
            "Row counts for the whole dataset. Use for "
            "'how many employees do we have'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    {
        "name": "get_audit_history",
        "description": (
            "Read-only search of employee_data_audit. Use for questions about "
            "what changed, when a change happened, an employee's change history, "
            "the last changes, or failed operations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {
                    "type": "string",
                },
                "operation": {
                    "type": "string",
                    "description": "UPDATE, INSERT, or DELETE",
                },
                "status": {
                    "type": "string",
                    "description": "SUCCESS or ERROR",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum 100 rows; default 10",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
        },
    },
    {
        "name": "get_team_structure",
        "description": (
            "Read-only org view: every lead/manager with their direct "
            "reports (team members). One call for 'team lead wise members', "
            "'who reports to whom', or org-structure questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


DISPATCH = {
    "get_team_structure": lambda a: _frame(
        repo.get_team_structure()
    ),

    "search_employees": lambda a: _frame(
        repo.search_employees(a["name"])
    ),

    "get_employee_360": lambda a: repo.get_employee_360(
        a["employee_id"]
    ),

    "find_employees": lambda a: _frame(
        repo.find_employees(
            skills=a.get("skills"),
            projects=a.get("projects"),
            certifications=a.get("certifications"),
            teams=a.get("teams"),
            positions=a.get("positions"),
            managers=a.get("managers"),
            project_type=a.get("project_type"),
        )
    ),

    "find_employees_by_project": lambda a: _frame(
        repo.find_employees_by_project(
            a["project"], a.get("project_type") or None
        )
    ),

    "find_employees_by_team": lambda a: _frame(
        repo.find_employees_by_team(a["team"])
    ),

    "find_employees_by_skill": lambda a: _frame(
        repo.find_employees_by_skill(a["skill"])
    ),

    "find_employees_by_certification": lambda a: _frame(
        repo.find_employees_by_certification(
            a["certification"],
            a.get("verified_only", False),
        )
    ),

    "headcount_by_project": lambda a: _frame(
        repo.headcount_by_project()
    ),

    "top_skills": lambda a: _frame(
        repo.top_skills(
            a.get("limit", 10),
            a.get("category"),
        )
    ),

    "skill_categories": lambda a: _frame(
        repo.skill_categories()
    ),

    "certification_counts": lambda a: _frame(
        repo.certification_counts()
    ),

    "get_direct_reports": lambda a: _frame(
        repo.get_direct_reports(a["employee_id"])
    ),

    "employees_without_skills": lambda a: _frame(
        repo.employees_without_skills()
    ),

    "dataset_stats": lambda a: repo.dataset_stats(),

    "run_sql": lambda a: _frame(
        repo.run_sql(a["sql"])
    ),

    "list_all_employees": lambda a: _frame(
        repo.list_all_employees()
    ),

    "get_audit_history": lambda a: _frame(
        repo.get_audit_history(
            a.get("employee_id"),
            a.get("operation"),
            a.get("status"),
            a.get("limit", 10),
        )
    ),
}


# Phase 2 — one small toolset: the model is offered only the five general
# read tools plus the propose_* tools. The narrow one-purpose specs above
# stay implemented (dispatch and repository functions remain available to
# the UI) but are no longer exposed to the model; their per-question
# mappings moved into prompt rules and the generic find_employees.
_TOOLS_OFFERED = (
    "search_employees",
    "get_employee_360",
    "find_employees",
    "run_sql",
    "get_team_structure",
    "get_audit_history",
)
TOOLS = [t for t in TOOLS if t["name"] in _TOOLS_OFFERED]


MANAGEMENT_TOOLS = [
    {
        "name": "propose_clone_skills",
        "description": (
            "Copy skills of one employee (with their real categories) to another "
            "employee in ONE confirmed batch. Use for requests like 'give <new> "
            "the same skills as <existing>' or 'copy skills from X to Y'. If the "
            "user names specific categories ('copy only the AI and Language skills "
            "from X to Y'), pass them in categories and copy ONLY those — never "
            "silently copy every category when the user restricted to some. "
            "Skills the target already has are skipped automatically. Resolve BOTH "
            "names with search_employees first, then call this ONCE. Never writes "
            "data; Streamlit confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_employee_id": {
                    "type": "string",
                    "description": "employee whose skills are copied",
                },
                "target_employee_id": {
                    "type": "string",
                    "description": "employee who receives the skills",
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional category filter — copy ONLY the source's skills "
                        "in these categories, e.g. ['AI', 'Language']. Omit to "
                        "copy all categories. Categories are matched "
                        "case-insensitively; if the source has no skills in the "
                        "requested categories the tool lists what they DO have."
                    ),
                },
            },
            "required": ["source_employee_id", "target_employee_id"],
        },
    },

    {
        "name": "propose_employee_update",
        "description": (
            "Propose an employee field update. "
            "Never writes data; Streamlit confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "field": {"type": "string"},
                "new_value": {"type": "string"},
            },
            "required": [
                "employee_id",
                "field",
                "new_value",
            ],
        },
    },

    {
        "name": "propose_add_skill",
        "description": (
            "Propose adding ONE single skill — use this only when the user "
            "names exactly one skill. For a LIST of two or more skills for "
            "the same person, call propose_add_skills (plural, batch) once "
            "instead of this tool repeatedly. "
            "Never writes data; Streamlit confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "skill": {"type": "string"},
                "category": {
                    "type": "string",
                    "description": (
                        "One of the categories actually used in this data: "
                        "Language, Database, AI, Tool, Platform, Security, "
                        "Design, BI, Cloud, OS, Other. Classify from what the "
                        "skill IS (Python → Language, PostgreSQL → Database, "
                        "Databricks → Platform, Power BI → BI, Docker → Tool). "
                        "Never invent a category; omit only if genuinely "
                        "unclassifiable."
                    ),
                },
                "qualifier": {"type": "string"},
            },
            "required": [
                "employee_id",
                "skill",
            ],
        },
    },

    {
        "name": "propose_add_skills",
        "description": (
            "Batch-add MANY skills to ONE employee in a single call. Use this "
            "WHENEVER the user lists two or more skills for the same person — "
            "NEVER call propose_add_skill repeatedly for a list. Each item "
            "needs skill and category (one of: Language, Database, AI, Tool, "
            "Platform, Security, Design, BI, Cloud, OS, Other); qualifier is "
            "optional. Skills the person already has are skipped and "
            "reported. Resolve the person with search_employees first (or use "
            "the employee_id the user gave), then call this ONCE with the "
            "complete list. Never writes data; Streamlit confirmation is "
            "required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "skills": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "skill": {"type": "string"},
                            "category": {"type": "string"},
                            "qualifier": {"type": "string"},
                        },
                        "required": ["skill"],
                    },
                    "description": "The complete list of skills to add",
                },
            },
            "required": ["employee_id", "skills"],
        },
    },

    {
        "name": "propose_remove_skill",
        "description": (
            "Propose removing one exact skill from an employee; "
            "confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "skill": {"type": "string"},
            },
            "required": [
                "employee_id",
                "skill",
            ],
        },
    },

    {
        "name": "propose_add_certification",
        "description": (
            "Propose adding one certification; confirmation is required. "
            "New claims default to unverified."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "certification": {"type": "string"},
                "vendor": {"type": "string"},
                "verified": {"type": "string"},
            },
            "required": [
                "employee_id",
                "certification",
            ],
        },
    },

    {
        "name": "propose_add_project",
        "description": (
            "Propose assigning an employee to a project. Handles all assignment "
            "phrasings: 'assign <employee> to/on <project>', '<employee> on "
            "<project>', possessive/typo forms like \"2020134's on L&T project\". "
            "An employee_id given directly needs no search_employees call. "
            "project_type defaults to 'main' unless the user says support — "
            "state that default in your answer. Confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "project": {"type": "string"},
                "client": {"type": "string"},
                "technologies": {"type": "string"},
                "role": {"type": "string"},
                "project_type": {
                    "type": "string",
                    "description": (
                        "Assignment type. DEFAULT is 'main' — use it whenever the "
                        "user just says assign without specifying a type, and say "
                        "so in your answer so they can correct it. Only use "
                        "'support' when the user explicitly says support."
                    ),
                    "enum": ["main", "support"],
                },
            },
            "required": [
                "employee_id",
                "project",
            ],
        },
    },

    {
        "name": "propose_remove_project",
        "description": (
            "Propose removing one exact employee-project assignment; "
            "confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "project": {"type": "string"},
            },
            "required": [
                "employee_id",
                "project",
            ],
        },
    },

    {
        "name": "propose_move_project",
        "description": (
            "Propose moving an employee from one project to another; "
            "confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "from_project": {"type": "string"},
                "to_project": {"type": "string"},
                "client": {"type": "string"},
                "technologies": {"type": "string"},
                "role": {"type": "string"},
            },
            "required": [
                "employee_id",
                "from_project",
                "to_project",
            ],
        },
    },

    {
        "name": "propose_create_employee",
        "description": (
            "Propose creating an employee with ID, name, mail, and optional "
            "designation, position and team; confirmation is required. Designation "
            "and position are FREE TEXT — accept new titles the user gives (e.g. "
            "'FDE Trainee', 'Gen AI Developer') instead of asking them to pick an "
            "existing one. Common designations: Data Engineer, Gen AI Developer, "
            "Cyber Security Analyst, HR Executive, IT Recruiter, Presales Executive. "
            "Common positions: Intern, Team Lead, Senior. ONLY team is fixed: it "
            "must be one of the real teams — Cyber Team, Data Team, Gen AI Team, "
            "HR Team, Presales Team, Resourcing Team, Sales Team — and the backend "
            "rejects anything else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "name": {"type": "string"},
                "email": {"type": "string"},
                "designation": {
                    "type": "string",
                    "description": "Job designation, e.g. Data Engineer, Gen AI Developer",
                },
                "position": {
                    "type": "string",
                    "description": (
                        "Position level/title, FREE TEXT — any new title is fine, "
                        "e.g. Intern, Team Lead, Senior, FDE Trainee"
                    ),
                },
                "team": {
                    "type": "string",
                    "description": (
                        "One of the real teams: Cyber Team, Data Team, Gen AI Team, "
                        "HR Team, Presales Team, Resourcing Team, Sales Team"
                    ),
                },
            },
            "required": [
                "employee_id",
                "name",
            ],
        },
    },

    {
        "name": "propose_delete_certification",
        "description": (
            "Propose deleting one exact certification. "
            "Never writes data; Streamlit confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "certification": {"type": "string"},
            },
            "required": [
                "employee_id",
                "certification",
            ],
        },
    },

    {
        "name": "propose_update_project_role",
        "description": (
            "Propose changing an existing project role. "
            "Never writes data; Streamlit confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "project": {"type": "string"},
                "role": {"type": "string"},
            },
            "required": [
                "employee_id",
                "project",
                "role",
            ],
        },
    },

    {
        "name": "propose_update_project_technologies",
        "description": (
            "Propose updating technologies for an existing employee-project "
            "assignment; confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "project": {"type": "string"},
                "technologies": {"type": "string"},
            },
            "required": [
                "employee_id",
                "project",
                "technologies",
            ],
        },
    },

    {
        "name": "propose_update_manager",
        "description": (
            "Propose changing an employee's reporting manager by unique name "
            "or employee ID. Never writes data; confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "manager": {"type": "string"},
            },
            "required": [
                "employee_id",
                "manager",
            ],
        },
    },

    {
        "name": "propose_delete_employee",
        "description": (
            "Propose permanently deleting an employee and ALL their linked "
            "records (skills, projects, certifications, manager links). "
            "Requires the exact employee_id — never match by name alone. "
            "Never writes data; Streamlit confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
            },
            "required": [
                "employee_id",
            ],
        },
    },

    {
        "name": "propose_update_certification",
        "description": (
            "Propose renaming an employee's certification and/or changing "
            "its vendor. Never writes data; Streamlit confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "certification": {"type": "string"},
                "new_certification": {"type": "string"},
                "vendor": {"type": "string"},
            },
            "required": [
                "employee_id",
                "certification",
            ],
        },
    },

    {
        "name": "propose_update_project_client",
        "description": (
            "Propose changing the client for an employee's project. "
            "Never writes data; Streamlit confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "project": {"type": "string"},
                "client": {"type": "string"},
            },
            "required": [
                "employee_id",
                "project",
                "client",
            ],
        },
    },

    {
        "name": "propose_remove_manager",
        "description": (
            "Propose clearing an employee's reporting line (removes their "
            "manager link). Never writes data; Streamlit confirmation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
            },
            "required": [
                "employee_id",
            ],
        },
    },
]


# The unified agent's complete toolset: general reads + write proposals.
ALL_TOOLS = TOOLS + MANAGEMENT_TOOLS


MANAGEMENT_DISPATCH = {
    "propose_employee_update": lambda a: repo.propose_employee_update(
        a["employee_id"],
        a["field"],
        a["new_value"],
    ),

    "propose_clone_skills": lambda a: repo.propose_clone_skills(
        a["source_employee_id"],
        a["target_employee_id"],
        a.get("categories") or None,
    ),

    "propose_add_skill": lambda a: repo.propose_add_skill(
        a["employee_id"],
        a["skill"],
        a.get("category", "Other"),
        a.get("qualifier", ""),
    ),

    "propose_add_skills": lambda a: repo.propose_add_skills(
        a["employee_id"],
        a.get("skills") or [],
    ),

    "propose_remove_skill": lambda a: repo.propose_remove_skill(
        a["employee_id"],
        a["skill"],
    ),

    "propose_add_certification": lambda a: repo.propose_add_certification(
        a["employee_id"],
        a["certification"],
        a.get("vendor", ""),
        a.get("verified", "false"),
    ),

    "propose_add_project": lambda a: repo.propose_add_project(
        a["employee_id"],
        a["project"],
        a.get("client", ""),
        a.get("technologies", ""),
        a.get("role", ""),
        a.get("project_type", "main"),
    ),

    "propose_remove_project": lambda a: repo.propose_remove_project(
        a["employee_id"],
        a["project"],
    ),

    "propose_move_project": lambda a: repo.propose_move_project(
        a["employee_id"],
        a["from_project"],
        a["to_project"],
        a.get("client", ""),
        a.get("technologies", ""),
        a.get("role", ""),
    ),

    "propose_create_employee": lambda a: repo.propose_create_employee(
        a["employee_id"],
        a["name"],
        a.get("email", ""),
        a.get("designation", ""),
        a.get("position", ""),
        a.get("team", ""),
    ),

    "propose_delete_certification": lambda a: repo.propose_delete_certification(
        a["employee_id"],
        a["certification"],
    ),

    "propose_update_project_role": lambda a: repo.propose_update_project_role(
        a["employee_id"],
        a["project"],
        a["role"],
    ),

    "propose_update_project_technologies": lambda a: repo.propose_update_project_technologies(
        a["employee_id"],
        a["project"],
        a["technologies"],
    ),

    "propose_update_manager": lambda a: repo.propose_update_manager(
        a["employee_id"],
        a["manager"],
    ),

    "propose_update_certification": lambda a: repo.propose_update_certification(
        a["employee_id"],
        a["certification"],
        a.get("new_certification", ""),
        a.get("vendor", ""),
    ),

    "propose_update_project_client": lambda a: repo.propose_update_project_client(
        a["employee_id"],
        a["project"],
        a["client"],
    ),

    "propose_remove_manager": lambda a: repo.propose_remove_manager(
        a["employee_id"],
    ),

    "propose_delete_employee": lambda a: repo.propose_delete_employee(
        a["employee_id"],
    ),
}


def _frame(df) -> list[dict]:
    """Convert a dataframe-like result into JSON-safe records."""
    return df.to_dict("records")


def run_tool(name: str, args: dict) -> dict:
    """
    Execute a read-only tool and wrap the result.

    The wrapper is the validation layer: the agent is told explicitly when
    a result is empty, so it reports 'no one found' instead of inventing
    someone.
    """
    if name not in DISPATCH:
        return {"error": f"unknown tool '{name}'"}

    try:
        result = DISPATCH[name](args or {})
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"{type(exc).__name__}: {exc}"
        }

    if isinstance(result, list):
        return {
            "row_count": len(result),
            "rows": result,
            "note": "no matching records" if not result else "",
        }

    if isinstance(result, dict) and not result:
        return {
            "row_count": 0,
            "rows": [],
            "note": "no such employee_id",
        }

    return {
        "row_count": 1,
        "rows": [result],
        "note": "",
    }


def run_management_tool(name: str, args: dict) -> dict:
    """
    Execute a management proposal.

    These functions only create validated proposals. They do not directly
    write to the database.
    """
    if name not in MANAGEMENT_DISPATCH:
        return {"error": f"unknown management tool '{name}'"}

    try:
        result = MANAGEMENT_DISPATCH[name](args or {})
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"{type(exc).__name__}: {exc}"
        }

    return {
        "row_count": 1,
        "rows": [result],
        "note": "proposal only; no database write occurred",
    }