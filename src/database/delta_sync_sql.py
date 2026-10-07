"""Delta Lake synchronization using Databricks SDK SQL Execution.

Works in Databricks Apps environment where Spark sessions aren't available.
Uses Databricks SDK's statement execution API for all Delta operations.
"""
from __future__ import annotations

import os
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import time

try:
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState
    SQL_AVAILABLE = True
except ImportError:
    SQL_AVAILABLE = False

# Unity Catalog table paths
UC_CATALOG = "workspace"
UC_SCHEMA = "employeelens"

TABLE_EMPLOYEE_MASTER = f"{UC_CATALOG}.{UC_SCHEMA}.employee_master"
TABLE_EMPLOYEE_SKILLS = f"{UC_CATALOG}.{UC_SCHEMA}.employee_skills"
TABLE_EMPLOYEE_PROJECTS = f"{UC_CATALOG}.{UC_SCHEMA}.employee_projects"
TABLE_EMPLOYEE_CERTIFICATIONS = f"{UC_CATALOG}.{UC_SCHEMA}.employee_certifications"
TABLE_EMPLOYEE_MANAGERS = f"{UC_CATALOG}.{UC_SCHEMA}.employee_managers"
TABLE_AUDIT_LOG = f"{UC_CATALOG}.{UC_SCHEMA}.change_audit_log"


def _get_sql_warehouse_id() -> str:
    """Get SQL warehouse ID from environment or find the first available one."""
    # Try environment variable first
    warehouse_id = os.environ.get("DATABRICKS_SQL_WAREHOUSE_ID")
    if warehouse_id:
        return warehouse_id
    
    # Find first available warehouse
    try:
        w = WorkspaceClient()
        warehouses = list(w.warehouses.list())
        if warehouses:
            return warehouses[0].id
    except Exception:
        pass
    
    raise RuntimeError("No SQL warehouse available. Set DATABRICKS_SQL_WAREHOUSE_ID.")


def _get_connection():
    """Get SQL warehouse connection."""
    if not SQL_AVAILABLE:
        raise RuntimeError("databricks-sql-connector not available")
    
    # In Databricks Apps, use WorkspaceClient which handles auth automatically
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.core import Config
    
    w = WorkspaceClient()
    cfg: Config = w.config
    
    # Get the workspace hostname from SDK config
    hostname = cfg.host
    if not hostname:
        raise RuntimeError("Cannot determine Databricks workspace hostname")
    
    # Remove https:// prefix if present
    if hostname.startswith("https://"):
        hostname = hostname[8:]
    if hostname.startswith("http://"):
        hostname = hostname[7:]
    
    # Use the token from the workspace client for SQL connection
    return sql.connect(
        server_hostname=hostname,
        http_path=f"/sql/1.0/warehouses/{_get_sql_warehouse_id()}",
        access_token=cfg.token().get("access_token")
    )


def _get_current_user() -> str:
    """Get current user from OAuth context."""
    return os.environ.get("DATABRICKS_USER", "system")


