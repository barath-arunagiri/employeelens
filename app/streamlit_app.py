"""
Employeelens — Streamlit UI.

Run from the project root:
    streamlit run app/streamlit_app.py

Four tabs: Ask (the agent), Profile (employee 360 with a Team Structure
view for the reporting hierarchy), Explore (aggregates, no model), and
Data Management (proposals + confirmation). Branding, statistics panel
and all existing behaviour are unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import (APP_NAME, DATA_MANAGEMENT_ADMINS, DATA_MANAGEMENT_ENABLED,
                        DATABRICKS_MODEL, REPORTS_DIR)  # noqa: E402
from src.database import repository as repo                                       # noqa: E402


def get_user_email() -> str:
    """
    Real user identity forwarded by Databricks Apps.

    Databricks Apps injects the authenticated user's email in the
    x-forwarded-email header. Returns '' when the identity is not
    available so callers can fail closed.
    """
    try:
        headers = st.context.headers or {}
        email = (headers.get("x-forwarded-email")
                 or headers.get("x-forwarded-user") or "").strip()
        return email
    except Exception:  # noqa: BLE001
        return ""

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
    /* Data tables: size columns to content instead of stretching the
       first column across the full container width */
    .stMarkdown table {
        width: auto;
        min-width: 30%;
        table-layout: auto;
    }
    .stMarkdown th,
    .stMarkdown td {
        white-space: nowrap;
        padding: 6px 18px 6px 10px;
    }
    .stMarkdown th:first-child,
    .stMarkdown td:first-child {
        width: 1%;
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
]

HAS_MODEL = True
BACKEND_LABEL = f"Databricks · {DATABRICKS_MODEL}"


# ------------------------------------------------------------------ setup

def fatal(message: str) -> None:
    st.error(message)
    st.stop()


# ---- Cached read layer -----------------------------------------------
# Streamlit reruns the whole script (every tab body) on every interaction.
# Without caching, each rerun fired ~8 Unity Catalog round trips and every
# tab switch took seconds. Reads are now cached for 5 minutes and the cache
# is cleared after every successful Data Management write, so changes show
# up immediately on every page. NOTE: these definitions must stay ABOVE
# the first use (STATS = c_dataset_stats()) at module level.

_CACHE_TTL = 300  # seconds


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def c_dataset_stats():
    return repo.dataset_stats()


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def c_list_all_employees():
    return repo.list_all_employees()


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def c_employee_360(employee_id: str):
    return repo.get_employee_360(employee_id)


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def c_headcount_by_project():
    return repo.headcount_by_project()


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def c_certification_counts():
    return repo.certification_counts()


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def c_top_skills(limit: int = 10):
    return repo.top_skills(limit)


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def c_skill_categories():
    return repo.skill_categories()


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def c_employees_without_skills():
    return repo.employees_without_skills()


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def c_team_structure():
    return repo.get_team_structure()


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def c_employee_directory():
    return repo.employee_directory()


# Unity Catalog is the single source of truth; reads go through the cached
# layer above so page switches don't re-query Delta tables on every rerun.

delta_sync_succeeded = False

try:
    from src.database import uc_connection
    if uc_connection.is_available():
        print("✅ Connected to Unity Catalog (workspace.employeelens)")
        delta_sync_succeeded = True
    else:
        print("⚠️  Unity Catalog not available")
except Exception as e:
    print(f"⚠️  Unity Catalog connection failed: {e}")

try:
    STATS = c_dataset_stats()
except Exception as exc:
    fatal(f"Could not read from Unity Catalog: {exc}")


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
    
    # Show data source
    if delta_sync_succeeded:
        st.caption("💾 Data: Unity Catalog (Delta Lake)")
    else:
        st.caption("💾 Data: CSV files (local)")
    
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
    st.caption("All data is stored in Unity Catalog (workspace.employeelens) "
               "and persists across app restarts.")


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
            from src.agent.agent import Session

            if "agent_session" not in st.session_state:
                st.session_state.agent_session = Session()

            holder = {}
            trace = []
            answer = ""
            try:
                # One agent, one session: questions AND change requests.
                # The final answer streams progressively; the completed
                # AgentResult (trace, proposals) arrives via result_holder.
                answer = st.write_stream(
                    get_agent().chat_stream(
                        prompt,
                        session=st.session_state.agent_session,
                        result_holder=holder,
                    )
                )
                result = holder.get("result")
                trace = result.trace if result else []

                # A change request typed in the Ask tab: proposals are
                # prepared (never written) and staged for confirmation in
                # the Data Management tab.
                if result and result.proposals:
                    st.session_state.management_proposals = result.proposals
                    st.session_state.management_proposal = result.proposals[0]
                    st.session_state.management_trace = result.trace
                    st.session_state.management_message = result.answer
                    st.info(
                        f"{len(result.proposals)} proposal(s) prepared — review "
                        "and confirm them in the **Data Management** tab. "
                        "Nothing is written until you confirm."
                    )
            except Exception as exc:                            # noqa: BLE001
                answer = (
                    f"I hit an error while answering that "
                    f"({type(exc).__name__}). Please try again."
                )
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
            from src.agent.agent import Session
            st.session_state.agent_session = Session()  # fresh memory too
            st.rerun()

# ---- Data Management
with management_tab:
    st.markdown("#### Data Management")
    st.caption("Use one natural-language request for a change or read-only audit history. Changes require confirmation.")

    # Admin-only, fail closed: writes require a configured admin list AND a
    # verified user identity from the Databricks Apps forwarded header.
    user_email = get_user_email()
    admin_ok = (
        DATA_MANAGEMENT_ENABLED
        and bool(DATA_MANAGEMENT_ADMINS)
        and bool(user_email)
        and user_email.lower() in DATA_MANAGEMENT_ADMINS
    )

    if not admin_ok:
        if not DATA_MANAGEMENT_ADMINS:
            st.info("Data Management is locked: no admin identities are configured "
                    "(DATA_MANAGEMENT_ADMINS is empty). Writes are disabled.")
        elif not user_email:
            st.info("Data Management is locked: no verified user identity was "
                    "forwarded by Databricks Apps. Writes are disabled.")
        else:
            st.info(f"Data Management is admin-only. You are signed in as {user_email}.")
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
                        from src.agent.agent import Session

                        if "agent_session" not in st.session_state:
                            st.session_state.agent_session = Session()
                        # Same unified agent and same session as the Ask
                        # tab — the model decides this is a change request.
                        result = get_agent().chat(
                            request,
                            session=st.session_state.agent_session,
                        )
                        st.session_state.management_proposals = result.proposals or ([result.proposal] if result.proposal else [])
                        st.session_state.management_proposal = result.proposal
                        st.session_state.management_message = result.answer
                        st.session_state.management_trace = result.trace
                    except Exception as exc:                    # noqa: BLE001
                        st.session_state.management_proposal = None
                        st.session_state.management_proposals = []
                        st.session_state.management_message = (
                            f"I hit an error preparing that change "
                            f"({type(exc).__name__}). Please try again."
                        )
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

            import html as _html

            _OP_COLORS = {"DELETE": "#b3261e", "INSERT": "#1e7b34",
                          "UPDATE": "#8a5a00", "MOVE": "#8a5a00"}

            def _proposal_card(p):
                """Human-readable, aligned summary of one proposal."""
                op = p.get("operation", "")
                color = _OP_COLORS.get(op, "#033c67")

                def esc(v):
                    return _html.escape(str(v))

                rows = [("Operation", f"{op} on {p.get('table', '')}"),
                        ("Employee", esc(p.get("employee_id", "")))]
                if p.get("field") and p.get("field") != "employee":
                    rows.append(("Field", esc(p["field"])))
                if p.get("old_value") is not None:
                    rows.append(("Current value", esc(p["old_value"])))
                if p.get("new_value") is not None:
                    rows.append(("New value", esc(p["new_value"])))
                for extra in ("project", "email", "category", "qualifier", "client",
                              "technologies", "role", "vendor", "verified"):
                    if p.get(extra):
                        rows.append((extra.replace("_", " ").title(), esc(p[extra])))
                table_rows = "".join(
                    f"<tr><td style='padding:4px 16px 4px 0;color:#5a6b7a;font-weight:600;"
                    f"white-space:nowrap;vertical-align:top;width:130px;'>{esc(k)}</td>"
                    f"<td style='padding:4px 0;'>{v}</td></tr>" for k, v in rows)
                card = (
                    f"<div style='border:1px solid #e3e9f0;border-left:4px solid {color};"
                    f"border-radius:8px;padding:10px 14px;background:#ffffff;margin:2px 0;'>"
                    f"<table style='border-collapse:collapse;font-size:14px;'>{table_rows}</table>"
                )
                s = p.get("summary")
                if s:
                    card += (
                        f"<div style='margin-top:8px;color:#b3261e;font-weight:600;'>"
                        f"This will permanently remove: {s.get('skills', 0)} skill(s), "
                        f"{s.get('projects', 0)} project assignment(s), "
                        f"{s.get('certifications', 0)} certification(s), "
                        f"{s.get('manager_links', 0)} manager link(s).</div>"
                    )
                if p.get("warning"):
                    card += (f"<div style='margin-top:6px;color:#b3261e;font-size:13px;'>"
                             f"\u26a0\ufe0f {esc(p['warning'])}</div>")
                card += "</div>"
                st.markdown(card, unsafe_allow_html=True)

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
                _proposal_card(proposal)
            action = "Confirm Changes" if len(proposals) > 1 else (
                "Confirm Delete" if proposals[0]["operation"] == "DELETE" else "Confirm Update"
            )
            confirm_col, cancel_col = st.columns(2)
            if confirm_col.button(action, type="primary", key="confirm_management"):
                confirmed_email = get_user_email() or "unknown"
                try:
                    results = repo.execute_proposals(proposals, confirmed_by=confirmed_email)
                    st.cache_data.clear()  # writes are live: refresh every page immediately
                    # Feed the verified outcome back into the shared agent
                    # session so follow-up turns know what really happened.
                    if "agent_session" in st.session_state:
                        st.session_state.agent_session.record_execution(
                            [r for r in results if r.get("status") == "SUCCESS"],
                            actor=confirmed_email,
                        )
                    ok = [r for r in results if r.get("status") == "SUCCESS"]
                    failed = [r for r in results if r.get("status") != "SUCCESS"]
                    if ok:
                        st.success(
                            f"SUCCESS: {len(ok)} change(s) committed and verified "
                            f"by {confirmed_email}."
                        )
                    for r in failed:
                        p = r.get("proposal", {})
                        st.warning(
                            f"Skipped {p.get('operation')} on {p.get('table')} "
                            f"for {p.get('employee_id')}: {r.get('error', 'unknown error')}"
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

# ---- Profile (with a Team Structure view selector)
with profile_tab:
    # A view switch requested from the Team Structure view ('Open full
    # profile') is applied BEFORE the view radio is instantiated —
    # Streamlit forbids changing a widget's state after the widget
    # exists in the same run.
    if "pending_profile_view" in st.session_state:
        st.session_state.profile_view = st.session_state.pop("pending_profile_view")

    view = st.radio("View", ["Employee profile", "Team structure"],
                    horizontal=True, key="profile_view",
                    label_visibility="collapsed")

    if view == "Employee profile":
        people = c_list_all_employees()
        labels = {f"{r['name']} — {r['employee_id']}": r["employee_id"]
                  for _, r in people.iterrows()}
        st.caption(f"All {len(labels)} employees")
        choice = st.selectbox("Employee", sorted(labels), index=None,
                              placeholder="Search for a name or employee id…",
                              key="profile_choice")

        if choice:
            profile = c_employee_360(labels[choice])
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

    else:  # Team structure view — team-lead-wise, in the organisation's team order
        import html as _ts_html

        def _ts_esc(v) -> str:
            return _ts_html.escape(str(v) if v is not None else "")

        structure = c_team_structure()
        directory = c_employee_directory()
        dir_by_id = {str(r["employee_id"]): r for _, r in directory.iterrows()}

        # The teams in the order the organisation wants them listed; any other
        # team present in the data is appended after (nothing is hidden).
        TEAM_ORDER = ["Data", "Gen AI", "Cyber", "Sales", "Presales", "Resourcing"]

        def _role(rec) -> str:
            # The same role string the Employee profile shows: senior
            # positions combine position and designation, other positions
            # show as-is, designation is the fallback.
            pos = str(rec.get("position") or "").strip()
            des = str(rec.get("designation") or "").strip()
            if pos and "senior" in pos.lower():
                return f"{pos} - {des}" if des else pos
            return pos or des

        def _card(rec, name: str, subtitle=None) -> str:
            sub = _role(rec) if subtitle is None else subtitle
            return ("<div style='border-left:3px solid #d7e3ef;background:#f8f9fa;"
                    "border-radius:6px;padding:8px 12px;margin:4px 0;'>"
                    f"<b style='color:#033c67;'>{_ts_esc(name)}</b>"
                    + (f"<br><span style='font-size:13px;color:#4a6a86;'>"
                       f"{_ts_esc(sub)}</span>" if sub else "")
                    + "</div>")

        def _is_team_lead(rec) -> bool:
            return "team lead" in str(rec.get("position") or "").lower()

        def _is_manager(rec) -> bool:
            return "manager" in str(rec.get("position") or "").lower()

        # lead_id -> direct-report rows (real reporting links only)
        members_by_lead: dict[str, list] = {}
        for _, r in structure.iterrows():
            members_by_lead.setdefault(str(r["lead_id"]), []).append(r)

        # Team directory from the team column, keyed without the ' Team' suffix
        team_dir: dict[str, list] = {}
        for _, r in directory.iterrows():
            team = str(r.get("team") or "").replace(" Team", "").strip()
            if team:
                team_dir.setdefault(team, []).append(r)

        ordered = [t for t in TEAM_ORDER if t in team_dir] \
            + sorted(t for t in team_dir if t not in TEAM_ORDER)

        # Leadership strip above the teams: managers / operational leads on the
        # left (employee-id order), HR on the top right. Real people from the
        # directory — not repeated inside the team sections below.
        leadership = sorted(
            [r for _, r in directory.iterrows()
             if ("manager" in str(r.get("position") or "").lower()
                 or "operational" in str(r.get("position") or "").lower())
             and not _is_team_lead(r)],
            key=lambda r: str(r["employee_id"]))
        hr_people = sorted(team_dir.get("HR", []),
                           key=lambda r: str(r["employee_id"]))
        strip_ids = {str(r["employee_id"]) for r in leadership + hr_people}

        strip_l, strip_r = st.columns([3, 1])
        with strip_l:
            lcols = st.columns(len(leadership)) if leadership else []
            for col, r in zip(lcols, leadership):
                with col:
                    st.markdown(_card(r, r["name"], _role(r)),
                                unsafe_allow_html=True)
        with strip_r:
            for r in hr_people:
                st.markdown(_card(r, r["name"], _role(r)),
                            unsafe_allow_html=True)

        for team in ordered:
            if team == "HR":
                continue  # HR is shown in the strip on the top right
            # Team members ordered by employee id (leads included, in id order)
            people = sorted(team_dir[team], key=lambda r: str(r["employee_id"]))
            leads = [r for r in people if _is_team_lead(r)]
            # Leadership-strip people are not repeated inside a team; manager-
            # level people who head a reporting group are not team members.
            shown = {str(r["employee_id"]) for r in people
                     if str(r["employee_id"]) in strip_ids
                     or (_is_manager(r) and str(r["employee_id"]) in members_by_lead)}

            st.markdown(f"#### {team} Team")
            if leads:
                st.caption("Team lead" + ("s" if len(leads) > 1 else "") + ": "
                           + ", ".join(str(r["name"]) for r in leads))
            else:
                st.caption("No team lead recorded")

            # Lead-wise groups from real reporting links, largest team first.
            lead_groups = []
            for r in leads:
                lid = str(r["employee_id"])
                rows = members_by_lead.get(lid, [])
                if not rows:
                    continue  # lead without recorded reports shows as a member below
                lead_groups.append((lid, r, rows))
                shown.add(lid)
                shown.update(str(x["member_id"]) for x in rows)
            lead_groups.sort(key=lambda g: len(g[2]), reverse=True)

            # Two per row so the next lead and their members fill the space
            # beside the previous one. Leads show the role from their profile.
            for i in range(0, len(lead_groups), 2):
                pair = st.columns(2)
                for col, (lid, lead, rows) in zip(pair, lead_groups[i:i + 2]):
                    with col:
                        lead_line = (f"**{lead.get('name', lid)}** · "
                                     f"{_role(lead) or 'Team Lead'}"
                                     f" — {len(rows)} member(s)")
                        st.markdown(lead_line)
                        st.markdown(
                            "<div style='border-left:2px solid #cfe0ef;padding-left:2px;'>"
                            + "".join(_card(dir_by_id.get(str(x["member_id"]), {}),
                                            x["member_name"]) for x in rows)
                            + "</div>", unsafe_allow_html=True)

            # Remaining team members (no reporting line recorded) — role only
            rest = [r for r in people if str(r["employee_id"]) not in shown]
            if rest and lead_groups:
                st.caption("Other members")
            for i in range(0, len(rest), 3):
                cols = st.columns(3)
                for col, r in zip(cols, rest[i:i + 3]):
                    with col:
                        st.markdown(_card(r, r["name"]), unsafe_allow_html=True)

# ---- Explore (dashboard view)
with explore_tab:
    import altair as alt

    st.markdown("#### Team dashboard")
    st.caption("Live aggregates from Unity Catalog · auto-refreshes every 5 minutes")

    stats = c_dataset_stats()
    coverage = stats["with_skills"] / max(stats["employees"], 1)

    def _section(title):
        st.markdown(
            f'<div style="border-left:4px solid #f46902;padding-left:10px;'
            f'font-size:17px;font-weight:700;color:#033c67;margin:10px 0 6px;">{title}</div>',
            unsafe_allow_html=True,
        )

    # --- Charts row 1: top skills + people per project ---
    left, right = st.columns(2)
    with left:
        how_many = st.slider("Top skills", 5, 30, 10, key="top_skills_n")
        _section("🛠️ Most common skills")
        st.altair_chart(
            alt.Chart(c_top_skills(how_many))
            .mark_bar(color="#f46902", cornerRadiusEnd=3)
            .encode(
                y=alt.Y("skill:N", sort="-x"),
                x=alt.X("people:Q", title="People"),
                tooltip=["skill", "category", "people"],
            )
            .properties(height=310),
            use_container_width=True,
        )

    with right:
        _section("📁 People per project")
        hp = c_headcount_by_project()
        st.altair_chart(
            alt.Chart(hp)
            .mark_bar(color="#033c67", cornerRadiusEnd=3)
            .encode(
                y=alt.Y("project:N", sort="-x"),
                x=alt.X("people:Q", title="People"),
                tooltip=["project", "client", "people"],
            )
            .properties(height=310),
            use_container_width=True,
        )
        with st.expander(f"Project detail ({len(hp)})"):
            st.dataframe(hp, hide_index=True, use_container_width=True)

    # --- Charts row 2: skills by category (donut) + certifications ---
    cat_col, cert_col = st.columns(2)
    with cat_col:
        _section("🗂️ Skills by category")
        sc = c_skill_categories()
        st.altair_chart(
            alt.Chart(sc)
            .mark_arc(innerRadius=62, outerRadius=110, stroke="#ffffff", strokeWidth=2)
            .encode(
                theta=alt.Theta("people:Q"),
                color=alt.Color("category:N", scale=alt.Scale(scheme="oranges")),
                tooltip=["category", "skills", "people"],
            )
            .properties(height=280),
            use_container_width=True,
        )

    with cert_col:
        _section("📜 Certifications held")
        cc = c_certification_counts()
        st.altair_chart(
            alt.Chart(cc)
            .mark_bar(color="#f18a31", cornerRadiusEnd=3)
            .encode(
                y=alt.Y("certification:N", sort="-x"),
                x=alt.X("claimed_total:Q", title="People"),
                tooltip=["certification", "confirmed", "unconfirmed", "claimed_total"],
            )
            .properties(height=280),
            use_container_width=True,
        )
        with st.expander(f"Certification detail ({len(cc)})"):
            st.dataframe(cc, hide_index=True, use_container_width=True)

    st.write("")

    # --- Coverage gap ---
    missing = c_employees_without_skills()
    _section(f"⚠️ Coverage gap — {len(missing)} profile(s) with no skills recorded")
    st.caption("This is the list to chase. Until these are filled in, any "
               "'nobody has skill X' answer is only true of the people who do have profiles.")
    st.progress(coverage, text=f"{stats['with_skills']} of {stats['employees']} profiles have skills")
    with st.expander("Profiles to chase"):
        st.dataframe(missing, hide_index=True, use_container_width=True, height=240)
