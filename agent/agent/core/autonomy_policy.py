from dataclasses import dataclass
from typing import Dict, Tuple

from agent.core.action import ExecutionMode, RiskLevel


@dataclass
class AutonomyThresholds:
    min_runs_for_copilot: int = 2
    min_runs_for_autopilot: int = 5
    min_success_rate_for_autopilot: float = 0.90
    max_interventions_for_autopilot: float = 0.20


DEFAULT_THRESHOLDS = AutonomyThresholds()


def calculate_autonomy_tier(
    run_count: int,
    success_count: int,
    intervention_count: int,
    risk_level: str = "medium",
    thresholds: AutonomyThresholds = DEFAULT_THRESHOLDS,
) -> str:
    """Calculate the earned autonomy tier for a skill.
    
    Tiers:
    - guide: Immature / unproven workflows (< min_runs_for_copilot).
    - copilot: Moderately tested workflows with good track record.
    - autopilot: High-volume, highly reliable workflows (>90% success, low interventions).
    """
    if run_count < thresholds.min_runs_for_copilot:
        return "guide"

    success_rate = (success_count / run_count) if run_count > 0 else 0.0
    avg_interventions = (intervention_count / run_count) if run_count > 0 else 0.0

    # High-risk skills (e.g. deleting files, arbitrary sudo) cap at copilot
    if risk_level == RiskLevel.HIGH.value:
        return "copilot"

    if (
        run_count >= thresholds.min_runs_for_autopilot
        and success_rate >= thresholds.min_success_rate_for_autopilot
        and avg_interventions <= thresholds.max_interventions_for_autopilot
    ):
        return "autopilot"

    return "copilot"


def resolve_task_execution_mode(
    requested_mode: str,
    skill_autonomy_tier: str,
    action_risk: str = "low",
) -> Tuple[ExecutionMode, str]:
    """Resolve the effective execution mode for a task step.
    
    Ensures safety boundaries:
    - If user explicitly requested GUIDE, always GUIDE.
    - If user explicitly requested COPILOT, never escalate to AUTOPILOT.
    - High-risk actions in AUTOPILOT step down to COPILOT if unconfirmed.
    """
    req = requested_mode.lower()
    if req == "guide":
        return ExecutionMode.GUIDE, "User selected Guide mode"

    if req == "autopilot":
        if skill_autonomy_tier == "autopilot":
            if action_risk == RiskLevel.HIGH.value:
                return ExecutionMode.COPILOT, "High-risk action requires Copilot confirmation"
            return ExecutionMode.AUTOPILOT, "Skill earned Autopilot status"
        return ExecutionMode.COPILOT, f"Skill status '{skill_autonomy_tier}' not yet eligible for Autopilot"

    return ExecutionMode.COPILOT, "Default Copilot supervision"
