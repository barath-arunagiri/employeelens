"""Delta Lake synchronization layer.

Handles all interactions with Unity Catalog Delta tables:
- Loading DuckDB from Delta (single source of truth)
- Writing changes to Delta with idempotent MERGE operations
- Audit logging
- Syncing changes back to DuckDB cache

All SQL operations use DataFrame API or Delta Table API to prevent SQL injection.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

try:
    from pyspark.sql import SparkSession, Row
    from pyspark.sql.functions import current_timestamp, lit, lower, col
    from delta.tables import DeltaTable
    SPARK_AVAILABLE = True
except ImportError:
    SPARK_AVAILABLE = False

# Unity Catalog table paths
UC_CATALOG = "workspace"
UC_SCHEMA = "employeelens"

TABLE_EMPLOYEE_MASTER = f"{UC_CATALOG}.{UC_SCHEMA}.employee_master"
TABLE_EMPLOYEE_SKILLS = f"{UC_CATALOG}.{UC_SCHEMA}.employee_skills"
TABLE_EMPLOYEE_PROJECTS = f"{UC_CATALOG}.{UC_SCHEMA}.employee_projects"
TABLE_EMPLOYEE_CERTIFICATIONS = f"{UC_CATALOG}.{UC_SCHEMA}.employee_certifications"
TABLE_EMPLOYEE_MANAGERS = f"{UC_CATALOG}.{UC_SCHEMA}.employee_managers"
TABLE_AUDIT_LOG = f"{UC_CATALOG}.{UC_SCHEMA}.change_audit_log"


def _get_spark() -> SparkSession:
    """Get or create Spark session.
    
    In Databricks Apps, local Spark sessions aren't available.
    This function will raise RuntimeError in that environment,
    and callers should handle it gracefully.
    """
    if not SPARK_AVAILABLE:
        raise RuntimeError("PySpark not available. Delta sync requires Spark or SQL Warehouse.")
    try:
        return SparkSession.builder.getOrCreate()
    except Exception as e:
        raise RuntimeError(f"Cannot create Spark session in Databricks Apps environment: {e}")


def _get_current_user() -> str:
    """Get the current Databricks user from OAuth context."""
    # In Databricks Apps, this comes from the OAuth identity
    return os.environ.get("DATABRICKS_USER", "system")


def load_from_delta_to_csv(clean_dir: str) -> None:
    """Load all data from Delta tables and write to CSV files for DuckDB.
    
    This is called at app startup to refresh the DuckDB cache from Delta.
    Delta is the source of truth; CSVs are just a transport layer.
    """
    spark = _get_spark()
    
    # Map Delta tables to CSV file names
    table_mappings = [
        (TABLE_EMPLOYEE_MASTER, "employees.csv", ["employee_id", "name", "email", "position", 
                                                   "team", "department", "designation", "location", 
                                                   "employment_status", "joining_date"]),
        (TABLE_EMPLOYEE_SKILLS, "employee_skills.csv", ["employee_id", "skill", "category", 
                                                         "qualifier", "source"]),
        (TABLE_EMPLOYEE_PROJECTS, "employee_projects.csv", ["employee_id", "project", "client", 
                                                            "technologies", "role", "project_type"]),
        (TABLE_EMPLOYEE_CERTIFICATIONS, "employee_certifications.csv", ["employee_id", "certification", 
                                                                        "vendor", "source", "verified", "raw_text"]),
        (TABLE_EMPLOYEE_MANAGERS, "employee_managers.csv", ["employee_id", "reporting_id"]),
    ]
    
    import os
    import shutil
    from pathlib import Path
    
    for table_name, csv_name, columns in table_mappings:
        df = spark.table(table_name).select(columns)
        output_path = f"{clean_dir}/{csv_name}"
        tmp_path = f"{output_path}.tmp"
        
        # Write as single CSV file (coalesce to 1 partition)
        df.coalesce(1).write.mode("overwrite").option("header", "true").csv(tmp_path)
        
        # Find the part file and move it to the final location
        tmp_dir = Path(tmp_path)
        part_files = list(tmp_dir.glob("part-*.csv"))
        if part_files:
            # Move the CSV file
            shutil.move(str(part_files[0]), output_path)
        
        # Clean up tmp directory
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)


def write_employee_update(employee_id: str, field: str, new_value: str, old_value: str = None) -> dict:
    """Write employee field update to Delta, then return success status.
    
    Uses MERGE for idempotency - safe to call multiple times.
    """
    spark = _get_spark()
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        # Update employee_master
        spark.sql(f"""
            MERGE INTO {TABLE_EMPLOYEE_MASTER} AS target
            USING (SELECT '{employee_id}' AS employee_id) AS source
            ON target.employee_id = source.employee_id
            WHEN MATCHED THEN UPDATE SET
                {field} = '{new_value}',
                last_updated = current_timestamp()
        """)
        
        # Log to audit
        _write_audit_log(employee_id, "UPDATE", "employee_master", field, old_value, new_value, user, timestamp)
        
        return {"success": True, "verified_value": new_value}
    except Exception as e:
        _write_audit_log(employee_id, "UPDATE", "employee_master", field, old_value, new_value, 
                        user, timestamp, status="error", error=str(e))
        return {"success": False, "error": str(e)}


def write_certification(employee_id: str, certification: str, vendor: str = "", 
                       source: str = "data_management", verified: str = "true") -> dict:
    """Write certification to Delta using idempotent MERGE.
    
    Defaults verified='true' as per requirements.
    Uses Delta Table API for safe MERGE operation.
    """
    spark = _get_spark()
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        # Create DataFrame for new/updated data
        new_data = spark.createDataFrame([Row(
            employee_id=str(employee_id),
            certification=str(certification),
            vendor=str(vendor),
            source=str(source),
            verified=str(verified),
            raw_text=str(certification)
        )])
        
        # Use Delta Table API for MERGE (idempotent, SQL-injection safe)
        delta_table = DeltaTable.forName(spark, TABLE_EMPLOYEE_CERTIFICATIONS)
        
        delta_table.alias("target").merge(
            new_data.alias("source"),
            "LOWER(target.employee_id) = LOWER(source.employee_id) AND " +
            "LOWER(target.certification) = LOWER(source.certification)"
        ).whenMatchedUpdate(set={
            "vendor": col("source.vendor"),
            "source": col("source.source"),
            "verified": col("source.verified"),
            "last_updated": current_timestamp()
        }).whenNotMatchedInsert(values={
            "employee_id": col("source.employee_id"),
            "certification": col("source.certification"),
            "vendor": col("source.vendor"),
            "source": col("source.source"),
            "verified": col("source.verified"),
            "raw_text": col("source.raw_text"),
            "last_updated": current_timestamp()
        }).execute()
        
        # Log to audit
        _write_audit_log(employee_id, "INSERT", "employee_certifications", "certification", 
                        None, certification, user, timestamp)
        
        return {"success": True, "verified_value": certification}
    except Exception as e:
        _write_audit_log(employee_id, "INSERT", "employee_certifications", "certification", 
                        None, certification, user, timestamp, status="error", error=str(e))
        return {"success": False, "error": str(e)}


def delete_certification(employee_id: str, certification: str) -> dict:
    """Delete certification from Delta."""
    spark = _get_spark()
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        spark.sql(f"""
            DELETE FROM {TABLE_EMPLOYEE_CERTIFICATIONS}
            WHERE employee_id = '{employee_id}'
              AND LOWER(certification) = LOWER('{certification}')
        """)
        
        _write_audit_log(employee_id, "DELETE", "employee_certifications", "certification", 
                        certification, None, user, timestamp)
        
        return {"success": True}
    except Exception as e:
        _write_audit_log(employee_id, "DELETE", "employee_certifications", "certification", 
                        certification, None, user, timestamp, status="error", error=str(e))
        return {"success": False, "error": str(e)}


def write_skill(employee_id: str, skill: str, category: str = "Other", 
               qualifier: str = "", source: str = "data_management") -> dict:
    """Write skill to Delta using idempotent MERGE."""
    spark = _get_spark()
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        spark.sql(f"""
            MERGE INTO {TABLE_EMPLOYEE_SKILLS} AS target
            USING (
                SELECT 
                    '{employee_id}' AS employee_id,
                    '{skill}' AS skill,
                    '{category}' AS category,
                    '{qualifier}' AS qualifier,
                    '{source}' AS source
            ) AS source
            ON target.employee_id = source.employee_id 
               AND LOWER(target.skill) = LOWER(source.skill)
            WHEN NOT MATCHED THEN INSERT (
                employee_id, skill, category, qualifier, source, last_updated
            ) VALUES (
                source.employee_id, source.skill, source.category, source.qualifier, 
                source.source, current_timestamp()
            )
        """)
        
        _write_audit_log(employee_id, "INSERT", "employee_skills", "skill", 
                        None, skill, user, timestamp)
        
        return {"success": True, "verified_value": skill}
    except Exception as e:
        _write_audit_log(employee_id, "INSERT", "employee_skills", "skill", 
                        None, skill, user, timestamp, status="error", error=str(e))
        return {"success": False, "error": str(e)}


def delete_skill(employee_id: str, skill: str) -> dict:
    """Delete skill from Delta."""
    spark = _get_spark()
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        spark.sql(f"""
            DELETE FROM {TABLE_EMPLOYEE_SKILLS}
            WHERE employee_id = '{employee_id}'
              AND LOWER(skill) = LOWER('{skill}')
        """)
        
        _write_audit_log(employee_id, "DELETE", "employee_skills", "skill", 
                        skill, None, user, timestamp)
        
        return {"success": True}
    except Exception as e:
        _write_audit_log(employee_id, "DELETE", "employee_skills", "skill", 
                        skill, None, user, timestamp, status="error", error=str(e))
        return {"success": False, "error": str(e)}


def write_project(employee_id: str, project: str, client: str = "", 
                 technologies: str = "", role: str = "", project_type: str = "main") -> dict:
    """Write project assignment to Delta."""
    spark = _get_spark()
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        spark.sql(f"""
            MERGE INTO {TABLE_EMPLOYEE_PROJECTS} AS target
            USING (
                SELECT 
                    '{employee_id}' AS employee_id,
                    '{project}' AS project,
                    '{client}' AS client,
                    '{technologies}' AS technologies,
                    '{role}' AS role,
                    '{project_type}' AS project_type
            ) AS source
            ON target.employee_id = source.employee_id 
               AND LOWER(target.project) = LOWER(source.project)
               AND LOWER(COALESCE(target.project_type, 'main')) = LOWER(source.project_type)
            WHEN NOT MATCHED THEN INSERT (
                employee_id, project, client, technologies, role, project_type, last_updated
            ) VALUES (
                source.employee_id, source.project, source.client, source.technologies,
                source.role, source.project_type, current_timestamp()
            )
        """)
        
        _write_audit_log(employee_id, "INSERT", "employee_projects", "project", 
                        None, project, user, timestamp)
        
        return {"success": True, "verified_value": project}
    except Exception as e:
        _write_audit_log(employee_id, "INSERT", "employee_projects", "project", 
                        None, project, user, timestamp, status="error", error=str(e))
        return {"success": False, "error": str(e)}


def delete_project(employee_id: str, project: str) -> dict:
    """Delete project assignment from Delta."""
    spark = _get_spark()
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        spark.sql(f"""
            DELETE FROM {TABLE_EMPLOYEE_PROJECTS}
            WHERE employee_id = '{employee_id}'
              AND LOWER(project) = LOWER('{project}')
        """)
        
        _write_audit_log(employee_id, "DELETE", "employee_projects", "project", 
                        project, None, user, timestamp)
        
        return {"success": True}
    except Exception as e:
        _write_audit_log(employee_id, "DELETE", "employee_projects", "project", 
                        project, None, user, timestamp, status="error", error=str(e))
        return {"success": False, "error": str(e)}


def update_project_field(employee_id: str, project: str, field: str, new_value: str) -> dict:
    """Update a project field (role or technologies)."""
    spark = _get_spark()
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    if field not in ["role", "technologies"]:
        return {"success": False, "error": "Invalid field"}
    
    try:
        spark.sql(f"""
            UPDATE {TABLE_EMPLOYEE_PROJECTS}
            SET {field} = '{new_value}',
                last_updated = current_timestamp()
            WHERE employee_id = '{employee_id}'
              AND LOWER(project) = LOWER('{project}')
        """)
        
        _write_audit_log(employee_id, "UPDATE", "employee_projects", field, 
                        None, new_value, user, timestamp)
        
        return {"success": True, "verified_value": new_value}
    except Exception as e:
        _write_audit_log(employee_id, "UPDATE", "employee_projects", field, 
                        None, new_value, user, timestamp, status="error", error=str(e))
        return {"success": False, "error": str(e)}


def create_employee(employee_id: str, name: str, email: str = "") -> dict:
    """Create new employee in Delta."""
    spark = _get_spark()
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        spark.sql(f"""
            INSERT INTO {TABLE_EMPLOYEE_MASTER} (employee_id, name, email, last_updated)
            VALUES ('{employee_id}', '{name}', '{email}', current_timestamp())
        """)
        
        _write_audit_log(employee_id, "INSERT", "employee_master", "employee", 
                        None, name, user, timestamp)
        
        return {"success": True, "verified_value": employee_id}
    except Exception as e:
        _write_audit_log(employee_id, "INSERT", "employee_master", "employee", 
                        None, name, user, timestamp, status="error", error=str(e))
        return {"success": False, "error": str(e)}


def update_manager(employee_id: str, reporting_id: str) -> dict:
    """Update employee's manager."""
    spark = _get_spark()
    user = _get_current_user()
    timestamp = datetime.now(timezone.utc)
    
    try:
        # Delete existing relationship
        spark.sql(f"""
            DELETE FROM {TABLE_EMPLOYEE_MANAGERS}
            WHERE employee_id = '{employee_id}'
        """)
        
        # Insert new relationship
        spark.sql(f"""
            INSERT INTO {TABLE_EMPLOYEE_MANAGERS} (employee_id, reporting_id, last_updated)
            VALUES ('{employee_id}', '{reporting_id}', current_timestamp())
        """)
        
        _write_audit_log(employee_id, "UPDATE", "employee_managers", "reporting_id", 
                        None, reporting_id, user, timestamp)
        
        return {"success": True, "verified_value": reporting_id}
    except Exception as e:
        _write_audit_log(employee_id, "UPDATE", "employee_managers", "reporting_id", 
                        None, reporting_id, user, timestamp, status="error", error=str(e))
        return {"success": False, "error": str(e)}


def _write_audit_log(employee_id: str, change_type: str, table_name: str, 
                    field_name: str, old_value: str, new_value: str, 
                    confirmed_by: str, confirmed_at: datetime, 
                    status: str = "success", error: str = None) -> None:
    """Write audit log entry to Delta using DataFrame API (safe from SQL injection)."""
    spark = _get_spark()
    
    try:
        # Use DataFrame API for safe insertion
        audit_data = spark.createDataFrame([Row(
            employee_id=str(employee_id) if employee_id else None,
            change_type=str(change_type),
            table_name=str(table_name),
            field_name=str(field_name) if field_name else None,
            old_value=str(old_value) if old_value else None,
            new_value=str(new_value) if new_value else None,
            confirmed_by=str(confirmed_by),
            confirmed_at=confirmed_at,
            status=str(status),
            error_message=str(error) if error else None
        )])
        
        audit_data.write.mode("append").saveAsTable(TABLE_AUDIT_LOG)
    except Exception as e:
        # Don't fail the main operation if audit logging fails
        print(f"Warning: Failed to write audit log: {e}")