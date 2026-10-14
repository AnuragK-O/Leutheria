/* Fixture data for scripts/mock-agent.js: a small capability catalog, skill
   history, standing approvals, one live app grant, input devices and wake
   models -- enough for every view in the main window to render something
   real-shaped without the Python agent. The shapes follow what
   agent/agent/core/control.py returns; the values are made up.

   Everything here is mutable state for the life of the mock process, so the
   management messages (set_preference, delete_skill, revoke_*) can change it
   and the UI's toggles, Reset, Revoke and Delete have something to act on. */

const now = () => Date.now() / 1000;
const str = (description) => ({ type: "string", description });

function tool(name, description, safety, props, required, runs, failures, agoS, extra) {
  const ask = safety === "destructive";
  return {
    kind: "tool",
    name,
    description,
    safety,
    enabled: true,
    requires_confirmation: ask,
    default_requires_confirmation: ask,
    is_overridden: false,
    input_schema: { type: "object", properties: props, required },
    runs,
    failures,
    last_used: agoS === null ? null : now() - agoS,
    deletable: false,
    ...extra,
  };
}

const inventory = {
  tools: [
    tool(
      "run_command",
      "Run a shell command in a working directory and return its output. Unbounded, so it always asks first.",
      "destructive",
      { cmd: str("The command line to run, exactly as it would be typed."), cwd: str("Directory to run it in. Defaults to the home folder.") },
      ["cmd"],
      42,
      3,
      340
    ),
    tool("list_dir", "List the files and folders directly inside a directory.", "safe", { path: str("Directory to list.") }, ["path"], 118, 0, 95),
    tool("read_file", "Read a text file and return its contents, up to a size limit.", "safe", { path: str("File to read.") }, ["path"], 76, 2, 1300),
    tool(
      "move_file",
      "Move or rename a file. Refuses to overwrite anything that already exists.",
      "safe",
      { src: str("File to move."), dest: str("Where it should end up.") },
      ["src", "dest"],
      31,
      1,
      7200
    ),
    tool("trash_file", "Move a file to the Trash, where it can be recovered.", "safe", { path: str("File to trash.") }, ["path"], 12, 0, 90000, {
      requires_confirmation: true,
      is_overridden: true,
    }),
    tool("open_app", "Open an application by name.", "safe", { name: str("Application name, as shown in the Dock.") }, ["name"], 64, 0, 610),
    tool("focus_app", "Bring a running application to the front so it can be typed into.", "scoped", { app: str("Application name.") }, ["app"], 19, 1, 2400),
    tool(
      "type_text",
      "Type text into the focused application, as keystrokes.",
      "scoped",
      { text: str("Text to type."), app: str("The application expected to have focus.") },
      ["text", "app"],
      23,
      2,
      2400
    ),
    tool(
      "press_keys",
      "Press a keyboard shortcut in the focused application.",
      "scoped",
      { keys: str("Shortcut, like cmd+s."), app: str("The application expected to have focus.") },
      ["keys", "app"],
      9,
      0,
      2500
    ),
    tool("get_time", "The current local date and time.", "safe", {}, [], 205, 0, 40),
    tool("web_search", "Search the web and return the top results.", "safe", { query: str("What to search for.") }, ["query"], 0, 0, null, {
      enabled: false,
      is_overridden: true,
    }),
  ],
  skills: [
    {
      kind: "skill",
      origin: "builtin",
      name: "create_project",
      enabled: true,
      description: "Scaffold a new project folder with a README and a git repository.",
      requires_confirmation: false,
      default_requires_confirmation: false,
      is_overridden: false,
      input_schema: { type: "object", properties: { name: str("Project name."), parent: str("Folder to create it in.") }, required: ["name"] },
      runs: 7,
      failures: 1,
      last_used: now() - 86400 * 3,
      deletable: false,
    },
    {
      kind: "skill",
      origin: "generated",
      name: "archive_downloads",
      enabled: true,
      description: "Move everything older than a week out of Downloads into a dated archive folder.",
      requires_confirmation: false,
      default_requires_confirmation: false,
      is_overridden: false,
      input_schema: { type: "object", properties: { folder: str("Archive folder name.") }, required: ["folder"] },
      steps: [
        { tool: "list_dir", args: { path: "~/Downloads" } },
        { tool: "run_command", args: { cmd: "mkdir -p ~/Archive/{folder}" } },
        { tool: "move_file", args: { src: "~/Downloads/{file}", dest: "~/Archive/{folder}/{file}" } },
      ],
      runs: 4,
      failures: 0,
      last_used: now() - 5400,
      created_at: now() - 86400 * 9,
      deletable: true,
    },
  ],
  history: [
    { event: "saved", name: "archive_downloads", ts: now() - 86400 * 9 },
    { event: "proposed", name: "archive_downloads", confident: false, uncertain_count: 1, ts: now() - 86400 * 9 - 30 },
    { event: "declined", name: "open_and_tile_editor", ts: now() - 86400 * 12 },
    { event: "proposed", name: "open_and_tile_editor", confident: true, ts: now() - 86400 * 12 - 20 },
  ],
  trusted: [
    { key: "t1", tool: "run_command", args: { cmd: "git status", cwd: "~/Projects/demo" }, scope: "always" },
    { key: "t2", tool: "run_command", args: { cmd: "npm test" }, scope: "session" },
  ],
  grants: [{ app: "Notes", expires_in: 412 }],
};

const EMPTY_INVENTORY = { tools: [], skills: [], history: [], trusted: [], grants: [] };

const INPUT_DEVICES = [
  { index: 0, name: "MacBook Pro Microphone", default: true },
  { index: 2, name: "Studio Display Microphone", default: false },
];

