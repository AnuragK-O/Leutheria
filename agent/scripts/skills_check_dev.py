"""Check of the dev / web / clipboard tools and skills.

    cd agent
    ./.venv/bin/python scripts/skills_check_dev.py

Every skill is driven through the real dispatch() with a fake websocket that
answers confirmations and records what was asked. Nothing here can touch the
user's machine:

  home        HOME points at a temp dir before anything is imported, so every
              "~" default (~/Projects, ~/Notes, ~/Scratch) lands there, and
              git reads its config from there.
  browser     open_url / open_app are replaced by recorders; open_with's one
              process launch is replaced too, so its own checks still run.
              The checks assert on the URLs and argv instead.
  clipboard   get_clipboard / set_clipboard are replaced by an in-memory
              fake; the real clipboard is never read or written.
  speech      the dispatcher's spoken confirmation prompt is a no-op.
  network     fetch_url runs against an http.server on 127.0.0.1. One real
              request (example.com) is made at the end and skipped offline.
  state       event log, preferences and trust files are temp files.

write_new_file belongs to the files branch. If the registry doesn't have it
(this branch on its own), a test-only implementation of the agreed contract is
registered below, in this process only. Once the real tool is merged the stub
is simply never installed and the same checks run against the real one.

Prints one line per check and exits non-zero on the first failure.
"""

import asyncio
import gzip
import html.parser
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

REAL_HOME = Path.home()
TMP = Path(tempfile.mkdtemp(prefix="leutheria_dev_check_")).resolve()
HOME = TMP / "home"
HOME.mkdir()
os.environ["HOME"] = str(HOME)
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ.pop("GIT_DIR", None)
os.environ.pop("GIT_WORK_TREE", None)
(HOME / ".gitconfig").write_text(
    "[user]\n\tname = Dev Check\n\temail = dev-check@example.invalid\n"
    "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
)

from agent.core import logging_util  # noqa: E402

logging_util.LOG_DIR = TMP / "logs"
logging_util.LOG_FILE = logging_util.LOG_DIR / "events.jsonl"

from agent.core import preferences, trust  # noqa: E402

preferences.PREFS_FILE = TMP / "preferences.json"
preferences._overrides.clear()
trust.TRUST_FILE = TMP / "trusted_commands.json"
trust._permanent_trust.clear()
trust._session_trust.clear()

from agent.core import dispatcher, path_policy  # noqa: E402

# The check's scratch folders sit beside the temp home, under /private/var:
# a user-content folder for this run, as far as the path policy goes.
path_policy.EXTRA_FREE_ROOTS.append(str(TMP))
from agent.skills import git_snapshot as git_snapshot_skill  # noqa: E402
from agent.skills.code_scratchpad import LANGUAGES  # noqa: E402
from agent.skills.registry import BUILTIN_SKILLS, SKILLS  # noqa: E402
from agent.tools import fetch_url as fetch_mod  # noqa: E402
from agent.tools import git_status as git_mod  # noqa: E402
from agent.tools import open_with as open_with_mod  # noqa: E402
from agent.tools.registry import TOOLS  # noqa: E402

DEV_TOOLS = ["fetch_url", "git_status", "open_with"]
DEV_SKILLS = [
    "new_python_project", "new_node_project", "new_web_page", "git_snapshot", "git_sync",
    "clone_repo", "repo_overview", "open_workspace", "web_search", "research_topic",
    "define_term", "github_open", "clipboard_to_note", "clipboard_to_file",
    "save_page_notes", "read_later", "code_scratchpad",
]

passed = 0


def check(condition, label):
    global passed
    if not condition:
        print(f"FAIL  {label}")
        sys.exit(1)
    passed += 1
    print(f"ok    {label}")


def section(title):
    print(f"\n--- {title}")


# --- stand-ins ---------------------------------------------------------------

HAS_REAL_WRITE_NEW_FILE = "write_new_file" in TOOLS


def _stub_write_new_file(path: str, content: str) -> dict:
    """The agreed contract and nothing more: create only if absent. It does
    not create parent folders, so a skill that forgot to would fail here."""
    target = Path(path).expanduser()
    try:
        with target.open("x", encoding="utf-8") as f:
            f.write(content)
    except FileExistsError:
        return {"ok": False, "error": f"{target} already exists"}
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(target)}


def install_write_new_file_stub():
    TOOLS["write_new_file"] = {
        "fn": _stub_write_new_file,
        "safety": "safe",
        "description": "TEST STUB (scripts/skills_check_dev.py): create a file only if it does not exist.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    }


opened_urls = []
opened_apps = []
launched = []
notifications = []


class Clipboard:
    text = ""


def _fake_open_url(url: str) -> dict:
    opened_urls.append(url)
    return {"ok": True, "message": f"opened {url}"}


def _fake_open_app(name: str) -> dict:
    opened_apps.append(name)
    return {"ok": True, "message": f"opened {name}"}


def _fake_launch(argv: list) -> subprocess.CompletedProcess:
    launched.append(argv)
    if argv[2] == "No Such App":
        return subprocess.CompletedProcess(argv, 1, "", "Unable to find application named 'No Such App'")
    return subprocess.CompletedProcess(argv, 0, "", "")


def _fake_get_clipboard() -> dict:
    return {"ok": True, "text": Clipboard.text}


def _fake_set_clipboard(text: str) -> dict:
    Clipboard.text = text
    return {"ok": True, "message": "copied to clipboard"}


def _fake_notification(title: str, message: str) -> dict:
    notifications.append((title, message))
    return {"ok": True, "message": "notification shown"}


async def _silent_speak(websocket, text):
    return None


TOOLS["open_url"]["fn"] = _fake_open_url
TOOLS["open_app"]["fn"] = _fake_open_app
TOOLS["get_clipboard"]["fn"] = _fake_get_clipboard
TOOLS["set_clipboard"]["fn"] = _fake_set_clipboard
TOOLS["show_notification"]["fn"] = _fake_notification
open_with_mod._launch = _fake_launch
dispatcher.speak = _silent_speak


class FakeSocket:
    """Stands in for the UI: answers every confirmation at once (approve
    unless told otherwise) and remembers what it was asked."""

    def __init__(self, pending):
        self.pending = pending
        self.asked = []
        self.approve = True

    async def send(self, raw):
        message = json.loads(raw)
        if message.get("type") != "confirmation_required":
            return
        self.asked.append((message["tool"], message["args"]))
        approve = self.approve(message["tool"], message["args"]) if callable(self.approve) else self.approve
        self.pending[message["id"]].set_result({"approved": bool(approve), "remember": None})


PENDING = {}
SOCKET = FakeSocket(PENDING)


async def call(name, args, approve=True):
    """Dispatch one capability; returns (result, confirmations asked)."""
    SOCKET.approve = approve
    SOCKET.asked = []
    result = await dispatcher.dispatch(name, args, SOCKET, PENDING)
    return result, list(SOCKET.asked)


def step_tools(result):
    return [s["tool"] for s in result.get("steps", [])]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30)


# --- local web server ----------------------------------------------------------

