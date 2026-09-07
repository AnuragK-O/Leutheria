import asyncio
import json
import tempfile
from pathlib import Path
import unittest

from agent.core.action import ExecutionMode
from agent.core.database import init_db
from agent.core.dispatcher import dispatch
from agent.core.skill_learning_v2 import finalize_learned_skill
from agent.core.trace import TraceManager
from agent.skills.registry_v2 import get_all_manifests, register_skill
from agent.skills.retrieval import find_matching_skill
from agent.skills.schema import SkillManifest


class TestIntegrationEndToEnd(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_integration.db"
        init_db(self.db_path)
        self.manager = TraceManager(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_end_to_end_flywheel(self):
        # 1. First Task: "Create a React project called BlueSea"
        task1 = self.manager.start_task(
            user_request="Create a React project called BlueSea in Projects",
            execution_mode="copilot",
        )

        # Step 1: create_folder
        step1 = self.manager.record_step_proposed(
            task_id=task1.task_id,
            step_index=0,
            tool_name="create_folder",
            tool_args={"path": "~/Projects/BlueSea"},
            action=type("MockAction", (), {"action_type": "create_folder", "risk_level": type("RL", (), {"value": "medium"}), "requires_confirmation": False})(),
        )
        self.manager.record_step_result(step1.step_id, {"ok": True}, success=True, duration_ms=50)

        # Step 2: run_command with a user correction!
        step2 = self.manager.record_step_proposed(
            task_id=task1.task_id,
            step_index=1,
            tool_name="run_command",
            tool_args={"cmd": "npm init -y"},
            action=type("MockAction", (), {"action_type": "terminal", "risk_level": type("RL", (), {"value": "high"}), "requires_confirmation": True})(),
        )

        # User intervenes and corrects the command
        correction = self.manager.record_correction(
            task_id=task1.task_id,
            step_id=step2.step_id,
            proposed_action={"cmd": "npm init -y"},
            user_correction={"cmd": "npm create vite@latest BlueSea -- --template react"},
            correction_type="parameter_override",
            context="User specified Vite template",
        )

        self.manager.record_step_result(step2.step_id, {"ok": True}, success=True, duration_ms=200)
        self.manager.complete_task(task1.task_id, status="completed", duration_ms=350)

        # Verify trace captured correction
        task1_record = self.manager.get_task(task1.task_id)
        self.assertEqual(task1_record.intervention_count, 1)
        self.assertEqual(task1_record.correction_count, 1)

        # 2. Skill Learning: Propose and save skill
        proposal = {
            "name": "create_react_project",
            "description": "Create a React project with Vite",
            "parameters": {
                "project_name": {"type": "string", "required": True},
                "dest_folder": {"type": "path", "required": False},
            },
            "permissions": ["filesystem:create", "terminal:execute"],
            "risk_level": "medium",
            "steps": [
                {"tool": "create_folder", "args": {"path": "{dest_folder}/{project_name}"}},
                {"tool": "run_command", "args": {"cmd": "npm create vite@latest {project_name} -- --template react"}},
            ],
            "uncertain_params": [
                {"name": "dest_folder", "guessed_value": "~/Projects", "question": "Always in ~/Projects?"}
            ],
        }

        # User resolves uncertain parameter (e.g. Always keep ~/Projects)
        saved_skill = finalize_learned_skill(proposal, {"dest_folder": False})
        self.assertEqual(saved_skill.name, "create_react_project")
        # dest_folder was baked into steps and removed from parameters
        self.assertNotIn("dest_folder", saved_skill.parameters)
        self.assertIn("project_name", saved_skill.parameters)

        register_skill(saved_skill)

        # 3. Second Task: "Create another React project called TestApp"
        query = "Create another React project called TestApp"
        matched = find_matching_skill(query, get_all_manifests(), threshold=0.35)

        self.assertIsNotNone(matched)
        self.assertEqual(matched.skill.name, "create_react_project")
        self.assertEqual(matched.extracted_params.get("project_name"), "TestApp")


if __name__ == "__main__":
    unittest.main()
