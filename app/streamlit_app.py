"""
Employeelens — Streamlit UI.

Run from the project root:
    streamlit run app/streamlit_app.py

Three tabs: Ask (the agent), Profile (employee 360), Explore (aggregates
straight from the repository, no model involved).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import (APP_NAME, CLEAN_DIR, DATA_MANAGEMENT_ENABLED, DATA_MANAGEMENT_PIN, 
                        DB_PATH, DATABRICKS_MODEL, REPORTS_DIR)  # noqa: E402
from src.database import repository as repo                                       # noqa: E402

st.set_page_config(page_title=APP_NAME, page_icon="🔎", layout="wide")

# DataPattern branding colors
DATAPATTERN_COLORS = {
    "primary": "#033c67",      # Dark blue (company name)
    "secondary": "#f46902",    # Orange (slogan)
    "accent": "#f18a31",       # Light orange (icon)
    "background": "#ffffff",   # White
}

# Custom CSS for DataPattern branding - ALL TEXT ELEMENTS
st.markdown("""
<style>
    /* DataPattern color scheme */
    .stApp {
        background-color: #ffffff;
    }
    
    /* Sidebar styling */
    [data-testid="stSidebar"] {
        background-color: #f8f9fa;
    }

    /* Sidebar logo – center it */
    [data-testid="stSidebar"] img {
        display: block;
        margin-left: auto;
        margin-right: auto;
    }
    
    /* ALL HEADERS - DataPattern blue */
    h1, h2, h3, h4, h5, h6 {
        color: #033c67 !important;
    }
    
    /* ALL BODY TEXT - DataPattern blue */
    p, span, div, label, li {
        color: #033c67;
    }
    
    /* Markdown text */
    .stMarkdown {
        color: #033c67;
    }
    
    /* Captions and small text */
    .stCaption, small {
        color: #033c67;
    }
    
    /* Input labels and form labels */
    [data-testid="stWidgetLabel"] {
        color: #033c67 !important;
    }
    
    /* Text input, text area */
    input, textarea {
        color: #033c67 !important;
    }
    
    /* Selectbox, radio, checkbox labels */
    .stSelectbox label, .stRadio label, .stCheckbox label {
        color: #033c67 !important;
    }
    
    /* Dataframe/table text */
    .stDataFrame, .stTable {
        color: #033c67;
    }
    
    /* Metrics styling */
    [data-testid="stMetricValue"] {
        color: #033c67 !important;
        font-weight: 600;
    }
    [data-testid="stMetricLabel"] {
        color: #033c67 !important;
    }
    [data-testid="stMetricDelta"] {
        color: #033c67 !important;
    }
    
    /* Expander headers */
    .streamlit-expanderHeader {
        color: #033c67 !important;
    }
    
    /* Info/Warning/Success/Error text */
    .stAlert {
        color: #033c67;
    }
    
    /* Primary buttons with DataPattern orange */
    .stButton>button[kind="primary"] {
        background-color: #f46902 !important;
        color: white !important;
        border: none;
    }
    .stButton>button[kind="primary"]:hover {
        background-color: #f18a31 !important;
        border: none;
    }
    
    /* Secondary buttons */
    .stButton>button[kind="secondary"] {
        color: #033c67 !important;
        border-color: #f46902 !important;
    }
    .stButton>button[kind="secondary"]:hover {
        background-color: #f46902 !important;
        color: white !important;
    }
    
    /* Links with orange accent */
    a {
        color: #f46902 !important;
    }
    a:hover {
        color: #f18a31 !important;
    }
    
    /* Tab styling */
    .stTabs [data-baseweb="tab-list"] button {
        color: #033c67;
    }
    .stTabs [data-baseweb="tab-list"] button[aria-selected="true"] {
        border-bottom-color: #f46902 !important;
        color: #033c67 !important;
        font-weight: 600;
    }
    
    /* Progress bar */
    .stProgress > div > div > div > div {
        background-color: #f46902 !important;
    }
    
    /* Slider labels and values */
    .stSlider label {
        color: #033c67 !important;
    }
    
    /* File uploader text */
    .stFileUploader label {
        color: #033c67 !important;
    }
    
    /* Chat messages */
    .stChatMessage {
        color: #033c67;
    }
    
    /* Code blocks - keep monospace but use blue */
    code {
        color: #033c67 !important;
    }
    pre {
        color: #033c67 !important;
    }
