from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
from typing import Any, Dict, List, Optional
import uuid

from agent.core.action import RiskLevel, StructuredAction
from agent.core.database import get_db_connection


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CorrectionRecord:
    correction_id: str
    task_id: str
    step_id: Optional[str]
    timestamp: str
    proposed_action: Dict[str, Any]
    correction_type: str  # parameter_override, action_replacement, guidance
    user_correction: Any
    context: Optional[str] = None


@dataclass
class TraceStepRecord:
    step_id: str
    task_id: str
    step_index: int
    timestamp: str
    observation: str
    reasoning: str
    action_type: str
    tool_name: str
    tool_args: Dict[str, Any]
    risk_level: str
    requires_confirmation: bool
    user_intervened: bool = False
    correction_id: Optional[str] = None
    result: Optional[Any] = None
    success: bool = True
    duration_ms: float = 0.0


@dataclass
class TaskRecord:
    task_id: str
    created_at: str
    completed_at: Optional[str]
    user_request: str
    provider: str
    model: str
    execution_mode: str
    os_platform: str
    status: str = "in_progress"
    skill_id: Optional[str] = None
    skill_name: Optional[str] = None
    intervention_count: int = 0
    correction_count: int = 0
    duration_ms: float = 0.0
    tokens_used: int = 0
    cost_estimate: float = 0.0
    outcome_summary: Optional[str] = None


from contextlib import contextmanager

