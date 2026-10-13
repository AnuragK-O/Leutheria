#!/usr/bin/env node
/* Checks the Electron side of the agent's WebSocket authentication without
   starting Electron or the Python agent:

     node scripts/auth-check.js

   - agent-auth.js: token generation, which token each mode uses, and the
     environment a child is handed (the spawn path `npm start` takes can't be
     run in a check -- it kills any running agent -- so the functions main.js
     builds that path from are tested here, and main.js is read to confirm it
     uses them)
   - what `ws` actually puts on the wire: the Authorization header, and no
     Origin header
   - scripts/mock-agent.js (port 8796, never 8765) enforcing the token and
     refusing an Origin, as the real agent does

   One line per check; exits non-zero if any failed. */

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const WebSocket = require("ws");
const auth = require("../agent-auth");

const MOCK_PORT = Number(process.env.AUTH_CHECK_PORT || 8796);
if (MOCK_PORT === 8765) {
  console.error("auth-check: refusing to use 8765, the real agent's port");
  process.exit(1);
}

let failures = 0;
function check(ok, label) {
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${label}`);
  if (!ok) failures += 1;
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Resolves "open", or the HTTP status the handshake was refused with.
function attempt(url, options) {
  return new Promise((resolve) => {
    const socket = new WebSocket(url, options);
    socket.on("open", () => {
      socket.close();
      resolve("open");
    });
    socket.on("unexpected-response", (_req, res) => {
      res.resume();
      resolve(res.statusCode);
    });
    socket.on("error", (err) => resolve(`error: ${err.message}`));
  });
}

async function main() {
  console.log("# tokens");
  const a = auth.generateToken();
  const b = auth.generateToken();
  check(/^[0-9a-f]{64}$/.test(a) && a !== b, "generateToken: 32 random bytes as hex, different every call");

  const spawned = auth.createAuth({ noSpawn: false, agentUrl: "ws://127.0.0.1:8765", env: { LEUTHERIA_TOKEN: "x".repeat(40) } });
  check(
    spawned.source === "generated" && /^[0-9a-f]{64}$/.test(spawned.current()) && spawned.current() === spawned.current(),
    "spawn mode: a fresh token, stable for the launch, ignoring an inherited LEUTHERIA_TOKEN"
  );
  const spawnedAgain = auth.createAuth({ noSpawn: false, agentUrl: "ws://127.0.0.1:8765", env: {} });
  check(spawned.current() !== spawnedAgain.current(), "spawn mode: a different token on the next launch");

  const supplied = "s".repeat(48);
  const dev = auth.createAuth({ noSpawn: true, agentUrl: "ws://127.0.0.1:8794", env: { LEUTHERIA_TOKEN: supplied } });
  check(dev.source === "env" && dev.current() === supplied, "NO_SPAWN: uses LEUTHERIA_TOKEN from the app's own environment");

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "leutheria-auth-check-"));
  try {
    const fileEnv = { LEUTHERIA_RUNTIME_DIR: tmp };
    const fromFile = auth.createAuth({ noSpawn: true, agentUrl: "ws://127.0.0.1:8794", env: fileEnv });
    check(
      fromFile.source === "file" && fromFile.file === path.join(tmp, "agent-8794.token") && fromFile.current() === null,
      "NO_SPAWN, no LEUTHERIA_TOKEN: falls back to the standalone agent's token file (none yet -> no token)"
    );
    fs.writeFileSync(fromFile.file, `${a}\n`, { mode: 0o600 });
    check(fromFile.current() === a, "...reads it once the agent has written it");
    fs.writeFileSync(fromFile.file, `${b}\n`, { mode: 0o600 });
    check(fromFile.current() === b, "...and re-reads on every attempt (the agent restarted with a new token)");
    fs.writeFileSync(fromFile.file, "short\n");
    check(fromFile.current() === null, "...ignoring a file too short to be a token");
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  check(
    auth.tokenFilePath("ws://127.0.0.1:8765", {}) ===
      path.join(os.homedir(), "Library", "Application Support", "Leutheria", "agent-8765.token"),
    "token file path matches the agent's default (~/Library/Application Support/Leutheria/agent-<port>.token)"
  );

  console.log("# what a child is handed");
  const env = auth.childEnv(a, { PATH: "/usr/bin", LEUTHERIA_TOKEN: "stale", HOME: "/h" });
  check(env.LEUTHERIA_TOKEN === a && env.PATH === "/usr/bin" && env.HOME === "/h", "childEnv: the token replaces an inherited one; the rest is passed through");
  const none = auth.childEnv(null, { PATH: "/usr/bin", LEUTHERIA_TOKEN: "stale" });
  check(!("LEUTHERIA_TOKEN" in none), "childEnv with no token: an inherited one is dropped, not passed on");
  const child = spawnSync(process.execPath, ["-e", "process.stdout.write(process.env.LEUTHERIA_TOKEN + ' ' + process.argv.join(' '))"], {
    env: auth.childEnv(a),
    encoding: "utf8",
  });
  const [seen, ...argv] = child.stdout.split(" ");
  check(seen === a && !argv.join(" ").includes(a), "a real child spawned with childEnv sees the token in its environment and not in its argv");

  const mainSource = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  check(
    mainSource.includes('spawn(AGENT_PYTHON, ["-m", "agent"], { cwd: AGENT_DIR, env: childEnv(agentAuth.current()) })'),
    "main.js spawns the Python agent with childEnv(agentAuth.current())"
  );
  check(mainSource.includes("new WebSocket(AGENT_URL, connectOptions(agentAuth.current()))"), "main.js connects with connectOptions(agentAuth.current())");
  check(/spawn\(QT_OVERLAY_BIN, args, \{[^}]*env: childEnv\(agentAuth\.current\(\)\)/s.test(mainSource), "main.js spawns the Qt overlay with the token in its environment");
  check(!/args\s*=\s*\[[^\]]*(token|TOKEN)/.test(mainSource), "main.js never puts a token in a child's argv");
  check(mainSource.includes("delete process.env[TOKEN_ENV]"), "main.js removes LEUTHERIA_TOKEN from its own environment once read");
  const loggedToken = mainSource.split("\n").filter((line) => /\blog\(/.test(line) && /agentAuth\.current|token\b/i.test(line) && !/explainRejection/.test(line));
  check(loggedToken.length === 0, "main.js has no log line that mentions the token");

  console.log("# what `ws` sends");
  const seenHeaders = await new Promise((resolve) => {
    const server = http.createServer();
    server.on("upgrade", (req, socket) => {
      socket.end("HTTP/1.1 400 Bad Request\r\n\r\n");
      server.close();
      resolve(req.headers);
    });
    server.listen(0, "127.0.0.1", () => {
      const probe = new WebSocket(`ws://127.0.0.1:${server.address().port}`, auth.connectOptions(a));
      probe.on("error", () => {});
      probe.on("unexpected-response", (_req, res) => res.resume());
    });
  });
  check(seenHeaders.authorization === `Bearer ${a}`, "the handshake carries Authorization: Bearer <token>");
  check(!("origin" in seenHeaders), "the handshake carries no Origin header (the agent would refuse it)");
  check(JSON.stringify(auth.connectOptions(null)) === "{}", "with no token, no Authorization header is invented");

  console.log(`# mock agent on ${MOCK_PORT}, token required`);
  const mock = spawn(process.execPath, [path.join(__dirname, "mock-agent.js")], {
    env: { ...auth.childEnv(a), MOCK_PORT: String(MOCK_PORT), MOCK_AUTOSTART: "0" },
    stdio: ["ignore", "pipe", "inherit"],
  });
  let out = "";
  mock.stdout.on("data", (data) => (out += data.toString()));
  try {
    for (let i = 0; i < 50 && !out.includes("AGENT_READY"); i += 1) await sleep(100);
    check(out.includes("AGENT_READY"), "mock agent started");
    const url = `ws://127.0.0.1:${MOCK_PORT}`;
    check((await attempt(url, {})) === 401, "no token -> 401");
    check((await attempt(url, auth.connectOptions(b))) === 401, "wrong token -> 401");
    check((await attempt(url, { headers: { Authorization: `Basic ${a}` } })) === 401, "right token, wrong scheme -> 401");
    check((await attempt(url, auth.connectOptions(a))) === "open", "right token -> accepted");
    check((await attempt(url, { ...auth.connectOptions(a), origin: "https://evil.example" })) === 403, "right token + Origin -> 403");
    await sleep(100);
    check(!out.includes(a) && !out.includes(b), "the mock's output never contains a token");
  } finally {
    mock.kill();
  }

  console.log("# explaining a refusal");
  check(/something else is listening/.test(auth.explainRejection("Unexpected server response: 401", spawned)), "401 in spawn mode: says something else owns the port");
  check(/LEUTHERIA_TOKEN/.test(auth.explainRejection("Unexpected server response: 401", dev)), "401 with NO_SPAWN: points at LEUTHERIA_TOKEN");
  check(auth.explainRejection("connect ECONNREFUSED 127.0.0.1:8765", dev) === null, "an unrelated error is left alone");
  const explained = [spawned, dev].map((mode) => auth.explainRejection("Unexpected server response: 401", mode)).join(" ");
  check(!explained.includes(supplied) && !explained.includes(spawned.current()), "the explanation never contains the token");

  console.log(failures ? `\n${failures} check(s) FAILED` : "\nALL CHECKS PASSED");
  process.exit(failures ? 1 : 0);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