</style>
""", unsafe_allow_html=True)

# these are only shortcuts — the chat box takes any question
SUGGESTIONS = [
    "Who works on the Phibro project?",
    "Who has a Databricks certification?",
    "What are the top five skills in the organisation?",
    "Which project has the most people?",
    "Who knows Python and Azure and works on Phibro?",
    "Tell me about Siva",
    "Who could take over a Snowflake migration?",
    "Which skills does only one person have?",
]

HAS_MODEL = True
BACKEND_LABEL = f"Databricks · {DATABRICKS_MODEL}"


# ------------------------------------------------------------------ setup

def fatal(message: str) -> None:
    st.error(message)
    st.stop()


# Load DuckDB cache from Delta tables (source of truth)
# Delta tables at workspace.employeelens.* are the single source of truth.
# DuckDB is just a fast read-only cache.
# Uses SQL Warehouse connection in Databricks Apps.
DELTA_SYNC_AVAILABLE = False
delta_sync = None

try:
    from src.database import delta_sync_sql as delta_sync
    DELTA_SYNC_AVAILABLE = True
except (ImportError, RuntimeError):
    # Fall back to Spark-based if needed
    try:
        from src.database import delta_sync
        DELTA_SYNC_AVAILABLE = True
    except (ImportError, RuntimeError):
        DELTA_SYNC_AVAILABLE = False

delta_sync_succeeded = False

if DELTA_SYNC_AVAILABLE:
    # Try to load from Delta on startup
    try:
        delta_sync.load_from_delta_to_csv(str(CLEAN_DIR))
        # Rebuild DuckDB from the refreshed CSVs
        if DB_PATH.exists():
            DB_PATH.unlink()  # Remove old cache
        from src.ingestion.load_duckdb import main as _build_db
        _build_db()
        delta_sync_succeeded = True
    except Exception as e:
        # Delta sync failed - ensure DuckDB exists from CSV
        import warnings
        warnings.warn(f"Delta sync unavailable, using DuckDB-only mode: {e}")
        delta_sync_succeeded = False

# Ensure DuckDB exists (either from Delta or from CSV)
if not DB_PATH.exists():
    from src.ingestion.load_duckdb import main as _build_db
    _build_db()

try:
    repo.ensure_management_schema()
    STATS = repo.dataset_stats()
except Exception as exc:                                        # noqa: BLE001
    fatal(f"Could not read the database: {exc}")


@st.cache_resource(show_spinner=False)
def get_agent():
    from src.agent.agent import PeopleLensAgent
    return PeopleLensAgent()


# ---------------------------------------------------------------- sidebar

with st.sidebar:
    # DataPattern logo (trimmed to exact content)
    logo_path = ROOT / "app" / "assets" / "Datapattern_logo.png"
    if logo_path.exists():
        st.image(str(logo_path), width=200)
    else:
        # Fallback: styled header
        st.markdown(
            f'<h2 style="color: #033c67; margin-bottom: 0px;">{APP_NAME}</h2>'
            '<p style="color: #f46902; font-size: 14px; margin-top: 0px;">'
            '<b>Ensure Transformation</b></p>',
            unsafe_allow_html=True
        )
    
    st.caption("Employee data assistant")

    st.metric("Employees", STATS["employees"])
    cols = st.columns(2)
    cols[0].metric("With skills", STATS["with_skills"])
    cols[1].metric("Projects", STATS["projects"])
    cols = st.columns(2)
    cols[0].metric("Distinct skills", STATS["skills"])
    if STATS["certifications_unconfirmed"]:
        cols[1].metric("Certifications", STATS["certifications_confirmed"],
                       delta=f"{STATS['certifications_unconfirmed']} unconfirmed",
                       delta_color="off")
    else:
        cols[1].metric("Certifications", STATS["certifications_confirmed"])

    coverage = STATS["with_skills"] / max(STATS["employees"], 1)
    st.progress(coverage, text=f"Skill coverage {coverage:.0%}")

    st.divider()
    st.caption(f"Model: `{BACKEND_LABEL}`")
    
    # Show data persistence mode
    if delta_sync_succeeded:
        st.caption("💾 Data: Delta Lake (Unity Catalog)")
    else:
        st.caption("💾 Data: DuckDB cache (CSV fallback)")
    
    # Debug mode toggle (off by default)
    if "debug_mode" not in st.session_state:
        st.session_state.debug_mode = False
    st.session_state.debug_mode = st.checkbox(
        "Show tool trace", 
        value=st.session_state.debug_mode,
        help="Show 'How this was answered' debug info under responses"
    )

    # Suspect certifications check removed - all certifications shown without verification status

    issues_path = REPORTS_DIR / "data_issues.csv"
    if issues_path.exists():
        with st.expander("Data quality flags"):
            st.dataframe(pd.read_csv(issues_path), hide_index=True, use_container_width=True)

    st.divider()
    st.caption("Management fields added through the UI are stored in DuckDB and are "
               "not written back to the source spreadsheets.")


# -------------------------------------------------------------------- tabs

ask_tab, profile_tab, explore_tab, management_tab = st.tabs(
    ["Ask", "Profile", "Explore", "Data Management"]
)

# ---- Ask
with ask_tab:
    if "messages" not in st.session_state:
        st.session_state.messages = []

    if not st.session_state.messages:
        st.markdown("#### Ask anything about the team")
        st.caption("Type your own question — these are just examples. Follow-ups work too, "
                   "so you can say \"and her projects?\" after asking about someone.")
        cols = st.columns(2)
        for i, suggestion in enumerate(SUGGESTIONS):
            if cols[i % 2].button(suggestion, key=f"sug{i}", use_container_width=True):
                st.session_state.pending = suggestion
                st.rerun()

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            # Show trace only if debug mode is enabled
            if message.get("trace") and st.session_state.get("debug_mode", False):
                with st.expander(f"How this was answered — {len(message['trace'])} tool call(s)"):
                    for step in message["trace"]:
                        st.markdown(f"**`{step['tool']}`** → {step['row_count']} row(s)")
                        st.json(step["input"], expanded=False)

    # Toggle between quick chat and detailed multiline mode
    if "input_mode" not in st.session_state:
        st.session_state.input_mode = "quick"  # default to quick mode

    # Mode toggle in sidebar or inline
    input_col1, input_col2 = st.columns([6, 1])
    with input_col2:
        if st.session_state.input_mode == "quick":
            if st.button("📝 Multiline", help="Switch to multiline input mode (Shift+Enter for newlines)", use_container_width=True):
                st.session_state.input_mode = "detailed"
                st.rerun()
        else:
            if st.button("💬 Quick", help="Switch to single-line chat mode (Enter to send)", use_container_width=True):
                st.session_state.input_mode = "quick"
                st.rerun()

    prompt = None

    # Quick mode: single-line chat input (fast, enter to send)
    if st.session_state.input_mode == "quick":
        prompt = st.chat_input("Ask anything about the team…")
        prompt = prompt or st.session_state.pop("pending", None)

    # Detailed mode: multiline text area (for complex queries)
    else:
        with input_col1:
            detailed_query = st.text_area(
                "Multiline query (press Enter for newlines, click Send to submit)",
                placeholder="I need to find employees who:\n1. Have Python and Azure skills\n2. Work on active projects\n3. Have Databricks certifications\n\nAnd show me their project history.",
                height=150,
                key="detailed_query_input",
                label_visibility="collapsed"
            )
            send_col1, send_col2 = st.columns([1, 5])
            with send_col1:
                if st.button("🚀 Send", type="primary", key="send_detailed", use_container_width=True):
                    if detailed_query.strip():
                        prompt = detailed_query
                    else:
                        st.warning("Please enter a query first.")
            with send_col2:
                st.caption("💡 Tip: Full multiline text is sent as-is, preserving all formatting.")
        
        # Suggestion buttons still work in detailed mode
        prompt = prompt or st.session_state.pop("pending", None)

    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Looking it up…"):
                try:
                    history = [{"role": m["role"], "content": m["content"]}
                               for m in st.session_state.messages[:-1]]
                    result = get_agent().ask(prompt, history=history)
                    answer, trace = result.answer, result.trace
                except Exception as exc:                        # noqa: BLE001
                    answer, trace = f"Something went wrong: `{exc}`", []
            st.markdown(answer)
            # Show trace only if debug mode is enabled
            if trace and st.session_state.get("debug_mode", False):
                with st.expander(f"How this was answered — {len(trace)} tool call(s)"):
                    for step in trace:
                        st.markdown(f"**`{step['tool']}`** → {step['row_count']} row(s)")
                        st.json(step["input"], expanded=False)

        st.session_state.messages.append(
            {"role": "assistant", "content": answer, "trace": trace})

    if st.session_state.messages:
        if st.button("Clear conversation"):
            st.session_state.messages = []
            st.rerun()

# ---- Data Management
with management_tab:
    st.markdown("#### Data Management")
    st.caption("Use one natural-language request for a change or read-only audit history. Changes require confirmation.")

    admin_ok = DATA_MANAGEMENT_ENABLED
    if DATA_MANAGEMENT_PIN:
        if not st.session_state.get("management_authenticated"):
            entered_pin = st.text_input("Admin PIN", type="password", key="management_pin")
            if st.button("Unlock Data Management", key="unlock_management"):
                if entered_pin == DATA_MANAGEMENT_PIN:
                    st.session_state.management_authenticated = True
                    st.rerun()
                st.error("Invalid admin PIN.")
            admin_ok = False

    if not admin_ok:
        st.info("Data Management is locked. Set DATA_MANAGEMENT_ENABLED=true and use the configured admin PIN.")
    else:
        request = st.text_area(
            "Describe the change you want to make",
            placeholder=("Change EMP1001's designation to Senior Data Engineer.\n"
                          "Add Python as a skill for EMP1005.\n\n"
                          "You can enter multiple requests, one per line."),
            key="management_request",
            height=150,  # Explicit height for better multiline input
        )
        if st.button("Process Request", type="primary", key="process_management"):
            st.session_state.management_proposal = None
            st.session_state.management_proposals = []
            st.session_state.management_trace = []
            if not request.strip():
                st.warning("Describe a change first.")
            else:
                with st.spinner("Preparing a proposed change..."):
                    try:
                        result = get_agent().propose_management(request)
                        st.session_state.management_proposals = result.proposals or ([result.proposal] if result.proposal else [])
                        st.session_state.management_proposal = result.proposal
                        st.session_state.management_message = result.answer
                        st.session_state.management_trace = result.trace
                    except Exception as exc:                    # noqa: BLE001
                        st.session_state.management_proposal = None
                        st.session_state.management_proposals = []
                        st.session_state.management_message = f"Something went wrong: {exc}"
                        st.session_state.management_trace = []

        if st.session_state.get("management_message"):
            st.info(st.session_state.management_message)

        for step in st.session_state.get("management_trace", []):
            if step.get("tool") == "get_audit_history":
                rows = step.get("output", {}).get("rows", [])
                st.markdown(f"**Audit history ({len(rows)} record(s))**")
                if rows:
                    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
                else:
                    st.caption("No matching audit records.")

        proposals = st.session_state.get("management_proposals", [])
        if proposals:
            st.markdown("**Proposed changes**")
            # Show project_type as a selectable dropdown for project proposals
            for i, proposal in enumerate(proposals):
                if proposal.get("table") == "employee_projects" and "project_type" in proposal:
                    current_type = proposal.get("project_type", "main")
                    selected_type = st.selectbox(
                        f"Project type — {proposal.get('project', 'project')}",
                        options=["main", "support"],
                        index=0 if current_type == "main" else 1,
                        key=f"project_type_select_{i}",
                    )
                    proposal["project_type"] = selected_type
                st.json(proposal, expanded=True)
            action = "Confirm Changes" if len(proposals) > 1 else (
                "Confirm Delete" if proposals[0]["operation"] == "DELETE" else "Confirm Update"
            )
            confirm_col, cancel_col = st.columns(2)
            if confirm_col.button(action, type="primary", key="confirm_management"):
                try:
                    results = repo.execute_proposals(proposals) if len(proposals) > 1 else [repo.execute_proposal(proposals[0])]
                    st.success(
                        f"SUCCESS: {len(results)} change(s) committed and verified."
                    )
                    st.session_state.management_proposal = None
                    st.session_state.management_proposals = []
                    st.session_state.management_message = None
                    st.session_state.management_trace = []
                except Exception as exc:                        # noqa: BLE001
                    st.error(f"The change was not applied: {exc}")
            if cancel_col.button("Cancel", key="cancel_management"):
                st.session_state.management_proposal = None
                st.session_state.management_proposals = []
                st.session_state.management_message = None
                st.session_state.management_trace = []
                st.info("Change cancelled. No database update was made.")

# ---- Profile
with profile_tab:
    people = repo.list_all_employees()
    labels = {f"{r['name']} — {r['employee_id']}": r["employee_id"]
              for _, r in people.iterrows()}
    st.caption(f"All {len(labels)} employees")
    choice = st.selectbox("Employee", sorted(labels), index=None,
                          placeholder="Search for a name or employee id…")

    if choice:
        profile = repo.get_employee_360(labels[choice])
        st.markdown(f"### {profile['name']}")
        position = profile.get("position") or ""
        designation = profile.get("designation") or ""
        if position and "senior" in position.lower():
            role = f"{position} - {designation}" if designation else position
        elif position:
            role = position
        else:
            role = designation
        details = st.columns([3, 1, 2, 2])
        details[0].markdown(f"**Email**  \n{profile['email'] or '—'}")
        details[1].markdown(f"**ID**  \n{profile['employee_id']}")
        details[2].markdown(f"**Role**  \n{role or '—'}")
        details[3].markdown(f"**Team**  \n{profile.get('team') or '—'}")

        left, right = st.columns(2)
        with left:
            st.markdown("**Projects**")
            if profile["projects"]:
                projects = pd.DataFrame(profile["projects"]).rename(
                    columns={"technologies": "Work & Tools"}
                )
                st.dataframe(projects, hide_index=True,
                             use_container_width=True)
            else:
                st.caption("No project recorded.")

            st.markdown("**Certifications**")
            if profile["certifications"]:
                for cert in profile["certifications"]:
                    st.markdown(f"- {cert['certification']}")
            else:
                st.caption("None recorded.")

        with right:
            st.markdown("**Reports to**")
            st.caption(", ".join(profile["reports_to"]) or "Not recorded, or reports "
                                                           "to someone outside this sheet.")
            if profile["direct_reports"]:
                st.markdown(f"**Direct reports** ({len(profile['direct_reports'])})")
                st.caption(", ".join(profile["direct_reports"]))

            st.markdown("**Skills**")
            skills = pd.DataFrame(profile["skills"])
            if skills.empty:
                st.caption("No skills recorded — this profile still needs to be filled in.")
            else:
                for category, group in skills.groupby("category"):
                    st.markdown(f"*{category}* — " + ", ".join(group["skill"]))

# ---- Explore
with explore_tab:
    left, right = st.columns(2)

    with left:
        st.markdown("**People per project**")
        st.dataframe(repo.headcount_by_project(), hide_index=True, use_container_width=True)

        st.markdown("**Certifications held**")
        st.caption("Confirmed = from the person's own skills entry. "
                   "Unconfirmed = from the copy-pasted block.")
        st.dataframe(repo.certification_counts(), hide_index=True, use_container_width=True)

    with right:
        st.markdown("**Most common skills**")
        how_many = st.slider("How many", 5, 30, 12)
        top = repo.top_skills(how_many)
        st.bar_chart(top.set_index("skill")["people"], horizontal=True)

        st.markdown("**Skill categories**")
        st.dataframe(repo.skill_categories(), hide_index=True, use_container_width=True)

    st.divider()
    missing = repo.employees_without_skills()
    st.markdown(f"**Profiles with no skills recorded — {len(missing)} people**")
    st.caption("This is the list to chase. Until these are filled in, any "
               "'nobody has skill X' answer is only true of the people who do have profiles.")
    st.dataframe(missing, hide_index=True, use_container_width=True)
