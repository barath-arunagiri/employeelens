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

# Multi-model fallback: if one model hits rate limit, automatically try the next.
# This prevents the agent from failing when a single model is rate-limited.
# ONLY models verified to make real OpenAI-style tool calls with this agent's
# prompts (tested 2026-10-14 with the clone-skills prompt):
#   gpt-oss-120b, gpt-oss-20b  -> proper search-then-propose flow
#   llama-4-maverick -> narrates tool calls as text instead of executing them
#   llama-3-3-70b / qwen3-next-80b -> skip name resolution, proposals fail
#   deepseek-v4-flash -> no tool calls at all
# Broken models must NOT appear here even as fallbacks: a fallback that
# produces a wrong flow is worse than a rate-limit error.
DATABRICKS_MODELS = [
    "databricks-gpt-oss-120b",   # Primary: verified function calling
    "databricks-gpt-oss-20b",    # Fallback: verified function calling
]

# Legacy single-model config (for backward compatibility)
DATABRICKS_MODEL = os.environ.get("DATABRICKS_MODEL", DATABRICKS_MODELS[0])
DATA_MANAGEMENT_ENABLED = os.environ.get("DATA_MANAGEMENT_ENABLED", "true").lower() == "true"
# Admin identities allowed to use Data Management (comma-separated emails,
# matched against the user identity Databricks Apps forwards in the
# x-forwarded-email header). Empty list = writes disabled (fail closed).
DATA_MANAGEMENT_ADMINS = [
    a.strip().lower() for a in os.environ.get("DATA_MANAGEMENT_ADMINS", "").split(",")
    if a.strip()
]
DATA_MANAGEMENT_PIN = os.environ.get("DATA_MANAGEMENT_PIN", "")

# Phase 2 unified-agent limits: the model may run up to 10 rounds of tool
# calls per request, and no more than MAX_REQUEST_TOOL_CALLS tool executions
# per request (each tool execution costs 1+ warehouse statements — this is
# the hard per-request warehouse budget).
MAX_TOOL_ROUNDS = 15
MAX_REQUEST_TOOL_CALLS = 40

# Conversation memory: a rolling session summary plus the most recent N
# messages are sent to the model so "that person" and "the first two"
# resolve without overflowing the context.
SESSION_HISTORY_MESSAGES = 12

# Trust the certifications column in the source spreadsheet as-is.
# True  = every certification is treated as confirmed (what the team asked for)
# False = certifications repeated identically across many people are flagged
# Set with TRUST_CERTIFICATIONS=false in .env to turn the flagging back on.
TRUST_CERTIFICATIONS = os.environ.get("TRUST_CERTIFICATIONS", "true").lower() == "true"

APP_NAME = "Employeelens"
