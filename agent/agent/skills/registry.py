"""Skills registry backwards-compatibility shim.

Re-exports everything from registry_v2 while maintaining existing APIs.
"""
from agent.skills.registry_v2 import (
    SKILLS,
    anthropic_skill_schemas,
    delete_skill,
    get_all_manifests,
    load_all_skills,
    record_skill_execution,
    register_skill,
)

__all__ = [
    "SKILLS",
    "register_skill",
    "delete_skill",
    "anthropic_skill_schemas",
    "load_all_skills",
    "record_skill_execution",
    "get_all_manifests",
]
