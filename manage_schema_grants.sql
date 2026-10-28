-- Employeelens — least-privilege Unity Catalog grants (Phase 1)
--
-- REVIEW ONLY: do not run until you have approved it. Not executed by the app
-- or by the assistant.
--
-- Principal: the Employeelens app's service principal
--   display name: app-3eeuv8 employeelens
--   id (application_id): ab5b0cb3-9f86-40ee-9117-202fa8ecda67
--
-- Current state (verified 2026-10-14 via the Grants API):
--   * `account users` hold broad schema-level grants on workspace.employeelens:
--     CREATE_TABLE, CREATE_VOLUME, EXECUTE, MODIFY, READ_VOLUME, SELECT,
--     USE_CONNECTION, USE_SCHEMA
--   * the app SP already holds schema-level MODIFY + SELECT + USE_SCHEMA
--   * two personal accounts hold ALL PRIVILEGES (yours — left untouched)
--
-- This file replaces the account-users grants with exactly what the app's
-- service principal needs: USE CATALOG, USE SCHEMA, SELECT on all tables,
-- and MODIFY only on the tables the app writes.

-- 1) Remove the schema-wide grants to account users
REVOKE CREATE TABLE ON SCHEMA workspace.employeelens FROM `account users`;
REVOKE CREATE VOLUME ON SCHEMA workspace.employeelens FROM `account users`;
REVOKE EXECUTE ON SCHEMA workspace.employeelens FROM `account users`;
REVOKE MODIFY ON SCHEMA workspace.employeelens FROM `account users`;
REVOKE READ VOLUME ON SCHEMA workspace.employeelens FROM `account users`;
REVOKE SELECT ON SCHEMA workspace.employeelens FROM `account users`;
REVOKE USE CONNECTION ON SCHEMA workspace.employeelens FROM `account users`;
REVOKE USE SCHEMA ON SCHEMA workspace.employeelens FROM `account users`;

-- 2) Grant the app service principal exactly what it needs
GRANT USE CATALOG ON CATALOG workspace TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
GRANT USE SCHEMA ON SCHEMA workspace.employeelens TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;

-- Reads: all business tables (the app's search, profile, and audit-history
-- tools all read through the same service principal)
GRANT SELECT ON TABLE workspace.employeelens.employee_master TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
GRANT SELECT ON TABLE workspace.employeelens.employee_skills TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
GRANT SELECT ON TABLE workspace.employeelens.employee_projects TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
GRANT SELECT ON TABLE workspace.employeelens.employee_certifications TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
GRANT SELECT ON TABLE workspace.employeelens.employee_managers TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
GRANT SELECT ON TABLE workspace.employeelens.employee_vendor_ownership TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
-- audit history tool reads this table (the free-form run_sql tool cannot —
-- it is excluded by the SQL guard)
GRANT SELECT ON TABLE workspace.employeelens.change_audit_log TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;

-- Writes: only the tables the app writes
GRANT MODIFY ON TABLE workspace.employeelens.employee_master TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
GRANT MODIFY ON TABLE workspace.employeelens.employee_skills TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
GRANT MODIFY ON TABLE workspace.employeelens.employee_projects TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
GRANT MODIFY ON TABLE workspace.employeelens.employee_certifications TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
GRANT MODIFY ON TABLE workspace.employeelens.employee_managers TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
-- change_audit_log: MODIFY is required because the app INSERTs an audit row
-- for every confirmed change (UC has no append-only grant). The application
-- code never deletes or updates audit rows.
GRANT MODIFY ON TABLE workspace.employeelens.change_audit_log TO `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;

-- 3) Optional tightening: replace the SP's existing schema-level grants with
--    the table-level grants above (uncomment after step 2 has been verified):
-- REVOKE MODIFY ON SCHEMA workspace.employeelens FROM `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;
-- REVOKE SELECT ON SCHEMA workspace.employeelens FROM `ab5b0cb3-9f86-40ee-9117-202fa8ecda67`;

-- NOTE: also review CATALOG-level grants (USE CATALOG on catalog `workspace`)
-- for `account users` separately — the Grants API shows schema-level only here.
