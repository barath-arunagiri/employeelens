# Deploying Employeelens to Streamlit Community Cloud

The app can run on Streamlit Community Cloud, but it does not inherit the
Databricks Apps service principal, resource bindings, or viewer identity.
Configure a separate Databricks service principal and store its credentials in
Streamlit's secret store.

## 1. Deploy the app

1. Push this repository to GitHub and create a Community Cloud app from it.
2. Select `app/streamlit-app.py` as the **Main file path**.
3. Use a Python version supported by the pinned Streamlit Cloud runtime and
   install from `requirements.txt`.
4. Do not make the app public unless everyone who can open it is authorized to
   view the employee data. The app uses a shared service identity to query
   Databricks; Unity Catalog does not automatically apply each viewer's
   permissions.

## 2. Create and configure Databricks credentials

Create a dedicated service principal in the Databricks workspace and create an
OAuth secret for it. Add the service principal to the workspace. Do not reuse
the Databricks Apps service principal unless you deliberately want both
deployments to share the same identity and grants.

In the service principal's workspace, grant:

- `CAN_USE` on the SQL warehouse used by the app.
- `CAN_QUERY` on both model serving endpoints configured below (or on the
  selected endpoint if using one model).
- Unity Catalog permissions listed in the next section.

The Databricks workspace host is the HTTPS workspace URL, for example
`https://<workspace-id>.cloud.databricks.com`; do not include a trailing API
path. Use the service principal's **application/client ID**, not a user email.

## 3. Grant Unity Catalog access

Run the following as a catalog/schema owner or other authorized administrator,
replacing `<cloud-service-principal-application-id>` with the Community Cloud
service principal's application ID. These read grants cover the tables read by
the current app, including employee profiles, audit history, and vendor
ownership:

```sql
GRANT USE CATALOG ON CATALOG workspace
  TO `<cloud-service-principal-application-id>`;
GRANT USE SCHEMA ON SCHEMA workspace.employeelens
  TO `<cloud-service-principal-application-id>`;

GRANT SELECT ON TABLE workspace.employeelens.employee_master
  TO `<cloud-service-principal-application-id>`;
GRANT SELECT ON TABLE workspace.employeelens.employee_skills
  TO `<cloud-service-principal-application-id>`;
GRANT SELECT ON TABLE workspace.employeelens.employee_projects
  TO `<cloud-service-principal-application-id>`;
GRANT SELECT ON TABLE workspace.employeelens.employee_certifications
  TO `<cloud-service-principal-application-id>`;
GRANT SELECT ON TABLE workspace.employeelens.employee_managers
  TO `<cloud-service-principal-application-id>`;
GRANT SELECT ON TABLE workspace.employeelens.employee_vendor_ownership
  TO `<cloud-service-principal-application-id>`;
GRANT SELECT ON TABLE workspace.employeelens.change_audit_log
  TO `<cloud-service-principal-application-id>`;
```

The app's Data Management features write employee and audit tables. The app
requires both a verified viewer identity and an email in
`DATA_MANAGEMENT_ADMINS` before it allows a write. Grant `MODIFY` only on the
tables those paths write; do not grant schema-wide `MODIFY` to enable the
feature.

## 4. Set Streamlit secrets

In Community Cloud, open the app's **Settings > Secrets** and paste the
contents of [`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example),
replacing its placeholders. The app maps these secrets to the environment
variables read by the Databricks SDK and model client. Keep these values out of
Git, issues, and logs.

The model names default to `databricks-gpt-oss-120b` and
`databricks-gpt-oss-20b`. They need to be enabled and queryable in the
workspace. To change the primary model, set `model` to an available endpoint
name in the secrets. The app uses Databricks OAuth M2M via the service
principal; it does not use a personal access token.

## 5. Enable admin-only Data Management

Databricks Apps supplies a verified `x-forwarded-email` identity automatically.
Streamlit Community Cloud does not, so configure an OIDC sign-in provider to
enable Data Management there. For Google sign-in:

1. Create a Google OAuth client of type **Web application**.
2. Add `https://<your-app-subdomain>.streamlit.app/oauth2callback` as an
   authorized redirect URI for that client.
3. Add the following `[auth]` section to Community Cloud **Settings > Secrets**,
   using the actual client credentials and a strong random cookie secret:

   ```toml
   [auth]
   redirect_uri = "https://<your-app-subdomain>.streamlit.app/oauth2callback"
   cookie_secret = "<long-random-secret>"
   client_id = "<google-oauth-client-id>"
   client_secret = "<google-oauth-client-secret>"
   server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
   ```

4. In the existing `[databricks]` section, set `data_management_enabled = true`
   and `data_management_admins = "admin@example.com"` to the exact verified
   Google account email(s) allowed to make changes.
5. Confirm the Community Cloud service principal has the table-level `MODIFY`
   grants required by the app's write operations. Do not rely on the admin
   email allowlist alone; authorization also requires a verified sign-in.

The app accepts verified OIDC identities on Community Cloud. It trusts
forwarded identity headers only in Databricks Apps, where
`TRUST_DATABRICKS_FORWARDED_IDENTITY=true` is set in `app.yaml`. Users who are
not in the admin allowlist can still use the app but cannot manage data.

## 6. Verify the deployment

After redeploying, check that the app loads its Unity Catalog statistics and
that a simple employee search succeeds. Authentication or permission failures
usually indicate one of the following:

- Host, client ID, or client secret is missing or incorrect.
- The service principal is not assigned to the workspace.
- The configured warehouse ID is wrong or the principal lacks `CAN_USE`.
- A table grant or `USE CATALOG` / `USE SCHEMA` grant is missing.
- The model endpoint is unavailable or the principal lacks `CAN_QUERY`.

If Data Management remains locked on Community Cloud, verify that the `[auth]`
section is configured, the user signed in with Google, the Google account's
email is verified, and its email is present in `data_management_admins`.
