const http = require("http");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const WebSocket = require("ws");

const PORT = process.env.PORT || 3000;
const AGENT_DIR = path.join(__dirname, "..", "agent");
const ASSETS_DIR = path.join(__dirname, "..", "assets");
const RENDERER_DIR = path.join(__dirname, "renderer");
const isWin = process.platform === "win32";

function getPythonPath() {
  if (isWin) {
    const venvPython = path.join(AGENT_DIR, ".venv", "Scripts", "python.exe");
    if (fs.existsSync(venvPython)) return venvPython;
    return process.env.PYTHON_PATH || "python";
  }
  const venvPython = path.join(AGENT_DIR, ".venv", "bin", "python");
  if (fs.existsSync(venvPython)) return venvPython;
  return process.env.PYTHON_PATH || "python3";
}

let agentProcess = null;

function startAgent() {
  const pythonPath = getPythonPath();
  agentProcess = spawn(pythonPath, ["-m", "agent"], { cwd: AGENT_DIR });

  agentProcess.stdout.on("data", (data) => {
    const text = data.toString().trim();
    console.log(`[agent] ${text}`);
  });

  agentProcess.stderr.on("data", (data) => {
    console.error(`[agent:err] ${data.toString().trim()}`);
  });

  agentProcess.on("exit", (code) => {
    console.log(`[agent] exited with code ${code}`);
  });
}

const MIME_TYPES = {
  ".html": "text/html",
  ".css": "text/css",
  ".js": "text/javascript",
  ".json": "application/json",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
};

const server = http.createServer((req, res) => {
  let reqPath = req.url.split("?")[0];
  if (reqPath === "/") reqPath = "/index.html";

  let filePath;
  if (reqPath.startsWith("/assets/")) {
    filePath = path.join(ASSETS_DIR, reqPath.replace("/assets/", ""));
  } else {
    filePath = path.join(RENDERER_DIR, reqPath);
  }

  // Safety check to prevent directory traversal outside repo
  const normalized = path.normalize(filePath);
  if (!normalized.startsWith(RENDERER_DIR) && !normalized.startsWith(ASSETS_DIR)) {
    res.writeHead(403, { "Content-Type": "text/plain" });
    res.end("Forbidden");
    return;
  }

  fs.stat(normalized, (err, stats) => {
    if (err || !stats.isFile()) {
      res.writeHead(404, { "Content-Type": "text/plain" });
      res.end("Not Found: " + reqPath);
      return;
    }

    const ext = path.extname(normalized).toLowerCase();
    const contentType = MIME_TYPES[ext] || "application/octet-stream";

    res.writeHead(200, { "Content-Type": contentType });
    fs.createReadStream(normalized).pipe(res);
  });
});

// Check if agent is already running before spawning
const testWs = new WebSocket("ws://127.0.0.1:8765");
testWs.on("open", () => {
  console.log("[bridge] agent sidecar is already running on ws://127.0.0.1:8765");
  testWs.close();
});
testWs.on("error", () => {
  console.log("[bridge] starting python agent sidecar...");
  startAgent();
});

server.listen(PORT, "127.0.0.1", () => {
  console.log("\n=======================================================");
  console.log("  🚀 Leutheria Web Interface is running!");
  console.log(`  🌐 Open in your browser: http://localhost:${PORT}`);
  console.log("  🤖 Agent WebSocket sidecar: ws://127.0.0.1:8765");
  console.log("=======================================================\n");
});

function cleanup() {
  if (agentProcess) {
    try {
      agentProcess.kill();
    } catch (_e) {}
  }
  process.exit(0);
}

process.on("SIGINT", cleanup);
process.on("SIGTERM", cleanup);
