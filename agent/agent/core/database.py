import os
from pathlib import Path
import sqlite3
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = DATA_DIR / "leutheria.db"

_db_initialized = False


def get_db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def get_db_connection(db_file: Optional[Path] = None) -> sqlite3.Connection:
    target_path = db_file or get_db_path()
    conn = sqlite3.connect(str(target_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_file: Optional[Path] = None) -> None:
    """Initialize database tables and indexes."""
    global _db_initialized
    conn = get_db_connection(db_file)
    with conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            user_request TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            execution_mode TEXT DEFAULT 'copilot',
            os_platform TEXT,
            status TEXT DEFAULT 'in_progress',
            skill_id TEXT,
            skill_name TEXT,
            intervention_count INTEGER DEFAULT 0,
            correction_count INTEGER DEFAULT 0,
            duration_ms REAL DEFAULT 0,
            tokens_used INTEGER DEFAULT 0,
            cost_estimate REAL DEFAULT 0,
            outcome_summary TEXT
        );

        CREATE TABLE IF NOT EXISTS trace_steps (
            step_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            observation TEXT,
            reasoning TEXT,
            action_type TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            tool_args TEXT NOT NULL,
            risk_level TEXT,
            requires_confirmation INTEGER DEFAULT 0,
            user_intervened INTEGER DEFAULT 0,
            correction_id TEXT,
            result TEXT,
            success INTEGER DEFAULT 1,
            duration_ms REAL DEFAULT 0,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS corrections (
            correction_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            step_id TEXT,
            timestamp TEXT NOT NULL,
            proposed_action TEXT,
            correction_type TEXT,
            user_correction TEXT NOT NULL,
            context TEXT,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS skills (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            version TEXT NOT NULL,
            description TEXT,
            manifest_json TEXT NOT NULL,
            origin TEXT DEFAULT 'generated',
            risk_level TEXT DEFAULT 'low',
            run_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            intervention_count INTEGER DEFAULT 0,
            correction_count INTEGER DEFAULT 0,
            last_run_at TEXT,
            last_success_at TEXT,
            autonomy_tier TEXT DEFAULT 'guide',
            is_enabled INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS skill_runs (
            run_id TEXT PRIMARY KEY,
            skill_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            success INTEGER NOT NULL,
            intervention_count INTEGER DEFAULT 0,
            correction_count INTEGER DEFAULT 0,
            duration_ms REAL DEFAULT 0,
            autonomy_mode_used TEXT,
            FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_trace_steps_task ON trace_steps(task_id);
        CREATE INDEX IF NOT EXISTS idx_corrections_task ON corrections(task_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_skill_runs_skill ON skill_runs(skill_id);
        """)
    conn.close()
    _db_initialized = True


# Initialize on import
init_db()
