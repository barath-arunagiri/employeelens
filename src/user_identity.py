"""Trusted identity resolution for admin-only data-management actions."""
from __future__ import annotations

from collections.abc import Mapping


def get_verified_user_email(
    headers: Mapping[str, object],
    oidc_user: Mapping[str, object],
    *,
    trust_databricks_headers: bool = False,
) -> str:
    """Return a trusted Databricks Apps or verified OIDC email, if available."""
    if trust_databricks_headers:
        forwarded_email = (
            headers.get("x-forwarded-email")
            or headers.get("x-forwarded-user")
        )
        if isinstance(forwarded_email, str) and forwarded_email.strip():
            return forwarded_email.strip()

    if oidc_user.get("is_logged_in") is not True:
        return ""
    if oidc_user.get("email_verified") is not True:
        return ""

    email = oidc_user.get("email")
    return email.strip() if isinstance(email, str) else ""
