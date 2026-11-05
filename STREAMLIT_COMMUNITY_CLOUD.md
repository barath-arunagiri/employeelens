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
4. The app uses a shared service identity to query Databricks; Unity Catalog
   does not automatically apply each viewer's permissions. App access is
   protected by the administrator login described below.

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

Bootstrap login access is initially granted to `baratha@datapattern.ai` and
`mirthular@datapattern.ai`. Active HR admins can then grant or revoke access
from **Data Management**. A grant requires an employee email and employee ID
that match a row in the employee table, and every access change requires
explicit confirmation. The last active admin cannot be revoked. Access grants
are stored in Unity Catalog, and successful changes are attributed to the
signed-in email in the audit log.

An employee ID is an identifier, not a strong secret; it may be visible to
app users or discoverable elsewhere. This login is a basic access gate, not
multi-factor authentication. Keep the Community Cloud app private and restrict
its viewers to trusted users. Use Google OIDC or another identity provider
instead if stronger authentication is required.

Create the admin access table and bootstrap the initial accounts once as a
catalog/schema owner:

```sql
CREATE TABLE IF NOT EXISTS workspace.employeelens.app_admin_access (
  email STRING NOT NULL,
  active BOOLEAN NOT NULL,
  granted_by STRING,
  granted_at TIMESTAMP,
  revoked_by STRING,
  revoked_at TIMESTAMP
)
USING DELTA;

MERGE INTO workspace.employeelens.app_admin_access AS target
USING (
  SELECT lower(email) AS email
  FROM workspace.employeelens.employee_master
  WHERE lower(email) IN (
    'baratha@datapattern.ai',
    'mirthular@datapattern.ai'
  )
) AS source
ON lower(target.email) = source.email
WHEN MATCHED THEN UPDATE SET
  active = true,
  granted_by = 'bootstrap',
  granted_at = current_timestamp(),
  revoked_by = NULL,
  revoked_at = NULL
WHEN NOT MATCHED THEN INSERT (
  email, active, granted_by, granted_at, revoked_by, revoked_at
) VALUES (
  source.email, true, 'bootstrap', current_timestamp(), NULL, NULL
);
```

Grant the Community Cloud service principal access to this table:

```sql
GRANT SELECT, MODIFY ON TABLE workspace.employeelens.app_admin_access
  TO `<cloud-service-principal-application-id>`;
```

The service principal must have `MODIFY` on only the tables written by the
confirmed proposal workflow:

```sql
GRANT MODIFY ON TABLE workspace.employeelens.employee_master
  TO `<cloud-service-principal-application-id>`;
GRANT MODIFY ON TABLE workspace.employeelens.employee_skills
  TO `<cloud-service-principal-application-id>`;
GRANT MODIFY ON TABLE workspace.employeelens.employee_projects
  TO `<cloud-service-principal-application-id>`;
GRANT MODIFY ON TABLE workspace.employeelens.employee_certifications
  TO `<cloud-service-principal-application-id>`;
GRANT MODIFY ON TABLE workspace.employeelens.employee_managers
  TO `<cloud-service-principal-application-id>`;
GRANT MODIFY ON TABLE workspace.employeelens.change_audit_log
  TO `<cloud-service-principal-application-id>`;
```

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

## 5. Enable authenticated Data Management

The example's `[databricks]` section sets `data_management_enabled = true`.
The login screen verifies an active admin grant and the matching employee ID.
Authorized admins can manage future login grants in Data Management.

The Databricks service principal needs the table-level `MODIFY` grants listed
above. Writes continue to use proposal review and explicit confirmation; the
audit actor is now the authenticated admin email.

## 6. Verify the deployment

After redeploying, check that the app loads its Unity Catalog statistics and
that a simple employee search succeeds. Authentication or permission failures
usually indicate one of the following:

- Host, client ID, or client secret is missing or incorrect.
- The service principal is not assigned to the workspace.
- The configured warehouse ID is wrong or the principal lacks `CAN_USE`.
- A table grant or `USE CATALOG` / `USE SCHEMA` grant is missing.
- The model endpoint is unavailable or the principal lacks `CAN_QUERY`.

If Data Management remains disabled, verify that
`data_management_enabled = true` is present in Community Cloud Secrets and
that the service principal has the required `MODIFY` grants.
