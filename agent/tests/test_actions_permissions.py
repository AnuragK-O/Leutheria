import unittest

from agent.core.action import ExecutionMode, RiskLevel, normalize_tool_action
from agent.core.permissions import (
    PermissionScope,
    evaluate_action_authorization,
    grant_permission,
    is_permission_granted,
    revoke_permission,
)


class TestActionsAndPermissions(unittest.TestCase):
    def test_normalize_tool_action(self):
        act_safe = normalize_tool_action("list_files", {"path": "~/Desktop"})
        self.assertEqual(act_safe.action_type, "list_files")
        self.assertEqual(act_safe.risk_level, RiskLevel.LOW)
        self.assertIn("filesystem:read", act_safe.required_permissions)
        self.assertFalse(act_safe.requires_confirmation)

        act_term = normalize_tool_action("run_command", {"cmd": "npm install"})
        self.assertEqual(act_term.action_type, "terminal")
        self.assertEqual(act_term.risk_level, RiskLevel.HIGH)
        self.assertIn("terminal:execute", act_term.required_permissions)
        self.assertTrue(act_term.requires_confirmation)

    def test_permissions_grant_and_revoke(self):
        perm = "test:perm"
        self.assertFalse(is_permission_granted(perm))
        
        grant_permission(perm, scope=PermissionScope.SESSION)
        self.assertTrue(is_permission_granted(perm))
        
        revoke_permission(perm, scope=PermissionScope.SESSION)
        self.assertFalse(is_permission_granted(perm))

    def test_evaluate_authorization_copilot(self):
        # Low risk without required special permissions or low risk default:
        act_low = normalize_tool_action("get_current_datetime", {})
        auth, reason = evaluate_action_authorization("get_current_datetime", act_low, ExecutionMode.COPILOT)
        self.assertTrue(auth)

        # High risk action in Copilot requires confirmation
        act_high = normalize_tool_action("run_command", {"cmd": "rm -rf /tmp/foo"})
        auth, reason = evaluate_action_authorization("run_command", act_high, ExecutionMode.COPILOT)
        self.assertFalse(auth)
        self.assertIn("confirmation", reason.lower())

    def test_guide_mode_restricts_mutation(self):
        act_mutating = normalize_tool_action("create_folder", {"path": "~/test"})
        auth, reason = evaluate_action_authorization("create_folder", act_mutating, ExecutionMode.GUIDE)
        self.assertFalse(auth)
        self.assertIn("Guide mode", reason)


if __name__ == "__main__":
    unittest.main()