class TraceManager:
    """Manages persistent recording of tasks, actions, corrections, and analytics."""

    def __init__(self, db_file: Optional[Path] = None):
        self.db_file = db_file

    @contextmanager
    def _conn(self):
        conn = get_db_connection(self.db_file)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start_task(
        self,
        user_request: str,
        provider: str = "anthropic",
        model: str = "claude-opus-5",
        execution_mode: str = "copilot",
        skill_id: Optional[str] = None,
        skill_name: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> TaskRecord:
        tid = task_id or str(uuid.uuid4())
        created = _now_iso()
        os_name = f"{platform.system()} {platform.release()}"

        task = TaskRecord(
            task_id=tid,
            created_at=created,
            completed_at=None,
            user_request=user_request,
            provider=provider,
            model=model,
            execution_mode=execution_mode,
            os_platform=os_name,
            status="in_progress",
            skill_id=skill_id,
            skill_name=skill_name,
        )

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, created_at, user_request, provider, model,
                    execution_mode, os_platform, status, skill_id, skill_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.created_at,
                    task.user_request,
                    task.provider,
                    task.model,
                    task.execution_mode,
                    task.os_platform,
                    task.status,
                    task.skill_id,
                    task.skill_name,
                ),
            )
        return task

    def record_step_proposed(
        self,
        task_id: str,
        step_index: int,
        tool_name: str,
        tool_args: Dict[str, Any],
        action: StructuredAction,
        observation: str = "",
        reasoning: str = "",
    ) -> TraceStepRecord:
        step_id = str(uuid.uuid4())
        ts = _now_iso()

        step = TraceStepRecord(
            step_id=step_id,
            task_id=task_id,
            step_index=step_index,
            timestamp=ts,
            observation=observation,
            reasoning=reasoning,
            action_type=action.action_type,
            tool_name=tool_name,
            tool_args=tool_args,
            risk_level=action.risk_level.value,
            requires_confirmation=action.requires_confirmation,
        )

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO trace_steps (
                    step_id, task_id, step_index, timestamp, observation,
                    reasoning, action_type, tool_name, tool_args, risk_level,
                    requires_confirmation, user_intervened, success
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1)
                """,
                (
                    step.step_id,
                    step.task_id,
                    step.step_index,
                    step.timestamp,
                    step.observation,
                    step.reasoning,
                    step.action_type,
                    step.tool_name,
                    json.dumps(step.tool_args),
                    step.risk_level,
                    1 if step.requires_confirmation else 0,
                ),
            )
        return step

    def record_correction(
        self,
        task_id: str,
        step_id: Optional[str],
        proposed_action: Dict[str, Any],
        user_correction: Any,
        correction_type: str = "parameter_override",
        context: Optional[str] = None,
    ) -> CorrectionRecord:
        cid = str(uuid.uuid4())
        ts = _now_iso()

        record = CorrectionRecord(
            correction_id=cid,
            task_id=task_id,
            step_id=step_id,
            timestamp=ts,
            proposed_action=proposed_action,
            correction_type=correction_type,
            user_correction=user_correction,
            context=context,
        )

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO corrections (
                    correction_id, task_id, step_id, timestamp,
                    proposed_action, correction_type, user_correction, context
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.correction_id,
                    record.task_id,
                    record.step_id,
                    record.timestamp,
                    json.dumps(record.proposed_action),
                    record.correction_type,
                    json.dumps(record.user_correction) if isinstance(record.user_correction, (dict, list)) else str(record.user_correction),
                    record.context,
                ),
            )
            # Mark step as intervened
            if step_id:
                conn.execute(
                    "UPDATE trace_steps SET user_intervened = 1, correction_id = ? WHERE step_id = ?",
                    (cid, step_id),
                )
            # Increment task correction and intervention counters
            conn.execute(
                "UPDATE tasks SET intervention_count = intervention_count + 1, correction_count = correction_count + 1 WHERE task_id = ?",
                (task_id,),
            )
        return record

    def record_step_result(
        self,
        step_id: str,
        result: Any,
        success: bool = True,
        duration_ms: float = 0.0,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE trace_steps
                SET result = ?, success = ?, duration_ms = ?
                WHERE step_id = ?
                """,
                (
                    json.dumps(result) if isinstance(result, (dict, list)) else str(result),
                    1 if success else 0,
                    duration_ms,
                    step_id,
                ),
            )

    def complete_task(
        self,
        task_id: str,
        status: str = "completed",
        outcome_summary: Optional[str] = None,
        duration_ms: float = 0.0,
        tokens_used: int = 0,
        cost_estimate: float = 0.0,
    ) -> None:
        completed_at = _now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET completed_at = ?, status = ?, outcome_summary = ?,
                    duration_ms = ?, tokens_used = ?, cost_estimate = ?
                WHERE task_id = ?
                """,
                (
                    completed_at,
                    status,
                    outcome_summary,
                    duration_ms,
                    tokens_used,
                    cost_estimate,
                    task_id,
                ),
            )

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            return TaskRecord(**dict(row))

    def get_task_steps(self, task_id: str) -> List[TraceStepRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trace_steps WHERE task_id = ? ORDER BY step_index ASC", (task_id,)
            ).fetchall()
            steps = []
            for r in rows:
                d = dict(r)
                d["tool_args"] = json.loads(d["tool_args"]) if d.get("tool_args") else {}
                try:
                    d["result"] = json.loads(d["result"]) if d.get("result") else None
                except Exception:
                    pass
                d["requires_confirmation"] = bool(d.get("requires_confirmation"))
                d["user_intervened"] = bool(d.get("user_intervened"))
                d["success"] = bool(d.get("success"))
                steps.append(TraceStepRecord(**d))
            return steps

    def get_task_corrections(self, task_id: str) -> List[CorrectionRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM corrections WHERE task_id = ? ORDER BY timestamp ASC", (task_id,)
            ).fetchall()
            corrections = []
            for r in rows:
                d = dict(r)
                try:
                    d["proposed_action"] = json.loads(d["proposed_action"])
                except Exception:
                    pass
                try:
                    d["user_correction"] = json.loads(d["user_correction"])
                except Exception:
                    pass
                corrections.append(CorrectionRecord(**d))
            return corrections

    def get_recent_successful_tasks(self, limit: int = 20) -> List[TaskRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'completed' ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [TaskRecord(**dict(r)) for r in rows]

    def get_metrics(self) -> Dict[str, Any]:
        """Compute aggregate productivity and autonomy metrics."""
        with self._conn() as conn:
            total_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            completed_tasks = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'completed'"
            ).fetchone()[0]
            interventions = conn.execute(
                "SELECT SUM(intervention_count), SUM(correction_count) FROM tasks"
            ).fetchone()
            total_interventions = interventions[0] or 0
            total_corrections = interventions[1] or 0

            avg_duration = conn.execute(
                "SELECT AVG(duration_ms) FROM tasks WHERE status = 'completed'"
            ).fetchone()[0] or 0.0

            total_steps = conn.execute("SELECT COUNT(*) FROM trace_steps").fetchone()[0]
            avg_steps = (total_steps / total_tasks) if total_tasks > 0 else 0

            # Skill reuse rate
            tasks_with_skill = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE skill_id IS NOT NULL OR skill_name IS NOT NULL"
            ).fetchone()[0]

            # Autopilot completions (tasks with 0 interventions in copilot/autopilot)
            autonomous_tasks = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'completed' AND intervention_count = 0"
            ).fetchone()[0]

            completion_rate = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0.0
            skill_reuse_rate = (tasks_with_skill / total_tasks * 100) if total_tasks > 0 else 0.0
            autonomous_rate = (autonomous_tasks / completed_tasks * 100) if completed_tasks > 0 else 0.0

            return {
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "completion_rate_pct": round(completion_rate, 1),
                "total_steps": total_steps,
                "avg_steps_per_task": round(avg_steps, 2),
                "total_interventions": total_interventions,
                "total_corrections": total_corrections,
                "avg_duration_ms": round(avg_duration, 1),
                "skill_reuse_rate_pct": round(skill_reuse_rate, 1),
                "autonomous_completion_rate_pct": round(autonomous_rate, 1),
            }


# Singleton manager instance
default_trace_manager = TraceManager()
