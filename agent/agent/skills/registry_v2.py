import functools
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.core.autonomy_policy import calculate_autonomy_tier
from agent.core.database import get_db_connection
from agent.core.trace import default_trace_manager
from agent.skills import backup_folder, quick_note, setup_project
from agent.skills.schema import SkillManifest
from agent.skills.template_skill import run_template

GENERATED_DIR = Path(__file__).resolve().parent / "generated"

# In-memory runtime map of active skills: name -> dict(manifest, fn)
SKILLS: Dict[str, Dict[str, Any]] = {}


def _init_builtin_skills():
    """Register built-in hand-crafted skills."""
    SKILLS["setup_project"] = {
        "fn": setup_project.run,
        "manifest": SkillManifest(
            name="setup_project",
            description=(
                "Create a new software project: make a folder, run `git init` inside it, "
                "and open it in VS Code."
            ),
            parameters={
                "name": {"type": "string", "description": "Project or folder name", "required": True},
                "base_dir": {"type": "string", "description": "Parent directory", "required": False},
            },
            requirements={"commands": ["git", "code"]},
            permissions=["filesystem:create", "terminal:execute"],
            risk_level="medium",
            autonomy_tier="copilot",
            provenance={"creator": "builtin"},
        ),
        "origin": "builtin",
    }

    SKILLS["quick_note"] = {
        "fn": quick_note.run,
        "manifest": SkillManifest(
            name="quick_note",
            description="Jot down a quick timestamped note and confirm with notification.",
            parameters={
                "text": {"type": "string", "description": "Note content", "required": True},
                "notes_path": {"type": "string", "description": "Notes file path", "required": False},
            },
            permissions=["filesystem:modify", "system:notify"],
            risk_level="low",
            autonomy_tier="autopilot",
            provenance={"creator": "builtin"},
        ),
        "origin": "builtin",
    }

    SKILLS["backup_folder"] = {
        "fn": backup_folder.run,
        "manifest": SkillManifest(
            name="backup_folder",
            description="Create a timestamped backup copy of a file or folder.",
            parameters={
                "path": {"type": "string", "description": "Path to back up", "required": True},
            },
            permissions=["filesystem:read", "filesystem:create"],
            risk_level="low",
            autonomy_tier="copilot",
            provenance={"creator": "builtin"},
        ),
        "origin": "builtin",
    }


def _sync_skill_to_db(manifest: SkillManifest, origin: str = "generated") -> None:
    """Ensure skill record exists and is synced in SQLite."""
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO skills (
                id, name, version, description, manifest_json, origin,
                risk_level, run_count, success_count, intervention_count,
                autonomy_tier, is_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                version = excluded.version,
                description = excluded.description,
                manifest_json = excluded.manifest_json,
                risk_level = excluded.risk_level,
                autonomy_tier = excluded.autonomy_tier,
                is_enabled = excluded.is_enabled
            """,
            (
                manifest.id,
                manifest.name,
                manifest.version,
                manifest.description,
                manifest.to_json(),
                origin,
                manifest.risk_level,
                manifest.run_count,
                manifest.success_count,
                manifest.intervention_count,
                manifest.autonomy_tier,
                1 if manifest.is_enabled else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _load_db_skill_stats():
    """Load run counts and autonomy tier from DB into memory."""
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT name, run_count, success_count, intervention_count, autonomy_tier FROM skills").fetchall()
        for r in rows:
            name = r["name"]
            if name in SKILLS and "manifest" in SKILLS[name]:
                m = SKILLS[name]["manifest"]
                m.run_count = r["run_count"]
                m.success_count = r["success_count"]
                m.intervention_count = r["intervention_count"]
                m.autonomy_tier = r["autonomy_tier"]
    finally:
        conn.close()


def _load_generated_file(path: Path) -> None:
    try:
        manifest = SkillManifest.from_json(path.read_text())
        SKILLS[manifest.name] = {
            "fn": functools.partial(run_template, manifest.to_dict()),
            "manifest": manifest,
            "origin": "generated",
            "path": str(path),
        }
        _sync_skill_to_db(manifest, origin="generated")
    except Exception:
        pass


def load_all_skills() -> None:
    _init_builtin_skills()
    for name, item in SKILLS.items():
        _sync_skill_to_db(item["manifest"], origin="builtin")

    if GENERATED_DIR.exists():
        for path in sorted(GENERATED_DIR.glob("*.json")):
            _load_generated_file(path)

    _load_db_skill_stats()


def register_skill(manifest_or_dict) -> None:
    """Save and register a new or updated skill."""
    if isinstance(manifest_or_dict, dict):
        manifest = SkillManifest.from_dict(manifest_or_dict)
    else:
        manifest = manifest_or_dict

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    file_path = GENERATED_DIR / f"{manifest.name}.json"
    file_path.write_text(manifest.to_json())

    SKILLS[manifest.name] = {
        "fn": functools.partial(run_template, manifest.to_dict()),
        "manifest": manifest,
        "origin": "generated",
        "path": str(file_path),
    }
    _sync_skill_to_db(manifest, origin="generated")


def delete_skill(name: str) -> Dict[str, Any]:
    skill = SKILLS.get(name)
    if not skill:
        return {"ok": False, "error": f"no such skill: {name}"}
    if skill.get("origin") == "builtin":
        return {"ok": False, "error": f"{name} is a built-in skill and cannot be deleted"}

    path_str = skill.get("path")
    if path_str:
        p = Path(path_str)
        if p.exists():
            p.unlink()

    SKILLS.pop(name, None)

    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM skills WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "name": name}


def record_skill_execution(name: str, task_id: str, success: bool, interventions: int, duration_ms: float = 0.0) -> None:
    """Update skill statistics and recompute autonomy tier."""
    skill = SKILLS.get(name)
    if not skill or "manifest" not in skill:
        return

    m: SkillManifest = skill["manifest"]
    m.run_count += 1
    if success:
        m.success_count += 1
    m.intervention_count += interventions

    new_tier = calculate_autonomy_tier(m.run_count, m.success_count, m.intervention_count, m.risk_level)
    m.autonomy_tier = new_tier

    conn = get_db_connection()
    try:
        conn.execute(
            """
            UPDATE skills SET
                run_count = run_count + 1,
                success_count = success_count + ?,
                intervention_count = intervention_count + ?,
                autonomy_tier = ?
            WHERE name = ?
            """,
            (1 if success else 0, interventions, new_tier, name),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_manifests() -> List[SkillManifest]:
    return [item["manifest"] for item in SKILLS.values() if "manifest" in item]


def anthropic_skill_schemas() -> List[Dict[str, Any]]:
    from agent.core import preferences

    schemas = []
    for name, skill in SKILLS.items():
        if not preferences.is_enabled(name):
            continue
        m: SkillManifest = skill.get("manifest")
        if m:
            schemas.append({
                "name": m.name,
                "description": m.description,
                "input_schema": m.to_input_schema(),
            })
    return schemas


# Initialize on import
load_all_skills()