const WAKE_MODELS = [
  { name: "hey_jarvis", downloaded: true },
  { name: "alexa", downloaded: true },
  { name: "hey_mycroft", downloaded: false },
  { name: "timer", downloaded: false },
];

/* The same range checks agent/agent/core/voice_settings.py applies, for the
   three numeric settings -- so the Voice view's "agent refused it" path (the
   error shown against the field it names) can be exercised. */
function voiceSettingProblem(update) {
  const threshold = update.wake_threshold;
  if (threshold !== undefined && !(typeof threshold === "number" && threshold > 0 && threshold < 1)) {
    return "wake_threshold must be between 0 and 1";
  }
  const endpoint = update.endpoint_silence_ms;
  if (endpoint !== undefined && !(typeof endpoint === "number" && endpoint >= 240 && endpoint <= 5000)) {
    return "endpoint_silence_ms must be between 240 and 5000";
  }
  const timeout = update.silence_timeout_s;
  if (timeout !== undefined && timeout !== null && !(typeof timeout === "number" && timeout >= 5)) {
    return "silence_timeout_s must be at least 5, or null for never";
  }
  return null;
}

function applyPreference(message) {
  const entry = [...inventory.tools, ...inventory.skills].find((item) => item.name === message.name);
  if (!entry) return;
  if (message.reset) {
    entry.enabled = true;
    entry.requires_confirmation = entry.default_requires_confirmation;
  }
  if (typeof message.enabled === "boolean") entry.enabled = message.enabled;
  if (typeof message.requires_confirmation === "boolean") entry.requires_confirmation = message.requires_confirmation;
  entry.is_overridden = !entry.enabled || entry.requires_confirmation !== entry.default_requires_confirmation;
}

/* The "propose" demo command: the trace it ran, and the skill it offers. */
function proposalFor(id) {
  const name = `summarize_project_${id.replace(/\D/g, "")}`;
  const description = "List a project folder and summarize its most recent commits.";
  const trace = [
    { tool: "list_dir", args: { path: "~/Projects/demo" }, result: { ok: true } },
    { tool: "run_command", args: { cmd: "git -C ~/Projects/demo log --oneline -5" }, result: { ok: true } },
  ];
  return {
    trace,
    reply: "The demo project has five recent commits. The latest is a fix to the build script.",
    proposed: {
      type: "skill_proposed",
      id,
      name,
      description,
      steps: trace.map((step) => ({ tool: step.tool, args: step.args })),
      uncertain_params: [
        {
          name: "project",
          guessed_value: "~/Projects/demo",
          question: "Is it always the demo project, or does the project change each time?",
        },
      ],
    },
    skill: {
      kind: "skill",
      origin: "generated",
      name,
      enabled: true,
      description,
      requires_confirmation: false,
      default_requires_confirmation: false,
      is_overridden: false,
      input_schema: { type: "object", properties: { project: str("Project folder.") }, required: ["project"] },
      steps: [
        { tool: "list_dir", args: { path: "{project}" } },
        { tool: "run_command", args: { cmd: "git -C {project} log --oneline -5" } },
      ],
      runs: 0,
      failures: 0,
      last_used: null,
      created_at: now(),
      deletable: true,
    },
  };
}

const FAILED_TRACE = [
  { tool: "list_dir", args: { path: "~/Documents" }, result: { ok: true } },
  { tool: "read_file", args: { path: "~/Documents/notes-2025.txt" }, result: { ok: false, error: "no such file" } },
];

module.exports = {
  inventory,
  EMPTY_INVENTORY,
  INPUT_DEVICES,
  WAKE_MODELS,
  FAILED_TRACE,
  voiceSettingProblem,
  applyPreference,
  proposalFor,
  now,
};

// A 300-character command, for the confirmation card's long-content case.
module.exports.LONG_COMMAND =
  "find ~/Projects/demo -type f \\( -name '*.log' -o -name '*.tmp' -o -name '*.cache' \\) -mtime +14 -print0 | " +
  "xargs -0 tar -czf ~/Archive/demo-logs-$(date +%Y%m%d).tar.gz && find ~/Projects/demo -type f \\( -name '*.log' -o -name '*.tmp' \\) " +
  "-mtime +14 -delete && echo 'archived and cleaned' && du -sh ~/Projects/demo ~/Archive";

// What the agent puts in inventory.settings_errors for a corrupt preferences file.
module.exports.SETTINGS_ERROR =
  "preferences.json couldn't be read (Expecting value: line 1 column 1), so every capability is using its built-in default until the file is fixed.";

// Long content: a learned skill with a very long name and 40 steps.
module.exports.inventory.skills.push({
  kind: "skill",
  name: "export_quarterly_reports_to_the_shared_finance_drive_and_notify_the_team",
  description:
    "Export every quarterly report in the Reports folder to the shared finance drive, then post a notification. Learned from a run on Oct 2.",
  input_schema: { type: "object", properties: { quarter: { type: "string", description: "Quarter, like 2026-Q3." } }, required: ["quarter"] },
  enabled: true,
  requires_confirmation: false,
  default_requires_confirmation: false,
  is_overridden: false,
  runs: 1,
  failures: 0,
  last_used: now() - 86400 * 11,
  origin: "generated",
  deletable: true,
  created_at: now() - 86400 * 11,
  steps: Array.from({ length: 40 }, (_unused, index) =>
    index % 4 === 3
      ? { tool: "show_notification", args: { title: `Exported part ${index + 1} of 40`, message: "{quarter}" } }
      : { tool: "copy_file", args: { src: `~/Reports/{quarter}/part-${String(index + 1).padStart(2, "0")}.pdf`, dest: "/Volumes/Finance/{quarter}/" } }
  ),
});
