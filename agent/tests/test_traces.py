import tempfile
from pathlib import Path
import unittest

from agent.core.action import normalize_tool_action
from agent.core.database import init_db
from agent.core.trace import TraceManager


class TestTraceSystem(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_traces.db"
        init_db(self.db_path)
        self.manager = TraceManager(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_task_lifecycle_and_steps(self):
        task = self.manager.start_task(
            user_request="Create a test folder",
            provider="anthropic",
            model="claude-opus-5",
            execution_mode="copilot",
        )
        self.assertEqual(task.status, "in_progress")
        self.assertEqual(task.user_request, "Create a test folder")

        # Record step 1
        act = normalize_tool_action("create_folder", {"path": "~/Desktop/test"})
        step = self.manager.record_step_proposed(
            task_id=task.task_id,
            step_index=0,
            tool_name="create_folder",
            tool_args={"path": "~/Desktop/test"},
            action=act,
            observation="Observed desktop directory",
            reasoning="Need to create folder first",
        )
        self.assertEqual(step.tool_name, "create_folder")

        # Step finishes successfully
        self.manager.record_step_result(step.step_id, {"ok": True}, success=True, duration_ms=120)

        # Complete task
        self.manager.complete_task(task.task_id, status="completed", outcome_summary="Folder created", duration_ms=300)

        retrieved = self.manager.get_task(task.task_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.status, "completed")
        self.assertEqual(retrieved.outcome_summary, "Folder created")

        steps = self.manager.get_task_steps(task.task_id)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].result, {"ok": True})
        self.assertTrue(steps[0].success)

    def test_human_correction_recording(self):
        task = self.manager.start_task("Install node packages", execution_mode="copilot")
        act = normalize_tool_action("run_command", {"cmd": "npm install"})
        step = self.manager.record_step_proposed(
            task_id=task.task_id,
            step_index=0,
            tool_name="run_command",
            tool_args={"cmd": "npm install"},
            action=act,
        )

        # User intervenes and corrects Node version / command
        correction = self.manager.record_correction(
            task_id=task.task_id,
            step_id=step.step_id,
            proposed_action={"cmd": "npm install"},
            user_correction={"cmd": "npm install --legacy-peer-deps"},
            correction_type="parameter_override",
            context="Node 22 incompatibility",
        )

        self.assertEqual(correction.user_correction, {"cmd": "npm install --legacy-peer-deps"})

        # Verify task and step reflect intervention
        updated_task = self.manager.get_task(task.task_id)
        self.assertEqual(updated_task.intervention_count, 1)
        self.assertEqual(updated_task.correction_count, 1)

        steps = self.manager.get_task_steps(task.task_id)
        self.assertTrue(steps[0].user_intervened)
        self.assertEqual(steps[0].correction_id, correction.correction_id)

        corrections = self.manager.get_task_corrections(task.task_id)
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0].context, "Node 22 incompatibility")

    def test_metrics_calculation(self):
        task1 = self.manager.start_task("Task 1")
        self.manager.complete_task(task1.task_id, status="completed", duration_ms=200)

        task2 = self.manager.start_task("Task 2")
        act = normalize_tool_action("run_command", {"cmd": "test"})
        step = self.manager.record_step_proposed(task2.task_id, 0, "run_command", {"cmd": "test"}, act)
        self.manager.record_correction(task2.task_id, step.step_id, {"cmd": "test"}, {"cmd": "test corrected"})
        self.manager.complete_task(task2.task_id, status="completed", duration_ms=400)

        metrics = self.manager.get_metrics()
        self.assertEqual(metrics["total_tasks"], 2)
        self.assertEqual(metrics["completed_tasks"], 2)
        self.assertEqual(metrics["completion_rate_pct"], 100.0)
        self.assertEqual(metrics["total_interventions"], 1)
        self.assertEqual(metrics["total_corrections"], 1)


if __name__ == "__main__":
    unittest.main()