def load_from_delta_to_csv(clean_dir: str) -> None:
    """Load data from Delta tables and write to CSV files.
    
    This is called at app startup to refresh DuckDB cache from Delta.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    
    try:
        # Map Delta tables to CSV files and columns
        table_mappings = [
            (TABLE_EMPLOYEE_MASTER, "employees.csv", 
             ["employee_id", "name", "email", "position", "team", "department", 
              "designation", "location", "employment_status", "joining_date"]),
            (TABLE_EMPLOYEE_SKILLS, "employee_skills.csv", 
             ["employee_id", "skill", "category", "qualifier", "source"]),
            (TABLE_EMPLOYEE_PROJECTS, "employee_projects.csv", 
             ["employee_id", "project", "client", "technologies", "role", "project_type"]),
            (TABLE_EMPLOYEE_CERTIFICATIONS, "employee_certifications.csv", 
             ["employee_id", "certification", "vendor", "source", "verified", "raw_text"]),
            (TABLE_EMPLOYEE_MANAGERS, "employee_managers.csv", 
             ["employee_id", "reporting_id"]),
        ]
        
        Path(clean_dir).mkdir(parents=True, exist_ok=True)
        
        for table_name, csv_name, columns in table_mappings:
            # Query Delta table
            cursor.execute(f"SELECT {', '.join(columns)} FROM {table_name}")
            rows = cursor.fetchall()
            
            # Write to CSV
            csv_path = Path(clean_dir) / csv_name
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(columns)  # Header
                writer.writerows(rows)
    
    finally:
        cursor.close()
        conn.close()


def write_certification(employee_id: str, certification: str, vendor: str = "", 
                       source: str = "data_management", verified: str = "true") -> dict:
    """Write certification to Delta using SQL MERGE (idempotent)."""
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        
        try:
            # Use MERGE for idempotent upsert
            cursor.execute(f"""
                MERGE INTO {TABLE_EMPLOYEE_CERTIFICATIONS} AS target
                USING (
                    SELECT 
                        ? AS employee_id,
                        ? AS certification,
                        ? AS vendor,
                        ? AS source,
                        ? AS verified,
                        ? AS raw_text
                ) AS source
                ON LOWER(target.employee_id) = LOWER(source.employee_id)
                   AND LOWER(target.certification) = LOWER(source.certification)
                WHEN MATCHED THEN UPDATE SET
                    target.vendor = source.vendor,
                    target.source = source.source,
                    target.verified = source.verified,
                    target.last_updated = current_timestamp()
                WHEN NOT MATCHED THEN INSERT (
                    employee_id, certification, vendor, source, verified, raw_text, last_updated
                ) VALUES (
                    source.employee_id, source.certification, source.vendor, source.source,
                    source.verified, source.raw_text, current_timestamp()
                )
            """, (employee_id, certification, vendor, source, verified, certification))
            
            # Log to audit
            _write_audit_log(cursor, employee_id, "INSERT", "employee_certifications", 
                           "certification", None, certification, user, timestamp)
            
            conn.commit()
            return {"success": True, "verified_value": certification}
        
        finally:
            cursor.close()
            conn.close()
    
    except Exception as e:
        return {"success": False, "error": str(e)}


def delete_certification(employee_id: str, certification: str) -> dict:
    """Delete certification from Delta."""
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute(f"""
                DELETE FROM {TABLE_EMPLOYEE_CERTIFICATIONS}
                WHERE LOWER(employee_id) = LOWER(?)
                  AND LOWER(certification) = LOWER(?)
            """, (employee_id, certification))
            
            _write_audit_log(cursor, employee_id, "DELETE", "employee_certifications",
                           "certification", certification, None, user, timestamp)
            
            conn.commit()
            return {"success": True}
        
        finally:
            cursor.close()
            conn.close()
    
    except Exception as e:
        return {"success": False, "error": str(e)}


def write_employee_update(employee_id: str, field: str, new_value: str, old_value: str = None) -> dict:
    """Update employee field in Delta."""
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute(f"""
                UPDATE {TABLE_EMPLOYEE_MASTER}
                SET {field} = ?, last_updated = current_timestamp()
                WHERE employee_id = ?
            """, (new_value, employee_id))
            
            _write_audit_log(cursor, employee_id, "UPDATE", "employee_master",
                           field, old_value, new_value, user, timestamp)
            
            conn.commit()
            return {"success": True, "verified_value": new_value}
        
        finally:
            cursor.close()
            conn.close()
    
    except Exception as e:
        return {"success": False, "error": str(e)}


def write_skill(employee_id: str, skill: str, category: str = "Other",
               qualifier: str = "", source: str = "data_management") -> dict:
    """Write skill to Delta."""
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute(f"""
                MERGE INTO {TABLE_EMPLOYEE_SKILLS} AS target
                USING (
                    SELECT ? AS employee_id, ? AS skill, ? AS category, ? AS qualifier, ? AS source
                ) AS source
                ON LOWER(target.employee_id) = LOWER(source.employee_id)
                   AND LOWER(target.skill) = LOWER(source.skill)
                WHEN NOT MATCHED THEN INSERT (
                    employee_id, skill, category, qualifier, source, last_updated
                ) VALUES (
                    source.employee_id, source.skill, source.category, source.qualifier,
                    source.source, current_timestamp()
                )
            """, (employee_id, skill, category, qualifier, source))
            
            _write_audit_log(cursor, employee_id, "INSERT", "employee_skills",
                           "skill", None, skill, user, timestamp)
            
            conn.commit()
            return {"success": True, "verified_value": skill}
        
        finally:
            cursor.close()
            conn.close()
    
    except Exception as e:
        return {"success": False, "error": str(e)}


def delete_skill(employee_id: str, skill: str) -> dict:
    """Delete skill from Delta."""
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute(f"""
                DELETE FROM {TABLE_EMPLOYEE_SKILLS}
                WHERE LOWER(employee_id) = LOWER(?)
                  AND LOWER(skill) = LOWER(?)
            """, (employee_id, skill))
            
            _write_audit_log(cursor, employee_id, "DELETE", "employee_skills",
                           "skill", skill, None, user, timestamp)
            
            conn.commit()
            return {"success": True}
        
        finally:
            cursor.close()
            conn.close()
    
    except Exception as e:
        return {"success": False, "error": str(e)}


def _write_audit_log(cursor, employee_id: str, change_type: str, table_name: str,
                    field_name: str, old_value: Optional[str], new_value: Optional[str],
                    confirmed_by: str, confirmed_at: datetime,
                    status: str = "success", error: Optional[str] = None) -> None:
    """Write audit log entry to Delta."""
    try:
        cursor.execute(f"""
            INSERT INTO {TABLE_AUDIT_LOG} (
                employee_id, change_type, table_name, field_name,
                old_value, new_value, confirmed_by, confirmed_at, status, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            employee_id, change_type, table_name, field_name,
            old_value, new_value, confirmed_by, confirmed_at.isoformat(),
            status, error
        ))
    except Exception as e:
        # Don't fail the main operation if audit logging fails
        print(f"Warning: Failed to write audit log: {e}")


# Stub implementations for other operations (can be expanded later)
def write_project(employee_id: str, project: str, client: str = "",
                 technologies: str = "", role: str = "", project_type: str = "main") -> dict:
    return {"success": False, "error": "Not implemented yet"}

def delete_project(employee_id: str, project: str) -> dict:
    return {"success": False, "error": "Not implemented yet"}

def update_project_field(employee_id: str, project: str, field: str, new_value: str) -> dict:
    return {"success": False, "error": "Not implemented yet"}

def create_employee(employee_id: str, name: str, email: str = "") -> dict:
    return {"success": False, "error": "Not implemented yet"}

def update_manager(employee_id: str, reporting_id: str) -> dict:
    return {"success": False, "error": "Not implemented yet"}
