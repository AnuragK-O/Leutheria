/* The Electron side of the agent's WebSocket authentication (see
   agent/agent/core/ws_auth.py for the rules the server enforces).

   Plain Node, no Electron imports, so `node scripts/auth-check.js` can test
   exactly the functions main.js uses to build the sidecar's environment and
   the connection's headers -- the spawn path can't otherwise be exercised
   without `npm start`, which kills any running agent.

   The token lives in a variable in the main process and in the environment
   of the children it is handed to. It is never logged, never written to
   disk, never put on a command line (`ps` shows argv to every user), and
   never sent to a renderer. */

const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const TOKEN_ENV = "LEUTHERIA_TOKEN";
const RUNTIME_DIR_ENV = "LEUTHERIA_RUNTIME_DIR";
const MIN_TOKEN_CHARS = 32; // the agent refuses a shorter one

function generateToken() {
  return crypto.randomBytes(32).toString("hex"); // 256 bits
}

/* Where a standalone agent (`python -m agent`, no LEUTHERIA_TOKEN) writes the
   token it generated -- must match ws_auth.token_file_path(). */
function tokenFilePath(agentUrl, env = process.env) {
  let port;
  try {
    port = Number(new URL(agentUrl).port) || 80;
  } catch (_err) {
    return null;
  }
  const dir = env[RUNTIME_DIR_ENV] || path.join(os.homedir(), "Library", "Application Support", "Leutheria");
  return path.join(dir, `agent-${port}.token`);
}

/* Decide the token source once, at startup.

   - Spawning the agent (the normal app): a fresh random token every launch.
     An inherited LEUTHERIA_TOKEN is ignored on purpose -- a value that has
     been sitting in a shell profile is not a per-launch secret.
   - LEUTHERIA_NO_SPAWN=1 (connecting to an agent someone else started):
     LEUTHERIA_TOKEN from our own environment, or failing that the token file
     a standalone agent wrote, re-read on every attempt because that agent
     writes a new one each time it starts. */
function createAuth({ noSpawn, agentUrl, env = process.env }) {
  if (!noSpawn) {
    const token = generateToken();
    return { source: "generated", current: () => token };
  }
  const supplied = env[TOKEN_ENV];
  if (supplied) return { source: "env", current: () => supplied };
  const file = tokenFilePath(agentUrl, env);
  return {
    source: "file",
    file,
    current: () => {
      try {
        const token = fs.readFileSync(file, "utf8").trim();
        return token.length >= MIN_TOKEN_CHARS ? token : null;
      } catch (_err) {
        return null;
      }
    },
  };
}

/* The environment for a child that needs the token (the Python sidecar, the
   Qt overlay). Any inherited value is dropped first, so a child never ends
   up with a stale one when there is no token to give. */
function childEnv(token, env = process.env) {
  const out = { ...env };
  delete out[TOKEN_ENV];
  if (token) out[TOKEN_ENV] = token;
  return out;
}

/* Options for `new WebSocket(url, options)`. Only the header: `ws` sends no
   Origin unless asked to, and the agent refuses any request that has one. */
function connectOptions(token) {
  return token ? { headers: { Authorization: `Bearer ${token}` } } : {};
}

/* A sentence for the log when the agent refuses the handshake, or null if
   the error is something else. Says which mode we're in, never the token. */
function explainRejection(message, auth) {
  const match = /Unexpected server response: (\d{3})/.exec(String(message));
  if (!match) return null;
  const status = Number(match[1]);
  if (status === 403) return "the agent refused the connection (403): the request carried an Origin header";
  if (status !== 401) return null;
  if (auth.source === "generated") {
    return "the agent refused our token (401): something else is listening on this port -- not the agent this app started";
  }
  if (auth.source === "env") {
    return `the agent refused our token (401): ${TOKEN_ENV} in this environment isn't the token that agent was started with`;
  }
  return (
    `the agent refused the connection (401): set ${TOKEN_ENV} to the token the agent was started with ` +
    `(no usable token file at ${auth.file} -- a standalone agent writes one there)`
  );
}

module.exports = {
  TOKEN_ENV,
  MIN_TOKEN_CHARS,
  generateToken,
  tokenFilePath,
  createAuth,
  childEnv,
  connectOptions,
  explainRejection,
};