STOP = threading.Event()
PAGE = (
    "<!DOCTYPE html><html><head><title>Test  Page &amp; Co</title>"
    "<style>body{color:red}</style><script>var secret='SCRIPT_TEXT';</script></head>"
    "<body><h1>Heading</h1><p>First   paragraph&nbsp;here.</p>"
    "<script>document.write('MORE_SCRIPT')</script><noscript>NOSCRIPT_TEXT</noscript>"
    "<ul><li>one</li><li>two</li></ul><p>caf&eacute; <b>bold</b>text</p></body></html>"
)
ARTICLE = (
    "<html><head><title>Real Title</title></head><body>"
    "<header><svg><title>Logo Title</title><text>SVG_TEXT</text></svg></header>"
    "<nav><ul>" + "<li><a href='/x'>MENU_ITEM</a></li>" * 200 + "</ul></nav>"
    "<main><h1>The Article</h1><p>" + "Body sentence number one. " * 20 + "</p>"
    "<aside>RELATED_LINKS</aside><button>CLICK_ME</button><p>Closing paragraph.</p></main>"
    "<footer>FOOTER_TEXT</footer></body></html>"
)
HITS = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", ctype="text/html; charset=utf-8", headers=None):
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        route = parts.path
        HITS.append(self.path)
        try:
            if route == "/page":
                self._send(200, PAGE.encode())
            elif route == "/article":
                self._send(200, ARTICLE.encode())
            elif route == "/unclosed-nav":
                self._send(200, ("<html><body><nav><a>Home</a><p>" + "Real content sentence. " * 20 + "</p></body></html>").encode())
            elif route == "/plain":
                self._send(200, b"just plain text\nsecond line\n", "text/plain")
            elif route == "/json":
                self._send(200, b'{"answer": 42}', "application/json")
            elif route == "/latin1":
                self._send(200, "<html><body><p>na\xefve</p></body></html>".encode("latin-1"), "text/html; charset=ISO-8859-1")
            elif route == "/redirect":
                self._send(302, headers={"Location": "/page"})
            elif route == "/chain":
                n = int(parse_qs(parts.query).get("n", ["0"])[0])
                self._send(301, headers={"Location": f"/chain?n={n - 1}" if n > 1 else "/page"})
            elif route == "/loop":
                self._send(302, headers={"Location": "/loop"})
            elif route == "/to-file":
                self._send(302, headers={"Location": "file:///etc/passwd"})
            elif route == "/to-ftp":
                self._send(302, headers={"Location": "ftp://example.com/x"})
            elif route == "/to-private":
                self._send(302, headers={"Location": "http://127.0.0.1:9/secret"})
            elif route == "/png":
                self._send(200, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")
            elif route == "/pdf":
                self._send(200, b"%PDF-1.7", "application/pdf")
            elif route == "/binary-untyped":
                self._send(200, b"\x00\x01\x02\x03" * 100, None)
            elif route == "/missing":
                self._send(404, b"<html><body>nope</body></html>")
            elif route == "/huge":
                self._send(200, None, "text/plain")
                line = b"0123456789abcdef" * 64 + b"\n"
                for _ in range(5000):  # ~5 MB
                    self.wfile.write(line)
            elif route == "/longpage":
                self._send(200, ("<html><body>" + "<p>word word word</p>" * 5000 + "</body></html>").encode())
            elif route == "/gzip":
                self._send(200, gzip.compress(PAGE.encode()), headers={"Content-Encoding": "gzip"})
            elif route == "/bomb":
                self._send(200, gzip.compress(b"A" * 30_000_000), "text/plain", {"Content-Encoding": "gzip"})
            elif route == "/slow":
                STOP.wait(30)
                self._send(200, b"late", "text/plain")
            elif route == "/drip":
                self._send(200, None, "text/plain")
                while not STOP.is_set():
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            elif route == "/empty":
                self._send(200, b"<html><body><script>x()</script></body></html>")
            else:
                self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass


# --- helpers for the scaffold checks ---------------------------------------------

VOID_TAGS = {"meta", "link", "br", "hr", "img", "input"}


class WellFormed(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.doctype, self.text, self.tags = [], [], False, {}, []

    def handle_decl(self, decl):
        self.doctype = decl.lower() == "doctype html"

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}>")
        else:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack:
            self.text[self.stack[-1]] = self.text.get(self.stack[-1], "") + data


def parse_html(markup):
    parser = WellFormed()
    parser.feed(markup)
    parser.close()
    return parser


def tree(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file())


# --- the checks ----------------------------------------------------------------------


async def check_registry():
    section("registry")
    for name in DEV_TOOLS:
        check(name in TOOLS and TOOLS[name]["safety"] == "safe", f"tool {name} registered as safe")
    for name in DEV_SKILLS:
        check(name in SKILLS and name in BUILTIN_SKILLS, f"skill {name} registered as built-in")
    check(not (set(DEV_SKILLS) & set(TOOLS)), "no dev skill shares a name with a tool")
    check(list(TOOLS)[-len(DEV_TOOLS):] == DEV_TOOLS or HAS_REAL_WRITE_NEW_FILE,
          "dev tools are one block at the end of TOOLS")
    for name in DEV_TOOLS + DEV_SKILLS:
        entry = TOOLS.get(name) or SKILLS[name]
        texts = [entry["description"]] + [
            p.get("description", "") for p in entry["input_schema"]["properties"].values()
        ]
        # Prompt surface, and read aloud when echoed: no markdown, no escapes
        # (BUGS.md #12). Underscores are fine -- tool names have them.
        bad = sorted({c for c in "\\\n\t*`#[]" for t in texts if c in t})
        check(not bad, f"{name}: description has no markdown or escape characters {bad}")
        lowered = entry["description"].lower()
        check(len(lowered) >= 80 and any(w in lowered for w in (" not ", "never", "cannot", "refuses")),
              f"{name}: description says what it is not for")
        check(set(entry["input_schema"].get("required", [])) <= set(entry["input_schema"]["properties"]),
              f"{name}: required params are declared")
    if not HAS_REAL_WRITE_NEW_FILE:
        result, _ = await call("new_python_project", {"name": "nostub", "base_dir": str(TMP / "nostub")})
        check(not result["ok"] and "unknown tool: write_new_file" in result["error"],
              "without write_new_file a scaffold fails cleanly and names the missing tool")
        check(isinstance(result.get("steps"), list) and result.get("message"), "the failure still carries steps and a message")
        install_write_new_file_stub()
        print("      (write_new_file not in the registry: test stub installed for this run)")
    else:
        print("      (write_new_file is the real tool from the registry)")
    probe = TMP / "probe.txt"
    first = TOOLS["write_new_file"]["fn"](path=str(probe), content="one")
    second = TOOLS["write_new_file"]["fn"](path=str(probe), content="two")
    check(first.get("ok") and first.get("path") and not second.get("ok") and probe.read_text() == "one",
          "write_new_file contract: creates, returns path, refuses an existing file")


async def check_fetch(base):
    section("fetch_url")
    fetch = fetch_mod.fetch_url

    # Scheme and host rules, with the production setting (private hosts refused).
    for url, why in [
        ("file:///etc/passwd", "file:"),
        ("ftp://example.com/x", "ftp:"),
        ("data:text/plain,hello", "data:"),
        ("javascript:alert(1)", "javascript:"),
        ("gopher://example.com/", "gopher:"),
        ("example.com/page", "no scheme"),
        ("", "empty"),
        ("http://", "no host"),
        ("https://user:pw@example.com/", "embedded credentials"),
        ("http://example.com:notaport/", "bad port"),
    ]:
        result = fetch(url)
        check(result["ok"] is False and result.get("error"), f"rejects {why}: {result.get('error')}")
    before = len(HITS)
    for url in [f"{base}/page", "http://localhost/", "http://[::1]/", "http://169.254.169.254/latest/",
                "http://10.0.0.1/", "http://192.168.1.1/", "http://2130706433/", "http://0.0.0.0/"]:
        result = fetch(url)
        check(result["ok"] is False and "public" in result["error"], f"refuses non-public host {url}")
    check(len(HITS) == before, "a refused host is never contacted")

    # From here the test server on 127.0.0.1 is allowed -- and only it: every
    # other host still goes through the real check.
    real_allowed = fetch_mod._address_allowed
    port = urlsplit(base).port

    def only_test_server(address, to_port):
        return (address == "127.0.0.1" and to_port == port) or real_allowed(address, to_port)

    fetch_mod._address_allowed = only_test_server

    # The address that is checked is the address that is connected to: one
    # lookup per hop, made by the connection itself, so there is no second
    # lookup for a rebinding DNS server to answer differently.
    lookups = []
    real_resolve = fetch_mod._resolve
    fetch_mod._resolve = lambda host, to_port: (lookups.append(host), real_resolve(host, to_port))[1]
    one = fetch(f"{base}/page")
    two = fetch(f"{base}/redirect")
    fetch_mod._resolve = real_resolve
    check(one["ok"] and two["ok"] and len(lookups) == 3, f"exactly one DNS lookup per hop ({len(lookups)} for 3 hops)")
    fetch_mod._resolve = lambda host, to_port: [(2, 1, 6, "", ("127.0.0.1", port)), (2, 1, 6, "", ("10.1.2.3", port))]
    mixed = fetch(f"http://mixed.invalid:{port}/page")
    fetch_mod._resolve = real_resolve
    check(mixed["ok"] is False and "public" in mixed["error"], "a name with one allowed and one private address is refused outright")

    page = fetch(f"{base}/page")
    check(page["ok"] and page["status"] == 200 and page["content_type"] == "text/html", f"fetches an HTML page ({page.get('error')})")
    check(page["title"] == "Test Page & Co", f"title extracted and unescaped: {page['title']!r}")
    text = page["text"]
    check("Heading" in text and "First paragraph here." in text and "café bold text" not in text and "café boldtext" in text,
          f"readable text, entities decoded, whitespace collapsed: {text!r}")
    check(not any(s in text for s in ("SCRIPT_TEXT", "MORE_SCRIPT", "NOSCRIPT_TEXT", "color:red", "<")),
          "script, style, noscript and tags are stripped")
    check("one\n" in text + "\n" and text.index("one") < text.index("two"), "list items land on separate lines")
    check(page["truncated"] is False and page["redirects"] == 0, "not truncated, no redirects")

    article = fetch(f"{base}/article", max_chars=300)
    check(article["title"] == "Real Title", f"an inline svg's title is not the page title: {article['title']!r}")
    check(article["text"].startswith("The Article\n\nBody sentence number one."),
          f"a page with <main> starts at its content, not its 200-item menu: {article['text'][:50]!r}")
    full = fetch(f"{base}/article", max_chars=20000)["text"]
    check("Closing paragraph." in full and not any(s in full for s in ("MENU_ITEM", "FOOTER_TEXT", "RELATED_LINKS", "CLICK_ME", "SVG_TEXT")),
          "menus, sidebars, buttons and footers are left out of it")
    check("Real content sentence." in fetch(f"{base}/unclosed-nav")["text"], "a page that never closes its <nav> still returns its text")

    plain = fetch(f"{base}/plain")
    check(plain["ok"] and plain["text"] == "just plain text\nsecond line" and plain["content_type"] == "text/plain", "plain text passes through")
    data = fetch(f"{base}/json")
    check(data["ok"] and json.loads(data["text"]) == {"answer": 42}, "JSON is returned as text")
    check(fetch(f"{base}/latin1")["text"] == "naïve", "charset from the Content-Type header is honoured")
    check(fetch(f"{base}/gzip").get("title") == "Test Page & Co", "gzip content is decoded")

    redirected = fetch(f"{base}/redirect")
    check(redirected["ok"] and redirected["redirects"] == 1 and redirected["url"].endswith("/page"), "follows a redirect and reports the final URL")
    check(fetch(f"{base}/chain?n=5")["redirects"] == 5, "follows five redirects")
    for route, needle in [("/chain?n=6", "too many redirects"), ("/loop", "too many redirects"),
                          ("/to-file", "only http and https"), ("/to-ftp", "only http and https"),
                          ("/to-private", "public")]:
        result = fetch(f"{base}{route}")
        check(result["ok"] is False and needle in result["error"], f"{route}: {result['error']}")

    for route, needle in [("/png", "image/png"), ("/pdf", "application/pdf"), ("/binary-untyped", "binary"),
                          ("/missing", "HTTP 404")]:
        result = fetch(f"{base}{route}")
        check(result["ok"] is False and needle in result["error"], f"{route}: {result['error']}")

    started = time.monotonic()
    huge = fetch(f"{base}/huge", max_chars=20000)
    check(huge["ok"] and huge["truncated"] is True and len(huge["text"]) == 20000,
          f"5 MB body: truncated to the cap in {time.monotonic() - started:.2f}s")
    capped = fetch(f"{base}/huge", max_chars=10**9)
    check(len(capped["text"]) == fetch_mod.MAX_CHARS, "max_chars cannot exceed the tool's own cap")
    small = fetch(f"{base}/longpage")
    check(small["ok"] and small["truncated"] and len(small["text"]) == fetch_mod.DEFAULT_CHARS, "a long page is cut to the default and says so")
    check(fetch(f"{base}/page", max_chars="lots")["ok"], "a non-integer max_chars falls back to the default")
    bomb = fetch(f"{base}/bomb")
    check(bomb["ok"] and bomb["truncated"] and len(bomb["text"]) <= fetch_mod.MAX_CHARS, "a 30 MB gzip bomb is cut off while inflating")

    saved = (fetch_mod.TIMEOUT, fetch_mod.DEADLINE)
    fetch_mod.TIMEOUT, fetch_mod.DEADLINE = 1.0, 2.5
    try:
        started = time.monotonic()
        slow = fetch(f"{base}/slow")
        elapsed = time.monotonic() - started
        check(slow["ok"] is False and "timed out" in slow["error"] and elapsed < 2.5, f"slow response times out ({elapsed:.1f}s): {slow['error']}")
        started = time.monotonic()
        drip = fetch(f"{base}/drip")
        elapsed = time.monotonic() - started
        check(drip["ok"] is False and "timed out" in drip["error"] and elapsed < 4.5, f"endless drip hits the overall deadline ({elapsed:.1f}s)")
    finally:
        fetch_mod.TIMEOUT, fetch_mod.DEADLINE = saved

    # Nothing listens here; the patched host check lets this one through.
    closed = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    closed_port = closed.server_address[1]
    closed.server_close()
    fetch_mod._address_allowed = lambda address, to_port: True
    dead = fetch(f"http://127.0.0.1:{closed_port}/x")
    fetch_mod._address_allowed = only_test_server
    check(dead["ok"] is False and "could not reach the server" in dead["error"], f"connection refused is a clean error: {dead['error']}")

    result, asked = await call("fetch_url", {"url": f"{base}/page"})
    check(result["ok"] and asked == [], "through dispatch: runs with no confirmation")
    result, _ = await call("fetch_url", {"url": f"{base}/page", "bogus": 1})
    check(result["ok"] is False, "through dispatch: an unknown argument is a clean error")
    return real_allowed


async def check_git():
    section("git_status")
    status = git_mod.git_status
    plain = TMP / "not a repo"
    plain.mkdir()
    result = status(str(plain))
    check(result["ok"] is False and result["error"] == f"{plain} is not a git repository", f"not a repository: {result['error']}")
    check(status(str(TMP / "nope"))["ok"] is False, "a missing folder is a clean error")
    check(status("")["ok"] is False, "an empty path is a clean error")

    repo = TMP / "repo with 'quote"
    repo.mkdir()
    check(git(repo, "init").returncode == 0, "temp repository created")
    fresh = status(str(repo))
    check(fresh["ok"] and fresh["branch"] == "main" and fresh["no_commits"] and fresh["clean"] and fresh["commits"] == [],
          f"fresh repository: {fresh.get('message')}")

    (repo / "a.txt").write_text("a\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.txt").write_text("b\n")
    result = status(str(repo))
    check(result["untracked"] == 2 and result["changed_files"] == 2 and not result["clean"], "untracked files are counted one by one, inside folders too")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "first commit")
    (repo / "a.txt").write_text("a2\n")
    (repo / "c.txt").write_text("c\n")
    git(repo, "add", "c.txt")
    (repo / "d.txt").write_text("d\n")
    result = status(str(repo / "sub"))
    check(result["ok"] and (result["staged"], result["modified"], result["untracked"]) == (1, 1, 1),
          f"staged / modified / untracked from a subfolder: {result['message']}")
    check(Path(result["root"]) == repo and result["commits"][0]["subject"] == "first commit", "root and last commit reported")
    check({f["path"] for f in result["files"]} == {"a.txt", "c.txt", "d.txt"}, "changed files listed")
    check(result["remote_url"] is None and result["upstream"] is None and result["upstream_note"] is None
          and "fetch" not in result["message"], "no remote: remote_url is None, and no upstream caveat")

    result, asked = await call("git_status", {"path": str(repo), "log_count": 99})
    check(result["ok"] and asked == [] and len(result["commits"]) == 1, "through dispatch: no confirmation, log_count clamped")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "second")
    for i in range(3):
        git(repo, "commit", "--allow-empty", "-m", f"empty {i}")
    check(len(status(str(repo), log_count=2)["commits"]) == 2, "log_count limits the commits")
    check(status(str(repo), log_count=0)["commits"] == [], "log_count 0 skips the log")

    # Read-only for real: status must not rewrite the index...
    os.utime(repo / "a.txt", (time.time() + 5, time.time() + 5))
    index = repo / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    status(str(repo))
    check((index.read_bytes(), index.stat().st_mtime_ns) == before, "git status does not refresh .git/index")

    # ...and must not run programs named by the repository's own config.
    marker = TMP / "pwned"
    hook = TMP / "evil.sh"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    hook.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(hook))
    git(repo, "config", "log.showSignature", "true")
    git(repo, "config", "gpg.program", str(hook))
    git(repo, "config", "core.pager", str(hook))
    result = status(str(repo))
    check(result["ok"] and not marker.exists(), "a hostile core.fsmonitor / gpg.program / pager in the repo config is not run")
    control = git(repo, "status", "--porcelain")
    check(marker.exists(), "(control: plain git status does run that fsmonitor program)")
    marker.unlink()
    for key in ("core.fsmonitor", "log.showSignature", "gpg.program", "core.pager"):
        git(repo, "config", "--unset", key)
    del control

    git(repo, "remote", "add", "origin", "https://someone:ghp_SECRETTOKEN@github.com/acme/widgets.git")
    result = status(str(repo))
    check(result["remote_url"] == "https://github.com/acme/widgets.git" and "SECRET" not in json.dumps(result),
          "credentials are stripped from the remote URL")
    git(repo, "remote", "remove", "origin")

    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "--detach", head)
    result = status(str(repo))
    check(result["detached"] and result["branch"] is None and result["message"].startswith("Detached HEAD"), "detached HEAD reported")
    git(repo, "checkout", "main")

    saved = git_mod._GIT
    git_mod._GIT = ["git-binary-that-does-not-exist"]
    check(status(str(repo))["error"] == "git is not installed", "missing git binary is a clean error")
    git_mod._GIT = saved
    return repo


