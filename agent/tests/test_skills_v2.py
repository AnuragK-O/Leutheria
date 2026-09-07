import json
import tempfile
from pathlib import Path
import unittest

from agent.core.autonomy_policy import AutonomyThresholds, calculate_autonomy_tier
from agent.core.skill_learning_v2 import (
    export_skill_package,
    finalize_learned_skill,
    import_skill_package,
    infer_permissions_and_risk,
)
from agent.skills.retrieval import find_matching_skill
from agent.skills.schema import SkillManifest


class TestSkillsV2(unittest.TestCase):
    def test_skill_manifest_serialization_and_validation(self):
        manifest = SkillManifest(
            name="create_react_project",
            description="Create and initialize a React project",
            parameters={
                "project_name": {"type": "string", "description": "Project name", "required": True},
                "dest_dir": {"type": "path", "description": "Destination directory", "required": False},
            },
            requirements={"commands": ["node", "npm"]},
            permissions=["filesystem:create", "terminal:execute"],
            risk_level="medium",
            steps=[
                {"tool": "create_folder", "args": {"path": "{dest_dir}/{project_name}"}},
                {"tool": "run_command", "args": {"cmd": "npm create vite@latest {project_name}"}},
            ],
            validation=[
                {"tool": "list_files", "args": {"path": "{dest_dir}/{project_name}"}},
            ],
        )

        errors = manifest.validate()
        self.assertEqual(len(errors), 0)

        # JSON round-trip
        json_str = manifest.to_json()
        restored = SkillManifest.from_json(json_str)
        self.assertEqual(restored.name, "create_react_project")
        self.assertEqual(len(restored.steps), 2)
        self.assertIn("project_name", restored.parameters)

        # Test invalid skill validation
        bad_manifest = SkillManifest(name="bad name with spaces!", description="", steps=[])
        bad_errors = bad_manifest.validate()
        self.assertGreater(len(bad_errors), 0)

    def test_autonomy_policy_calculation(self):
        # 0 runs -> guide
        tier_0 = calculate_autonomy_tier(run_count=0, success_count=0, intervention_count=0)
        self.assertEqual(tier_0, "guide")

        # 3 runs, 3 successes, 0 interventions -> copilot
        tier_3 = calculate_autonomy_tier(run_count=3, success_count=3, intervention_count=0)
        self.assertEqual(tier_3, "copilot")

        # 6 runs, 6 successes, 0 interventions -> autopilot eligible
        tier_6 = calculate_autonomy_tier(run_count=6, success_count=6, intervention_count=0)
        self.assertEqual(tier_6, "autopilot")

        # 6 runs, but 3 failures (50% success rate) -> copilot
        tier_low_success = calculate_autonomy_tier(run_count=6, success_count=3, intervention_count=0)
        self.assertEqual(tier_low_success, "copilot")

        # High risk action never gets autopilot
        tier_high_risk = calculate_autonomy_tier(run_count=10, success_count=10, intervention_count=0, risk_level="high")
        self.assertEqual(tier_high_risk, "copilot")

    def test_semantic_retrieval(self):
        skill = SkillManifest(
            name="create_react_project",
            description="Create a new React project and initialize development environment",
            parameters={
                "project_name": {"type": "string", "required": True},
                "folder": {"type": "string", "required": False},
            },
            steps=[{"tool": "create_folder", "args": {"path": "~/Projects"}}],
        )

        query = "Create a React project called BlueSea in Projects"
        match = find_matching_skill(query, [skill])
        self.assertIsNotNone(match)
        self.assertEqual(match.skill.name, "create_react_project")
        self.assertGreater(match.confidence, 0.4)
        self.assertEqual(match.extracted_params.get("project_name"), "BlueSea")

    def test_import_and_export_skill_package(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_path = Path(tmpdir) / "test_skill.leutheria-skill"
            skill = SkillManifest(
                name="exportable_skill",
                description="A test exportable skill",
                steps=[{"tool": "list_files", "args": {"path": "~/Desktop"}}],
            )

            export_skill_package(skill, pkg_path)
            self.assertTrue(pkg_path.exists())

            # Read and import
            imported = import_skill_package(pkg_path.read_text())
            self.assertEqual(imported.name, "exportable_skill")
            self.assertEqual(imported.autonomy_tier, "guide")  # Reset to Guide for safety
            self.assertEqual(imported.provenance.get("origin"), "imported")

            # Corrupted package test
            with self.assertRaises(ValueError):
                import_skill_package("not valid json at all")


if __name__ == "__main__":
    unittest.main()
