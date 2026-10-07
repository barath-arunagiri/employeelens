"""Simple Unity Catalog Delta reader using Databricks SDK.

Loads data from Unity Catalog Delta tables on app startup.
Uses SDK statement execution API which works in Databricks Apps.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import List, Tuple

try:
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

# Unity Catalog configuration
UC_CATALOG = "workspace"
UC_SCHEMA = "employeelens"


def _get_warehouse_id() -> str:
    """Get first available SQL warehouse."""
    if not SDK_AVAILABLE:
        raise RuntimeError("databricks-sdk not available")
    
    w = WorkspaceClient()
    warehouses = list(w.warehouses.list())
    
    if not warehouses:
        raise RuntimeError("No SQL warehouse available")
    
    return warehouses[0].id


def _execute_query(query: str) -> List[List]:
    """Execute SQL query and return results as list of rows."""
    w = WorkspaceClient()
    warehouse_id = _get_warehouse_id()
    
    response = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=query,
        wait_timeout="60s"
    )
    
    if response.status.state == StatementState.SUCCEEDED:
        # Return data rows (skip header)
        return response.result.data_array if response.result and response.result.data_array else []
    else:
        error_msg = response.status.error.message if response.status.error else "Unknown error"
        raise RuntimeError(f"Query failed: {error_msg}")


def load_from_delta_to_csv(clean_dir: str) -> None:
    """Load data from Unity Catalog Delta tables and export to CSV.
    
    This is called on app startup to refresh the DuckDB cache.
    """
    if not SDK_AVAILABLE:
        raise RuntimeError("databricks-sdk not available")
    
    Path(clean_dir).mkdir(parents=True, exist_ok=True)
    
    # Table configurations
    table_configs = [
        ("employee_master", "employees.csv",
         ["employee_id", "name", "email", "position", "team", "department",
          "designation", "location", "employment_status", "joining_date"]),
        
        ("employee_skills", "employee_skills.csv",
         ["employee_id", "skill", "category", "qualifier", "source"]),
        
        ("employee_projects", "employee_projects.csv",
         ["employee_id", "project", "client", "technologies", "role", "project_type"]),
        
        ("employee_certifications", "employee_certifications.csv",
         ["employee_id", "certification", "vendor", "source", "verified", "raw_text"]),
        
        ("employee_managers", "employee_managers.csv",
         ["employee_id", "reporting_id"]),
    ]
    
    for table_name, csv_name, columns in table_configs:
        full_table = f"{UC_CATALOG}.{UC_SCHEMA}.{table_name}"
        query = f"SELECT {', '.join(columns)} FROM {full_table}"
        
        # Execute query
        rows = _execute_query(query)
        
        # Write to CSV
        csv_path = Path(clean_dir) / csv_name
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(columns)  # Header
            writer.writerows(rows)  # Data
        
        print(f"Loaded {len(rows)} rows from {full_table} to {csv_name}")


def is_available() -> bool:
    """Check if Unity Catalog Delta reading is available."""
    if not SDK_AVAILABLE:
        return False
    
    try:
        _get_warehouse_id()
        return True
    except Exception:
        return False