async def check_open_with():
    section("open_with")
    folder = TMP / "proj folder"
    folder.mkdir()
    launched.clear()
    result, asked = await call("open_with", {"path": str(folder), "app": "Visual Studio Code"})
    check(result["ok"] and asked == [] and launched == [["open", "-a", "Visual Studio Code", str(folder)]],
          f"opens a folder with fixed argv: {launched}")
    launched.clear()
    for app in ["Terminal", "terminal.app", "iTerm", "Script Editor", "Python Launcher", "Keychain Access", "Warp",
                "kitty", "Alacritty", "Automator", "Shortcuts", "Installer", "  TERMINAL  ", "Archive Utility",
                "DiskImageMounter", "System Settings", "Jar Launcher"]:
        result, _ = await call("open_with", {"path": str(folder), "app": app})
        check(result["ok"] is False and "never" in result["error"], f"refuses {app}")
    for app in ["/tmp/Evil.app", "-R", "", "../x"]:
        result, _ = await call("open_with", {"path": str(folder), "app": app})
        check(result["ok"] is False, f"refuses app given as {app!r}")
    for name in ["run.command", "Thing.app", "tool.scpt", "setup.pkg", "x.terminal", "build.sh", "RUN.SH", "app.jar",
                 "image.dmg", "wifi.mobileconfig", "do.shortcut", "link.webloc", "flow.workflow", "a.zsh"]:
        target = TMP / name
        target.mkdir() if name.endswith((".app", ".workflow")) else target.write_text("x")
        result, _ = await call("open_with", {"path": str(target), "app": "TextEdit"})
        check(result["ok"] is False and "rather than opened" in result["error"], f"refuses {name}")

    # A symlink is judged by what it points at, however innocent its name.
    for link_name, points_at in [("innocent.txt", "run.command"), ("notes.md", "build.sh"), ("photos", "Thing.app")]:
        link = TMP / link_name
        link.symlink_to(TMP / points_at)
        result, _ = await call("open_with", {"path": str(link), "app": "TextEdit"})
        check(result["ok"] is False and "rather than opened" in result["error"], f"refuses {link_name}, a symlink to {points_at}")

    # Executables with no telling suffix.
    exe = TMP / "report.txt"
    exe.write_text("#!/bin/sh\ntouch pwned\n")
    exe.chmod(0o755)
    result, _ = await call("open_with", {"path": str(exe), "app": "TextEdit"})
    check(result["ok"] is False and "executable file" in result["error"], "refuses a file with the execute bit set, whatever its name")
    link = TMP / "summary.txt"
    link.symlink_to(exe)
    check((await call("open_with", {"path": str(link), "app": "TextEdit"}))[0]["ok"] is False, "and a symlink to one")
    binary = TMP / "data.bin"
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
    result, _ = await call("open_with", {"path": str(binary), "app": "TextEdit"})
    check(result["ok"] is False and "compiled program" in result["error"], "refuses a Mach-O binary with no execute bit")
    script = TMP / "deploy"
    script.write_text("#!/bin/sh\necho hi\n")
    result, _ = await call("open_with", {"path": str(script), "app": "TextEdit"})
    check(result["ok"] is False and "no file extension" in result["error"], "refuses an extensionless script")
    fifo = TMP / "pipe"
    os.mkfifo(fifo)
    check((await call("open_with", {"path": str(fifo), "app": "TextEdit"}))[0]["ok"] is False, "refuses something that is neither file nor folder")

    # Finder opening a file is a double-click, so Finder gets folders only.
    doc = TMP / "plain.txt"
    doc.write_text("hello")
    result, _ = await call("open_with", {"path": str(doc), "app": "Finder"})
    check(result["ok"] is False and "folders only" in result["error"], "Finder is refused a file")
    check(launched == [], "nothing was launched by any refused call so far")

    # What must keep working.
    for target, app in [(folder, "Finder"), (folder, "Visual Studio Code"), (doc, "TextEdit")]:
        launched.clear()
        result, _ = await call("open_with", {"path": str(target), "app": app})
        check(result["ok"] and launched == [["open", "-a", app, str(target)]], f"opens {target.name} in {app}")
    for name in ["main.py", "index.js", "Makefile", "page.html", "data.json"]:
        target = TMP / name
        target.write_text("all:\n\techo hi\n" if name == "Makefile" else "x = 1\n")
        launched.clear()
        result, _ = await call("open_with", {"path": str(target), "app": "Visual Studio Code"})
        check(result["ok"] and len(launched) == 1, f"opens the source file {name} in an editor")
    good_link = TMP / "shortcut-to-folder"
    good_link.symlink_to(folder)
    launched.clear()
    await call("open_with", {"path": str(good_link), "app": "Visual Studio Code"})
    check(launched == [["open", "-a", "Visual Studio Code", str(folder)]], "a symlink to a folder opens the resolved folder")
    launched.clear()
    inner = TMP / "Thing.app" / "inner.txt"
    inner.write_text("x")
    check((await call("open_with", {"path": str(inner), "app": "TextEdit"}))[0]["ok"] is False, "refuses a file inside an app bundle")
    result, _ = await call("open_with", {"path": str(TMP / "missing.txt"), "app": "TextEdit"})
    check(result["ok"] is False and "does not exist" in result["error"], "a missing path is a clean error")
    check(launched == [], "nothing was launched by the remaining refused calls")
    result, _ = await call("open_with", {"path": str(folder), "app": "No Such App"})
    check(result["ok"] is False and result["error"] == "there is no application named No Such App", "unknown app is a clean error")
    (HOME / "tilde.txt").write_text("x")
    launched.clear()
    await call("open_with", {"path": "~/tilde.txt", "app": "TextEdit"})
    check(launched and launched[0][3] == str(HOME / "tilde.txt"), "~ is expanded before the path is passed on")


