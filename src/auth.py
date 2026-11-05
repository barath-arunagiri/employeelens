"""Allowlisted HR admin login using the employee ID as the login code."""
from __future__ import annotations

import time


ALLOWED_ADMINS = frozenset({
    "baratha@datapattern.ai",
    "mirthular@datapattern.ai",
})
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCK_SECONDS = 900
SESSION_TTL_SECONDS = 43_200
AUTH_SESSION_VERSION = "employee-id-login-v1"


def _email(value: str) -> str:
    return value.strip().lower()


def _get_uc_connection():
    from src.database import uc_connection as uc

    return uc


def authenticate(email: str, employee_id: str) -> bool:
    """Authenticate only the two approved emails against their employee record."""
    email = _email(email)
    if email not in ALLOWED_ADMINS or not employee_id.strip():
        return False

    matched_id = _get_uc_connection().fetchval(
        "SELECT employee_id FROM employees "
        "WHERE lower(email) = ? AND employee_id = ?",
        [email, employee_id.strip()],
    )
    return matched_id is not None and str(matched_id).strip() == employee_id.strip()


def render_login(st) -> bool:
    """Render the restricted login and return whether this session is authenticated."""
    email = st.session_state.get("authenticated_email")
    authenticated_at = int(st.session_state.get("authenticated_at", 0) or 0)
    if (email in ALLOWED_ADMINS
            and st.session_state.get("auth_session_version") == AUTH_SESSION_VERSION
            and int(time.time()) - authenticated_at < SESSION_TTL_SECONDS):
        return True
    st.session_state.pop("authenticated_email", None)
    st.session_state.pop("authenticated_at", None)
    st.session_state.pop("auth_session_version", None)

    locked_until = int(st.session_state.get("login_locked_until", 0) or 0)
    st.title("Employeelens HR Login")
    st.caption(
        "Only the approved DataPattern HR administrator accounts can sign in."
    )
    if locked_until > int(time.time()):
        st.error("Too many failed attempts. Please wait 15 minutes and try again.")
        return False
    if locked_until:
        st.session_state.login_failed_attempts = 0
        st.session_state.login_locked_until = 0

    with st.form("admin_login"):
        email_input = st.text_input("Approved DataPattern email")
        employee_id = st.text_input("Password (your employee ID)", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        try:
            if authenticate(email_input, employee_id):
                st.session_state.authenticated_email = _email(email_input)
                st.session_state.authenticated_at = int(time.time())
                st.session_state.auth_session_version = AUTH_SESSION_VERSION
                st.session_state.login_failed_attempts = 0
                st.session_state.pop("login_locked_until", None)
                st.rerun()
            attempts = int(st.session_state.get("login_failed_attempts", 0)) + 1
            if attempts >= MAX_LOGIN_ATTEMPTS:
                st.session_state.login_failed_attempts = 0
                st.session_state.login_locked_until = (
                    int(time.time()) + LOGIN_LOCK_SECONDS
                )
                st.error("Too many failed attempts. Please wait 15 minutes and try again.")
            else:
                st.session_state.login_failed_attempts = attempts
                st.error("Sign-in failed. Check your email and employee ID.")
        except Exception as exc:
            st.error(
                f"Sign-in is unavailable ({type(exc).__name__}). "
                "Check the Unity Catalog connection and employee-table permissions."
            )
    return False
