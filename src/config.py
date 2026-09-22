"""Paths and settings. Everything else imports from here."""
from __future__ import annotations

import os
from pathlib import Path

try:                                    # optional, keeps .env out of the shell
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
REFERENCE_DIR = DATA_DIR / "reference"
CLEAN_DIR = DATA_DIR / "clean"
REPORTS_DIR = DATA_DIR / "reports"
DB_PATH = ROOT / "database" / "peoplelens.duckdb"

DETAILS_XLSX = RAW_DIR / "Details.xlsx"
OCCUPATIONALS_XLSX = RAW_DIR / "Data_Team_Occupationals.xlsx"
if not OCCUPATIONALS_XLSX.exists():
    occupational_matches = sorted(RAW_DIR.glob("Data_Team_Occupationals*.xlsx"))
    if len(occupational_matches) == 1:
        OCCUPATIONALS_XLSX = occupational_matches[0]

VALID_EMAIL_DOMAIN = "@datapattern.ai"

# Databricks Foundation Model API (free pay-per-token).
# In Databricks Apps, authentication is automatic — no API key needed.
# Available endpoints: databricks-gpt-oss-120b, databricks-gpt-oss-20b,
# databricks-llama-4-maverick, databricks-qwen3-next-80b-a3b-instruct, etc.
DATABRICKS_MODEL = os.environ.get("DATABRICKS_MODEL", "databricks-gpt-oss-120b")
DATA_MANAGEMENT_ENABLED = os.environ.get("DATA_MANAGEMENT_ENABLED", "true").lower() == "true"
DATA_MANAGEMENT_PIN = os.environ.get("DATA_MANAGEMENT_PIN", "")

MAX_TOOL_ROUNDS = 6

# Trust the certifications column in the source spreadsheet as-is.
# True  = every certification is treated as confirmed (what the team asked for)
# False = certifications repeated identically across many people are flagged
# Set with TRUST_CERTIFICATIONS=false in .env to turn the flagging back on.
TRUST_CERTIFICATIONS = os.environ.get("TRUST_CERTIFICATIONS", "true").lower() == "true"

APP_NAME = "Employeelens"
