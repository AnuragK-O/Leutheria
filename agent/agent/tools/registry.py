from agent.tools.basic import create_folder, list_files, open_app, run_command

# "safety" is a stub for the confirmation-tiering work (roadmap step 5).
# run_command is marked destructive now, not "safe", because it executes
# arbitrary shell input -- unlike the other three, its blast radius isn't
# bounded by fixed parameters.
TOOLS = {
    "open_app": {"fn": open_app, "safety": "safe"},
    "create_folder": {"fn": create_folder, "safety": "safe"},
    "list_files": {"fn": list_files, "safety": "safe"},
    "run_command": {"fn": run_command, "safety": "destructive"},
}