async def check_scaffolds():
    section("new_python_project / new_node_project / new_web_page")
    base = TMP / "projects"

    result, asked = await call("new_python_project", {"name": "atlas", "base_dir": str(base)})
    root = base / "atlas"
    check(result["ok"] and asked == [], f"python project created with no confirmation: {result.get('message')}")
    check(tree(root) == [".gitignore", "README.md", "main.py", "requirements.txt"], f"files: {tree(root)}")
    check(step_tools(result) == ["create_folder"] + ["write_new_file"] * 4, "every step went through dispatch")
    ran = subprocess.run([sys.executable, "main.py"], cwd=root, capture_output=True, text=True, timeout=30)
    check(ran.returncode == 0 and ran.stdout.strip() == "Hello from atlas!", f"python main.py runs: {ran.stdout.strip()!r}")
    check(".venv/" in (root / ".gitignore").read_text() and not (root / ".git").exists() and not (root / ".venv").exists(),
          "no git init and no venv unless asked")

    snapshot = {p: (root / p).read_text() for p in tree(root)}
    (root / "main.py").write_text("print('my work')\n")
    again, asked = await call("new_python_project", {"name": "atlas", "base_dir": str(base), "git_init": True})
    check(again["ok"] is False and "not empty" in again["error"] and asked == [] and step_tools(again) == [],
          f"refuses an existing non-empty folder before doing anything: {again['error']}")
    check((root / "main.py").read_text() == "print('my work')\n" and tree(root) == sorted(snapshot), "the existing project is untouched")
    for other in ("new_node_project", "new_web_page"):
        result, _ = await call(other, {"name": "atlas", "base_dir": str(base)})
        check(result["ok"] is False and "not empty" in result["error"], f"{other} refuses the same folder")

    (base / "afile").write_text("x")
    result, _ = await call("new_python_project", {"name": "afile", "base_dir": str(base)})
    check(result["ok"] is False and "not a folder" in result["error"], "a file of that name stops it")
    (base / "empty").mkdir()
    result, _ = await call("new_python_project", {"name": "empty", "base_dir": str(base)})
    check(result["ok"] and (base / "empty" / "main.py").exists(), "an existing empty folder is fine")
    for bad in ["../escape", "a/b", ".hidden", "-rf", "", "x" * 80, "name;rm", "$(whoami)", "trailing.", 7]:
        result, _ = await call("new_python_project", {"name": bad, "base_dir": str(base)})
        check(result["ok"] is False and "plain folder name" in result["error"], f"rejects project name {bad!r}")
    check(not (TMP / "escape").exists(), "nothing was written outside the base folder")

    # ~ default: lands in the (temp) home, expanded.
    result, _ = await call("new_python_project", {"name": "homeproj"})
    check(result["ok"] and (HOME / "Projects" / "homeproj" / "main.py").exists(), "default base_dir is ~/Projects, expanded")

    # Opt-in extras go through run_command -> asked, one prompt each, quoted.
    tricky = TMP / "it's a $HOME `dir`"
    result, asked = await call("new_python_project", {"name": "my app", "base_dir": str(tricky), "git_init": True, "open_in": "Visual Studio Code"})
    root = tricky / "my app"
    check(result["ok"] and [a[0] for a in asked] == ["run_command"], f"git_init asks exactly once: {asked}")
    check(shlex.split(asked[0][1]["cmd"]) == ["git", "-C", str(root), "init"], f"the command is quoted: {asked[0][1]['cmd']}")
    check((root / ".git").is_dir(), "git init really ran in a path with a quote, a space, $ and backticks")
    check(launched[-1] == ["open", "-a", "Visual Studio Code", str(root)], "open_in opens the project folder")
    ran = subprocess.run([sys.executable, "main.py"], cwd=root, capture_output=True, text=True, timeout=30)
    check(ran.stdout.strip() == "Hello from my app!", "a name with a space still yields a runnable main.py")

    result, asked = await call("new_python_project", {"name": "declined", "base_dir": str(base), "git_init": True}, approve=False)
    check(result["ok"] is False and len(asked) == 1 and "was created" in result["message"] and "declined" in result["message"],
          f"declined git init: reported, project kept: {result['message']}")
    check((base / "declined" / "main.py").exists() and not (base / "declined" / ".git").exists(), "files exist, git init did not run")

    result, asked = await call("new_python_project", {"name": "venvproj", "base_dir": str(base), "create_venv": True})
    venv = base / "venvproj" / ".venv"
    check(result["ok"] and len(asked) == 1 and shlex.split(asked[0][1]["cmd"]) == ["python3", "-m", "venv", str(venv)],
          "create_venv asks with the quoted command")
    check((venv / "bin" / "python").exists(), "the virtual environment was really created")
    result, asked = await call("new_python_project", {"name": "strflag", "base_dir": str(base), "git_init": "false", "create_venv": 0})
    check(result["ok"] and asked == [], "only a real true turns an extra on")

    # Node
    result, asked = await call("new_node_project", {"name": "Atlas API", "base_dir": str(base)})
    root = base / "Atlas API"
    check(result["ok"] and asked == [] and tree(root) == [".gitignore", "README.md", "index.js", "package.json"], f"node project: {tree(root)}")
    package = json.loads((root / "package.json").read_text())
    check(package["name"] == "atlas-api" and package["main"] == "index.js" and package["scripts"]["start"] == "node index.js" and package["private"] is True,
          f"package.json is valid and its name is npm-safe: {package['name']}")
    check("node_modules/" in (root / ".gitignore").read_text() and not (root / "node_modules").exists(), "no install unless asked")
    node = shutil.which("node")
    if node:
        ran = subprocess.run([node, "index.js"], cwd=root, capture_output=True, text=True, timeout=30)
        check(ran.returncode == 0 and ran.stdout.strip() == "Hello from Atlas API!", f"node index.js runs: {ran.stdout.strip()!r}")
    else:
        print("skip  node not installed: index.js not executed")
    result, asked = await call("new_node_project", {"name": "inst", "base_dir": str(base), "install": True, "git_init": True},
                               approve=lambda tool, args: "npm" not in args["cmd"])
    check([shlex.split(a[1]["cmd"]) for a in asked] == [["git", "-C", str(base / "inst"), "init"], ["npm", "install", "--prefix", str(base / "inst")]],
          "node extras ask in order: git init, then npm install")
    check(result["ok"] is False and "npm install failed or was declined" in result["message"] and (base / "inst" / ".git").is_dir(),
          "a declined npm install stops there and says so")

    # Web page
    hostile = 'Tom & "Jerry" <script>alert(1)</script>'
    opened_urls.clear()
    result, asked = await call("new_web_page", {"name": "site", "title": hostile, "base_dir": str(base), "open_in_browser": True})
    root = base / "site"
    check(result["ok"] and asked == [] and tree(root) == ["index.html", "script.js", "styles.css"], f"web page: {tree(root)}")
    parsed = parse_html((root / "index.html").read_text())
    check(parsed.doctype and not parsed.errors and not parsed.stack, f"index.html is well-formed {parsed.errors} {parsed.stack}")
    check(parsed.text.get("title") == hostile and parsed.text.get("h1") == hostile, "the title is escaped, and reads back exactly")
    check(parsed.tags.count("script") == 1 and "alert(1)</script>" not in (root / "index.html").read_text(), "a title cannot inject a script tag")
    check(opened_urls == [(root / "index.html").as_uri()], f"open_in_browser opens the file URL: {opened_urls}")
    if node:
        js = (root / "script.js").read_text()
        probe = "const document={getElementById:()=>({set textContent(v){console.log(v)}})};\n" + js
        ran = subprocess.run([node, "-e", probe], capture_output=True, text=True, timeout=30)
        check(ran.returncode == 0 and ran.stdout.strip() == f"{hostile} is ready.", f"script.js runs and carries the title: {ran.stdout.strip()!r}")
    css = (root / "styles.css").read_text()
    check(css.count("{") == css.count("}") == 2, "styles.css braces balance")
    result, _ = await call("new_web_page", {"name": "plainsite", "base_dir": str(base)})
    check(parse_html((base / "plainsite" / "index.html").read_text()).text.get("title") == "plainsite", "title defaults to the name")


