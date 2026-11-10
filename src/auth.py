"""HR administrator login and access management."""
from __future__ import annotations

import time


ADMIN_ACCESS_TABLE = "workspace.employeelens.app_admin_access"
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCK_SECONDS = 900
SESSION_TTL_SECONDS = 43_200
AUTH_SESSION_VERSION = "employee-id-login-v2"


def _email(value: str) -> str:
    return value.strip().lower()


def _get_uc_connection():
    from src.database import uc_connection as uc

    return uc


def authenticate(email: str, employee_id: str) -> bool:
    """Authenticate an active admin grant and its matching employee record."""
    email = _email(email)
    employee_id = employee_id.strip()
    if not email.endswith("@datapattern.ai") or not employee_id:
        return False

    matched_id = _get_uc_connection().fetchval(
        f"""
        SELECT e.employee_id
        FROM {ADMIN_ACCESS_TABLE} a
        JOIN employees e ON lower(e.email) = lower(a.email)
        WHERE lower(a.email) = ? AND a.active = true AND e.employee_id = ?
        """,
        [email, employee_id],
    )
    return matched_id is not None and str(matched_id).strip() == employee_id


def is_admin(email: str) -> bool:
    email = _email(email)
    if not email.endswith("@datapattern.ai"):
        return False
    result = _get_uc_connection().fetchval(
        f"SELECT email FROM {ADMIN_ACCESS_TABLE} "
        "WHERE lower(email) = ? AND active = true",
        [email],
    )
    return result is not None


def list_admins() -> list[dict]:
    rows = _get_uc_connection().query(
        f"""
        SELECT email, granted_by, granted_at
        FROM {ADMIN_ACCESS_TABLE}
        WHERE active = true
        ORDER BY lower(email)
        """
    )
    return rows.to_dict("records")


def _require_admin(actor_email: str) -> str:
    actor_email = _email(actor_email)
    if not is_admin(actor_email):
        raise PermissionError("Only an active admin can manage login access.")
    return actor_email


def _validate_employee(email: str, employee_id: str) -> str:
    email = _email(email)
    employee_id = employee_id.strip()
    if not email.endswith("@datapattern.ai"):
        raise ValueError("Use a @datapattern.ai employee email address.")
    if not employee_id:
        raise ValueError("Enter the employee ID.")
    match = _get_uc_connection().fetchval(
        "SELECT employee_id FROM employees "
        "WHERE lower(email) = ? AND employee_id = ?",
        [email, employee_id],
    )
    if match is None:
        raise ValueError("That email and employee ID do not match an employee record.")
    return email


def grant_admin_access(actor_email: str, email: str, employee_id: str) -> None:
    actor_email = _require_admin(actor_email)
    email = _validate_employee(email, employee_id)
    _get_uc_connection().execute(
        f"""
        MERGE INTO {ADMIN_ACCESS_TABLE} AS target
        USING (SELECT ? AS email, ? AS granted_by) AS source
        ON lower(target.email) = lower(source.email)
        WHEN MATCHED THEN UPDATE SET
            active = true,
            granted_by = source.granted_by,
            granted_at = current_timestamp(),
            revoked_by = NULL,
            revoked_at = NULL
        WHEN NOT MATCHED THEN INSERT (
            email, active, granted_by, granted_at, revoked_by, revoked_at
        ) VALUES (
            source.email, true, source.granted_by, current_timestamp(), NULL, NULL
        )
        """,
        [email, actor_email],
    )
    if not is_admin(email):
        raise RuntimeError("The admin access grant could not be verified.")


def revoke_admin_access(actor_email: str, email: str) -> None:
    actor_email = _require_admin(actor_email)
    email = _email(email)
    if not email.endswith("@datapattern.ai"):
        raise ValueError("Invalid employee email address.")

    uc = _get_uc_connection()
    active_email = uc.fetchval(
        f"SELECT email FROM {ADMIN_ACCESS_TABLE} "
        "WHERE lower(email) = ? AND active = true",
        [email],
    )
    if active_email is None:
        raise ValueError("That account does not have active login access.")
    admin_count = int(uc.fetchval(
        f"SELECT COUNT(*) FROM {ADMIN_ACCESS_TABLE} WHERE active = true"
    ) or 0)
    if admin_count <= 1:
        raise ValueError("The last active admin cannot be removed.")

    uc.execute(
        f"""
        UPDATE {ADMIN_ACCESS_TABLE}
        SET active = false, revoked_by = ?, revoked_at = current_timestamp()
        WHERE lower(email) = ? AND active = true
        """,
        [actor_email, email],
    )
    if is_admin(email):
        raise RuntimeError("The admin access revocation could not be verified.")


def render_login(st) -> bool:
    """Render the restricted login and return whether this session is authenticated."""
    email = st.session_state.get("authenticated_email")
    authenticated_at = int(st.session_state.get("authenticated_at", 0) or 0)
    if (email
            and st.session_state.get("auth_session_version") == AUTH_SESSION_VERSION
            and int(time.time()) - authenticated_at < SESSION_TTL_SECONDS):
        try:
            if is_admin(email):
                return True
            st.session_state.login_error = "Your admin access has been revoked."
        except Exception as exc:
            st.error(
                f"Could not verify admin access ({type(exc).__name__}). "
                "Check the Unity Catalog admin-access table and permissions."
            )
            return False
    st.session_state.pop("authenticated_email", None)
    st.session_state.pop("authenticated_at", None)
    st.session_state.pop("auth_session_version", None)

    st.markdown(
        """
        <style>
        .st-key-employeelens_login_panel {
            box-sizing: border-box;
            width: 100%;
            max-width: 440px;
            margin: clamp(1.5rem, 14vh, 7rem) auto 0;
        }
        .st-key-employeelens_login_panel h1 {
            font-size: 1.8rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key="employeelens_login_panel"):
        st.title("Employeelens HR Login")
        st.caption(
            "Sign in with an employee account that has been granted HR admin access."
        )
        if st.session_state.get("login_error"):
            st.warning(st.session_state.pop("login_error"))

        locked_until = int(st.session_state.get("login_locked_until", 0) or 0)
        if locked_until > int(time.time()):
            st.error("Too many failed attempts. Please wait 15 minutes and try again.")
            return False
        if locked_until:
            st.session_state.login_failed_attempts = 0
            st.session_state.login_locked_until = 0

        with st.form("admin_login"):
            email_input = st.text_input("DataPattern employee email")
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
                    st.error(
                        "Too many failed attempts. Please wait 15 minutes and try again."
                    )
                else:
                    st.session_state.login_failed_attempts = attempts
                    st.error("Sign-in failed. Check your email and employee ID.")
            except Exception as exc:
                st.error(
                    f"Sign-in is unavailable ({type(exc).__name__}). "
                    "Check the Unity Catalog connection and employee-table permissions."
                )
    return False
