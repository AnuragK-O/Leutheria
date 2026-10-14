/* Which group a capability sits under in the Library.

   The agent's inventory has no category field (agent/agent/core/inventory.py
   returns name, kind, description, safety, origin, ...), so the grouping is
   decided here, on the client. A capability is placed by the first of these
   that gives an answer:

     1. BY_NAME        its name is listed under a group below
     2. its steps      a learned skill goes where most of its steps' tools go
     3. PATTERNS       a word in its name matches a group's pattern
     4. "other"        the fallback, shown last

   Rule 4 is the point of the file: a capability nobody has filed yet still
   shows up, under Other, instead of disappearing. When you add a tool or a
   built-in skill, add its name to a group here. Order in GROUPS is the order
   the groups are listed in.

   This is display only. Nothing here changes what a capability can do or
   whether it asks first. */

(function (LX) {
  const GROUPS = [
    {
      id: "files",
      label: "Files",
      hint: "Read, write, move and open files and folders",
      names:
        "append_text copy_file create_folder file_info list_files move_file open_file open_with read_file " +
        "reveal_in_finder trash_file write_new_file backup_folder",
      pattern: /file|folder|dir\b|_dir|path/,
    },
    {
      id: "search",
      label: "Search and storage",
      hint: "Find files and see what is taking up space",
      names:
        "find_files find_duplicates folder_size search_file_contents spotlight_search duplicate_report " +
        "find_large_files find_recent_files folder_summary storage_report",
      pattern: /find|search_file|spotlight|duplicate|storage|size/,
    },
    {
      id: "tidy",
      label: "Tidy and archive",
      hint: "Sort, zip and unpack",
      names:
        "extract_archive zip_folder archive_folder archive_old_downloads clean_desktop collect_screenshots " +
        "extract_latest_download organize_by_date organize_folder",
      pattern: /archive|zip|organi[sz]e|clean|tidy|extract/,
    },
    {
      id: "notes",
      label: "Notes and clipboard",
      hint: "Capture text to the clipboard or a file",
      names:
        "get_clipboard set_clipboard clipboard_to_file clipboard_to_note daily_journal quick_note read_later " +
        "save_page_notes",
      pattern: /clipboard|note|journal/,
    },
    {
      id: "web",
      label: "Web and weather",
      hint: "Pages, searches and forecasts",
      names:
        "fetch_url open_url get_weather set_home_location weather_report web_search define_term research_topic " +
        "github_open",
      pattern: /url|web|weather|browser|page|research/,
    },
    {
      id: "dev",
      label: "Development",
      hint: "Shell, git and project scaffolding",
      names:
        "run_command git_status clone_repo code_scratchpad git_snapshot git_sync new_node_project " +
        "new_python_project new_web_page open_workspace project_snapshot repo_overview setup_project",
      pattern: /git|repo|project|code|command|workspace/,
    },
    {
      id: "system",
      label: "System",
      hint: "Battery, disk, network, sound and the screen",
      names:
        "get_battery_status get_disk_space get_network_status get_system_info get_volume set_volume " +
        "media_control show_notification take_screenshot get_current_datetime battery_check " +
        "system_health_check screenshot_to_folder",
      pattern: /battery|disk|network|system|volume|media|notification|screenshot|datetime/,
    },
    {
      id: "apps",
      label: "Apps and GUI control",
      hint: "Open apps, and type into them with a per-app grant",
      names: "open_app list_running_apps focus_app describe_ui press_keys type_text",
      pattern: /_app\b|apps\b|_ui\b|keys|type_text/,
    },
    {
      id: "time",
      label: "Timers and routines",
      hint: "Timers, focus sessions and daily briefings",
      names:
        "set_timer cancel_timer list_timers quick_timer pomodoro focus_mode wind_down morning_briefing " +
        "end_of_day meeting_prep",
      pattern: /timer|pomodoro|briefing|routine|meeting/,
    },
  ];

  const OTHER = { id: "other", label: "Other", hint: "Not filed under a group yet" };

  const BY_NAME = new Map();
  for (const group of GROUPS) {
    for (const name of group.names.split(/\s+/)) BY_NAME.set(name, group.id);
  }

  function byPattern(name) {
    const group = GROUPS.find((entry) => entry.pattern.test(name));
    return group ? group.id : null;
  }

  /** The group id for one inventory entry. Never returns nothing. */
  function groupOf(entry) {
    const filed = BY_NAME.get(entry.name);
    if (filed) return filed;

    // A learned skill is a list of tool calls: file it where most of them live.
    if (entry.steps && entry.steps.length) {
      const votes = new Map();
      for (const step of entry.steps) {
        const id = BY_NAME.get(step.tool) || byPattern(String(step.tool || ""));
        if (id) votes.set(id, (votes.get(id) || 0) + 1);
      }
      const winner = [...votes.entries()].sort((a, b) => b[1] - a[1])[0];
      if (winner) return winner[0];
    }

    return byPattern(entry.name) || OTHER.id;
  }

  LX.libraryGroups = { GROUPS: [...GROUPS, OTHER], groupOf };
})(window.LX);