async def check_git_skills(repo):
    section("git_snapshot / git_sync / clone_repo / repo_overview / open_workspace / github_open")
    result, asked = await call("git_snapshot", {"path": str(repo), "message": "nothing"})
    check(result["ok"] and result["committed"] is False and asked == [] and "Nothing to commit" in result["message"],
          "clean tree: nothing to commit, nothing asked")

    marker = TMP / "injected"
    message = f"fix: it's \"quoted\" $(touch {marker}) `touch {marker}` ; touch {marker}"
    (repo / "new.txt").write_text("new\n")
    (repo / "a.txt").write_text("changed\n")
    result, asked = await call("git_snapshot", {"path": str(repo / "sub"), "message": message})
    check(result["ok"] and result["committed"] and len(asked) == 1 and asked[0][0] == "run_command", f"commit asks once: {result.get('message')}")
    argv = shlex.split(asked[0][1]["cmd"])
    check(argv == ["git", "-C", str(repo), "add", "-A", "&&", "git", "-C", str(repo), "commit", "-m", message],
          "the confirmed command is exactly add -A then commit -m, with path and message quoted")
    check(git(repo, "log", "-1", "--format=%s").stdout.strip() == message and not marker.exists(),
          "the commit message landed verbatim and nothing in it was executed")
    check(git(repo, "status", "--porcelain").stdout == "", "everything was committed")
    check(step_tools(result) == ["git_status", "run_command"], "steps: git_status then run_command")

    (repo / "more.txt").write_text("more\n")
    result, asked = await call("git_snapshot", {"path": str(repo), "message": "declined one"}, approve=False)
    check(result["ok"] is False and result["committed"] is False and len(asked) == 1 and "Stopped at the commit" in result["error"],
          f"declined: {result['error']}")
    check("more.txt" in git(repo, "status", "--porcelain").stdout, "a declined commit changed nothing")

    (repo / "sub" / ".env").write_text("TOKEN=abc\n")
    result, asked = await call("git_snapshot", {"path": str(repo), "message": "oops"})
    check(result["ok"] is False and asked == [] and "sub/.env" in result["error"] and "secrets" in result["error"],
          f"an untracked .env stops the commit before any prompt: {result['error']}")
    check(git_snapshot_skill.sensitive_files([{"status": "??", "path": p} for p in
          ["a/.env.example", "src/key.py", "id_rsa", "x/server.pem", ".env.local", "notes.txt"]]) == ["id_rsa", "x/server.pem", ".env.local"],
          "secret-looking names are matched, look-alikes are not")
    result, asked = await call("git_snapshot", {"path": str(repo), "message": "with env", "allow_sensitive": True})
    check(result["ok"] and len(asked) == 1, "allow_sensitive commits it, still asking first")
    for args, why in [({"path": str(repo)}, "no message"), ({"message": "x"}, "no path"),
                      ({"path": str(repo), "message": "x" * 600}, "an overlong message"),
                      ({"path": str(TMP / "not a repo"), "message": "x"}, "not a repository")]:
        result, asked = await call("git_snapshot", args)
        check(result["ok"] is False and asked == [], f"git_snapshot with {why}: {result['error']}")

    # git_sync against a local bare repository standing in for the remote.
    result, asked = await call("git_sync", {"path": str(repo), "action": "push"})
    check(result["ok"] is False and asked == [] and "no remote" in result["error"], "git_sync without a remote stops, nothing asked")
    bare = TMP / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, timeout=30)
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-u", "origin", "main")
    result, asked = await call("git_sync", {"path": str(repo), "action": "push"})
    check(result["ok"] and asked == [] and "Nothing to push" in result["message"], "nothing to push: says so, nothing asked")
    git(repo, "commit", "--allow-empty", "-m", "ahead one")
    status, _ = await call("git_status", {"path": str(repo)})
    check(status["ahead"] == 1 and status["upstream"] == "origin/main", "git_status reports ahead of upstream")

    # Someone else pushes. Until this clone fetches, git_status cannot know --
    # and has to say that rather than report a confident "behind 0".
    other = TMP / "other clone"
    subprocess.run(["git", "clone", str(bare), str(other)], capture_output=True, timeout=30)
    git(other, "commit", "--allow-empty", "-m", "from elsewhere")
    git(other, "push", "origin", "main")
    fetch_head = repo / ".git" / "FETCH_HEAD"
    before = fetch_head.stat().st_mtime_ns if fetch_head.exists() else None
    status, _ = await call("git_status", {"path": str(repo)})
    after = fetch_head.stat().st_mtime_ns if fetch_head.exists() else None
    check(status["behind"] == 0 and before == after, "git_status does not fetch: a commit pushed from elsewhere is not seen")
    check(status["remote_checked"] is False and "cannot tell whether there is anything new to pull" in status["upstream_note"]
          and "the remote was not checked" in status["message"], f"and the result says so: {status['message']}")
    git(repo, "reset", "--hard", "origin/main")
    result, asked = await call("git_sync", {"path": str(repo), "action": "pull"})
    check(result["ok"] and len(asked) == 1 and git(repo, "log", "-1", "--format=%s").stdout.strip() == "from elsewhere",
          "git_sync pull still asks and pulls when git_status showed nothing behind")
    git(repo, "commit", "--allow-empty", "-m", "ahead one")
    status, _ = await call("git_status", {"path": str(repo)})
    result, asked = await call("git_sync", {"path": str(repo), "action": "push"})
    check(result["ok"] and shlex.split(asked[0][1]["cmd"]) == ["git", "-C", str(repo), "push"], f"push asks, then pushes: {result['message']}")
    check(subprocess.run(["git", "-C", str(bare), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip() == "ahead one", "the push arrived")
    result, asked = await call("git_sync", {"path": str(repo), "action": "pull"})
    check(result["ok"] and shlex.split(asked[0][1]["cmd"]) == ["git", "-C", str(repo), "pull", "--ff-only"], "pull asks and uses --ff-only")
    result, asked = await call("git_sync", {"path": str(repo), "action": "pull"}, approve=False)
    check(result["ok"] is False and len(asked) == 1, "a declined pull is reported")
    result, asked = await call("git_sync", {"path": str(repo), "action": "force-push"})
    check(result["ok"] is False and asked == [], "only pull and push are accepted")

    # clone_repo: never reaches the network here -- run_command is a recorder.
    ran = []
    real_run = TOOLS["run_command"]["fn"]
    TOOLS["run_command"]["fn"] = lambda cmd: (ran.append(cmd), {"ok": True, "stdout": "", "stderr": ""})[1]
    try:
        clones = TMP / "clones"
        result, asked = await call("clone_repo", {"repo": "acme/widgets", "base_dir": str(clones)})
        check(result["ok"] and len(asked) == 1 and shlex.split(ran[-1]) == ["git", "clone", "--", "https://github.com/acme/widgets", str(clones / "widgets")],
              f"owner/name clones from GitHub after asking: {ran[-1]}")
        result, _ = await call("clone_repo", {"repo": "https://gitlab.com/group/thing.git", "base_dir": str(clones), "name": "mine"})
        check(shlex.split(ran[-1])[-2:] == ["https://gitlab.com/group/thing.git", str(clones / "mine")], "an https URL and a custom folder name")
        count = len(ran)
        for bad in ["git@github.com:a/b.git", "ssh://git@github.com/a/b", "file:///etc", "/local/path", "ext::sh -c id",
                    "http://github.com/a/b", "https://u:p@github.com/a/b", "--upload-pack=x", "a/b; rm -rf ~", "", "https://github.com/"]:
            result, asked = await call("clone_repo", {"repo": bad, "base_dir": str(clones)})
            check(result["ok"] is False and asked == [], f"clone_repo rejects {bad!r}")
        (clones / "exists").mkdir(parents=True)
        result, asked = await call("clone_repo", {"repo": "acme/exists", "base_dir": str(clones)})
        check(result["ok"] is False and asked == [] and "already exists" in result["error"] and len(ran) == count,
              "an existing destination stops the clone before anything is asked")
    finally:
        TOOLS["run_command"]["fn"] = real_run

    # repo_overview
    (repo / "README.md").write_text("# Widgets\n\nThings that widge.\n")
    (repo / "pyproject.toml").write_text("[project]\nname='w'\n")
    result, asked = await call("repo_overview", {"path": str(repo / "sub")})
    check(result["ok"] and asked == [] and step_tools(result) == ["git_status", "list_files", "read_file"], "repo_overview: three read-only steps")
    check(result["readme_excerpt"].startswith("# Widgets") and result["project_kinds"] == ["Python"] and ".git" not in result["top_level"]
          and "README.md" in result["top_level"] and result["branch"] == "main", f"overview: {result['message']}")
    result, _ = await call("repo_overview", {"path": str(TMP / "not a repo")})
    check(result["ok"] is False and "not a git repository" in result["error"], "repo_overview outside a repository says so")

    # open_workspace
    launched.clear()
    result, asked = await call("open_workspace", {"path": str(repo)})
    check(result["ok"] and asked == [] and launched == [["open", "-a", "Visual Studio Code", str(repo)]] and "On branch main" in result["message"],
          f"open_workspace: {result['message']}")
    plain = TMP / "not a repo"
    result, _ = await call("open_workspace", {"path": str(plain), "app": "Zed"})
    check(result["ok"] and launched[-1][2] == "Zed" and step_tools(result) == ["list_files", "open_with"], "a non-repository opens without a git step, in a named editor")
    result, _ = await call("open_workspace", {"path": str(TMP / "gone")})
    check(result["ok"] is False and "finding the folder" in result["error"], "a missing folder is reported")
    result, _ = await call("open_workspace", {"path": str(plain), "app": "Terminal"})
    check(result["ok"] is False and "never" in result["error"], "open_workspace cannot hand a folder to Terminal")

    # github_open
    cases = [
        ({"repo": "torvalds/linux"}, "https://github.com/torvalds/linux"),
        ({"repo": "torvalds/linux", "section": "issues"}, "https://github.com/torvalds/linux/issues"),
        ({"repo": "torvalds/linux", "section": "pull requests"}, "https://github.com/torvalds/linux/pulls"),
        ({"repo": "https://github.com/acme/widgets.git"}, "https://github.com/acme/widgets"),
        ({"repo": "acme/widgets", "query": "retry & backoff"}, "https://github.com/acme/widgets/search?q=retry+%26+backoff"),
        ({"repo": "octocat"}, "https://github.com/octocat"),
        ({"query": "voice assistant c++"}, "https://github.com/search?type=repositories&q=voice+assistant+c%2B%2B"),
        ({}, "https://github.com"),
    ]
    for args, expected in cases:
        opened_urls.clear()
        result, asked = await call("github_open", args)
        check(result["ok"] and opened_urls == [expected] and asked == [], f"github_open {args} -> {expected}")
    for remote, expected in [("git@github.com:acme/widgets.git", "https://github.com/acme/widgets/actions"),
                             ("https://x:tok@github.com/acme/w.x.git", "https://github.com/acme/w.x/actions")]:
        git(repo, "remote", "set-url", "origin", remote)
        opened_urls.clear()
        result, _ = await call("github_open", {"path": str(repo), "section": "actions"})
        check(opened_urls == [expected], f"github_open from a local repo with remote {remote.split('@')[-1]}")
    git(repo, "remote", "set-url", "origin", str(bare))
    opened_urls.clear()
    result, _ = await call("github_open", {"path": str(repo)})
    check(result["ok"] is False and "not on GitHub" in result["error"] and opened_urls == [], "a non-GitHub remote opens nothing")
    for bad in [{"repo": "a/b/c"}, {"repo": "evil.com/x?y"}, {"repo": "a b/c"}, {"repo": "x", "section": "nonsense"}]:
        opened_urls.clear()
        result, _ = await call("github_open", bad)
        check(result["ok"] is False and opened_urls == [], f"github_open rejects {bad}")


async def check_web_skills():
    section("web_search / research_topic / define_term")
    cases = [
        ({"query": "best pizza & pasta near me?"}, "https://www.google.com/search?q=best+pizza+%26+pasta+near+me%3F"),
        ({"query": "c++ move semantics", "engine": "duckduckgo"}, "https://duckduckgo.com/?q=c%2B%2B+move+semantics"),
        ({"query": "lo-fi", "engine": "YouTube"}, "https://www.youtube.com/results?search_query=lo-fi"),
        ({"query": "coffee", "engine": "maps"}, "https://www.google.com/maps/search/?api=1&query=coffee"),
        ({"query": "a#b&c=d e/f", "engine": "stack overflow"}, "https://stackoverflow.com/search?q=a%23b%26c%3Dd+e%2Ff"),
        ({"query": "日本語"}, "https://www.google.com/search?q=%E6%97%A5%E6%9C%AC%E8%AA%9E"),
    ]
    for args, expected in cases:
        opened_urls.clear()
        result, asked = await call("web_search", args)
        check(result["ok"] and opened_urls == [expected] and asked == [], f"web_search {args} -> {expected}")
    opened_urls.clear()
    Clipboard.text = "  copied   phrase\nsecond line "
    result, _ = await call("web_search", {})
    check(opened_urls == ["https://www.google.com/search?q=copied+phrase+second+line"] and "from the clipboard" in result["message"],
          "no query: searches for the clipboard text")
    Clipboard.text = ""
    opened_urls.clear()
    result, _ = await call("web_search", {})
    check(result["ok"] is False and opened_urls == [], "no query and an empty clipboard opens nothing")
    result, _ = await call("web_search", {"query": "x", "engine": "askjeeves"})
    check(result["ok"] is False and opened_urls == [], "an unknown engine opens nothing")
    await call("web_search", {"query": "q" * 1000})
    check(len(opened_urls[-1]) < 400, "an enormous query is capped")

    opened_urls.clear()
    result, asked = await call("research_topic", {"topic": "solid state batteries"})
    check(result["ok"] and asked == [] and opened_urls == [
        "https://www.google.com/search?q=solid+state+batteries",
        "https://en.wikipedia.org/wiki/Special:Search?search=solid+state+batteries",
        "https://news.google.com/search?q=solid+state+batteries",
    ], f"research_topic opens its three defaults: {result.get('message')}")
    check("Google, Wikipedia and Google News" in result["message"] and len(result["opened"]) == 3, "and says what it opened")
    opened_urls.clear()
    result, _ = await call("research_topic", {"topic": "x", "sources": ["web", "web", "scholar", "videos", "discussion", "news", "wikipedia", "bogus"]})
    check(len(opened_urls) == 4 and len(set(opened_urls)) == 4, f"research_topic never opens more than four tabs ({len(opened_urls)})")
    check("Skipped" in result["message"] and "bogus" in result["message"], f"and names what it skipped: {result['message']}")
    opened_urls.clear()
    result, _ = await call("research_topic", {"topic": "x", "sources": ["bogus"]})
    check(result["ok"] is False and opened_urls == [], "only unknown sources: nothing opened")
    result, _ = await call("research_topic", {"topic": "  "})
    check(result["ok"] is False and opened_urls == [], "an empty topic opens nothing")

    for args, expected in [
        ({"term": "serendipity"}, "https://www.merriam-webster.com/dictionary/serendipity"),
        ({"term": "ad hoc"}, "https://www.merriam-webster.com/dictionary/ad%20hoc"),
        ({"term": "a/b?c", "source": "dictionary"}, "https://www.merriam-webster.com/dictionary/a%2Fb%3Fc"),
        ({"term": "happy", "source": "thesaurus"}, "https://www.merriam-webster.com/thesaurus/happy"),
        ({"term": "naïve café", "source": "wiktionary"}, "https://en.wiktionary.org/wiki/na%C3%AFve_caf%C3%A9"),
        ({"term": "entropy", "source": "wikipedia"}, "https://en.wikipedia.org/wiki/Special:Search?search=entropy"),
    ]:
        opened_urls.clear()
        result, _ = await call("define_term", args)
        check(result["ok"] and opened_urls == [expected], f"define_term {args} -> {expected}")
    opened_urls.clear()
    result, _ = await call("define_term", {"term": ""})
    check(result["ok"] is False and opened_urls == [], "define_term with no term opens nothing")

    # The preference gate reaches a skill's steps: disable open_url and the skill stops.
    preferences.set_preference("open_url", enabled=False)
    opened_urls.clear()
    result, _ = await call("web_search", {"query": "x"})
    check(result["ok"] is False and "disabled" in result["error"] and opened_urls == [], f"with open_url disabled, web_search stops: {result['error']}")
    preferences.clear("open_url")


async def check_clipboard_and_pages(base):
    section("clipboard_to_note / clipboard_to_file / read_later / save_page_notes / code_scratchpad")
    notes = TMP / "notes" / "clip.txt"
    Clipboard.text = "a single copied line"
    result, asked = await call("clipboard_to_note", {"notes_path": str(notes), "label": "quote"})
    check(result["ok"] and asked == [] and step_tools(result) == ["get_clipboard", "append_text"], f"clipboard_to_note: {result.get('message')}")
    first = notes.read_text()
    check(first.endswith("] quote a single copied line\n") and first.startswith("[20"), f"one line, timestamped and labelled: {first!r}")
    Clipboard.text = "line one\nline two\n"
    await call("clipboard_to_note", {"notes_path": str(notes)})
    check(notes.read_text().startswith(first) and notes.read_text().endswith("]\nline one\nline two\n\n"), "a second save appends; multi-line text goes under its header")
    Clipboard.text = "   \n"
    result, _ = await call("clipboard_to_note", {"notes_path": str(notes)})
    check(result["ok"] is False and "empty" in result["error"] and notes.read_text().count("[20") == 2, "an empty clipboard saves nothing")
    Clipboard.text = "x" * 60000
    result, _ = await call("clipboard_to_note", {})
    check(result["ok"] and result["truncated"] and (HOME / "Notes" / "clipboard.txt").stat().st_size < 51000, "default path is ~/Notes/clipboard.txt; a huge clipboard is capped")

    Clipboard.text = "file body"
    target = TMP / "out" / "deep" / "quote.txt"
    result, asked = await call("clipboard_to_file", {"path": str(target)})
    check(result["ok"] and asked == [] and target.read_text() == "file body\n", f"clipboard_to_file writes a new file: {result.get('message')}")
    Clipboard.text = "different"
    result, _ = await call("clipboard_to_file", {"path": str(target)})
    check(result["ok"] is False and "already exists" in result["error"] and target.read_text() == "file body\n", "it never overwrites")
    result, _ = await call("clipboard_to_file", {})
    made = list((HOME / "Notes").glob("clipboard-*.txt"))
    check(result["ok"] and len(made) == 1 and made[0].read_text() == "different\n", "default is a timestamped file in ~/Notes")
    launched.clear()
    result, _ = await call("clipboard_to_file", {"path": "~/opened.txt", "open_in": "TextEdit"})
    check(result["ok"] and launched == [["open", "-a", "TextEdit", str(HOME / "opened.txt")]], "open_in opens the new file; ~ expanded")
    Clipboard.text = ""
    result, _ = await call("clipboard_to_file", {"path": str(TMP / "out" / "none.txt")})
    check(result["ok"] is False and not (TMP / "out" / "none.txt").exists(), "an empty clipboard makes no file")

    reading = TMP / "notes" / "later.md"
    result, asked = await call("read_later", {"url": "https://example.com/a?b=1", "title": "An  article", "list_path": str(reading)})
    check(result["ok"] and asked == [] and reading.read_text().endswith("] An article: https://example.com/a?b=1\n"), f"read_later appends a dated line: {reading.read_text()!r}")
    Clipboard.text = " example.org/path \n"
    result, _ = await call("read_later", {"list_path": str(reading)})
    lines = reading.read_text().splitlines()
    check(result["ok"] and len(lines) == 2 and lines[1].endswith("] https://example.org/path") and "from the clipboard" in result["message"],
          "no URL: takes it from the clipboard, adding https")
    for clip in ["not a url at all", "javascript:alert(1)", "file:///etc/passwd", "two words.com x", ""]:
        Clipboard.text = clip
        result, _ = await call("read_later", {"list_path": str(reading)})
        check(result["ok"] is False and len(reading.read_text().splitlines()) == 2, f"clipboard {clip!r} is not taken for a URL")
    result, _ = await call("read_later", {"url": "ftp://example.com/x", "list_path": str(reading)})
    check(result["ok"] is False, "a non-web URL argument is rejected")
    await call("read_later", {"url": "https://example.com"})
    check((HOME / "Notes" / "read-later.md").exists(), "default list is ~/Notes/read-later.md")

    pages = TMP / "pages"
    result, asked = await call("save_page_notes", {"url": f"{base}/redirect", "folder": str(pages), "note": "for the trip"})
    check(result["ok"] and asked == [] and step_tools(result) == ["fetch_url", "create_folder", "write_new_file"], f"save_page_notes: {result.get('message') or result.get('error')}")
    saved = list(pages.glob("*.md"))
    body = saved[0].read_text() if saved else ""
    check(len(saved) == 1 and saved[0].name.endswith("-test-page-co.md"), f"file named from date and title: {[p.name for p in saved]}")
    check(body.startswith("# Test Page & Co\n\nSource: " + f"{base}/page\n") and "Note: for the trip" in body and "First paragraph here." in body and "SCRIPT_TEXT" not in body,
          "the file has title, final URL, note and the page text")
    Clipboard.text = f"{base}/longpage"
    result, _ = await call("save_page_notes", {"folder": str(pages)})
    check(result["ok"] and result["truncated"] and "only the first part" in result["message"], "URL from the clipboard; a long page is saved truncated and says so")
    for route, needle in [("/png", "image/png"), ("/missing", "HTTP 404"), ("/empty", "no readable text")]:
        result, _ = await call("save_page_notes", {"url": f"{base}{route}", "folder": str(pages)})
        check(result["ok"] is False and needle in result["error"] and len(list(pages.glob("*.md"))) == 2, f"save_page_notes {route}: {result['error']}")
    result, _ = await call("save_page_notes", {"url": "http://127.0.0.1:9/x", "folder": str(pages)})
    check(result["ok"] is False and "public" in result["error"], "save_page_notes cannot reach another local port")

    scratch = TMP / "scratch"
    launched.clear()
    result, asked = await call("code_scratchpad", {"folder": str(scratch)})
    made = list(scratch.glob("scratch-*.py"))
    check(result["ok"] and asked == [] and len(made) == 1 and launched == [["open", "-a", "Visual Studio Code", str(made[0])]],
          f"code_scratchpad: a timestamped Python file, opened in the editor: {result.get('message')}")
    ran = subprocess.run([sys.executable, str(made[0])], capture_output=True, text=True, timeout=30)
    check(ran.returncode == 0 and ran.stdout.strip() == "scratch", "the Python starter runs")
    Clipboard.text = "SELECT 42;"
    result, _ = await call("code_scratchpad", {"folder": str(scratch), "language": "sql", "name": "query", "from_clipboard": True, "open_in": "Zed"})
    check(result["ok"] and (scratch / "query.sql").read_text() == "SELECT 42;\n" and launched[-1][2] == "Zed", "from_clipboard fills a named file")
    result, _ = await call("code_scratchpad", {"folder": str(scratch), "language": "sql", "name": "query"})
    check(result["ok"] is False and "already exists" in result["error"] and (scratch / "query.sql").read_text() == "SELECT 42;\n", "an existing scratch file is never replaced")
    for name in ["run.command", "../up", "a/b", "x.app", "tool.sh"]:
        result, _ = await call("code_scratchpad", {"folder": str(scratch), "language": "python", "name": name})
        check(result["ok"] is False, f"code_scratchpad rejects the name {name!r}")
    for language in ["shell", "sh", "bash"]:
        result, _ = await call("code_scratchpad", {"folder": str(scratch), "language": language})
        check(result["ok"] is False and not list(scratch.glob("*.sh")), f"code_scratchpad offers no {language} file")
    made_any = next(scratch.glob("scratch-*.py"))
    check(not os.access(made_any, os.X_OK), "a scratch file is created without the executable bit")
    for language in LANGUAGES:
        launched.clear()
        result, _ = await call("code_scratchpad", {"folder": str(scratch), "language": language, "name": f"every-{language}"})
        check(result["ok"] and len(launched) == 1, f"a {language} scratch file is created and passes open_with")
    result, _ = await call("code_scratchpad", {"folder": str(scratch), "language": "cobol"})
    check(result["ok"] is False, "an unknown language is rejected")
    result, _ = await call("code_scratchpad", {"folder": str(scratch), "name": "term", "open_in": "Terminal"})
    check(result["ok"] is False and (scratch / "term.py").exists() and "could not open" in result["message"],
          "asked to open in Terminal: file kept, open refused, said so")
    node = shutil.which("node")
    if node:
        ran = subprocess.run([node, "-e", LANGUAGES["javascript"][1]], capture_output=True, text=True, timeout=30)
        check(ran.stdout.strip() == "scratch", "the JavaScript starter runs")
    check(json.loads(LANGUAGES["json"][1]) == {} and not parse_html(LANGUAGES["html"][1]).errors, "the JSON and HTML starters are valid")


async def check_results_shape_and_isolation():
    section("result shape and isolation")
    log = [json.loads(line) for line in logging_util.LOG_FILE.read_text().splitlines()]
    check(any(r["type"] == "confirmation_requested" for r in log), "confirmations were logged to the temp event log")
    for name in DEV_SKILLS:
        result, _ = await call(name, {"path": 5, "name": None, "url": [], "topic": {}, "term": 0, "repo": 1.5, "message": None, "action": None,
                                      "language": 3, "engine": [], "query": {}}, approve=False)
        check(isinstance(result, dict) and "ok" in result and isinstance(result.get("message"), str) and isinstance(result.get("steps"), list),
              f"{name}: wrong-typed arguments give a result with ok, message and steps, not an exception")
    for folder in ("Projects", "Notes", "Scratch"):
        check((HOME / folder).is_dir(), f"~/{folder} defaults resolved inside the temp home")
    check(str(REAL_HOME) != str(HOME) and not str(TMP).startswith(str(REAL_HOME / "Desktop")), "the real home was never the target")


def check_real_fetch(real_allowed):
    section("fetch_url, one real request")
    fetch_mod._address_allowed = real_allowed
    result = fetch_mod.fetch_url("https://example.com/")
    if result["ok"]:
        check(result["title"] == "Example Domain" and len(result["text"]) > 40 and "<" not in result["text"],
              f"example.com over TLS: title {result['title']!r}, {len(result['text'])} characters of text")
    elif any(s in result["error"] for s in ("could not look up", "could not reach", "timed out")):
        print(f"skip  offline, real request not made ({result['error']})")
    else:
        check(False, f"example.com failed for a reason other than being offline: {result['error']}")


async def main():
    print(f"temp dir: {TMP}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        await check_registry()
        real_allowed = await check_fetch(base)
        repo = await check_git()
        await check_open_with()
        await check_scaffolds()
        await check_git_skills(repo)
        await check_web_skills()
        await check_clipboard_and_pages(base)
        await check_results_shape_and_isolation()
        check_real_fetch(real_allowed)
    finally:
        STOP.set()
        server.shutdown()
    check(opened_apps == [] and notifications == [], "no app was opened and no notification shown by any dev skill")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\nall {passed} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
