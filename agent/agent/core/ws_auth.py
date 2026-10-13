"""Who may open the agent's WebSocket (security review C1, BUGS.md).

The socket is the whole control surface: whoever holds a connection can send
commands, answer confirmations and rewrite preferences. Binding to loopback
is not access control -- every local process, and JavaScript on any web page
the user has open, can reach 127.0.0.1. So two rules are enforced during the
HTTP opening handshake, before the upgrade, so that a rejected client never
gets a connection object, never becomes the primary and never sees a frame:

1. Any request carrying an `Origin` header is refused (403), token or not.
   Browsers always send Origin on a WebSocket handshake and page script
   cannot remove it; none of our clients (Node's `ws`, QWebSocket, Python's
   `websockets`) send one. No value is allowed -- not `null`, not `file://`.

2. The request must carry `Authorization: Bearer <token>` matching this
   process's token (401 otherwise). A header, not a query string: URLs end
   up in logs, and a browser can't set this header on a WebSocket at all.

Where the token comes from:

- `LEUTHERIA_TOKEN` in the environment -- how Electron hands a fresh
  per-launch token to the sidecar it spawns. It is removed from os.environ
  once read, so nothing the agent runs (run_command's shell, `open`, git)
  inherits it and no `env` output can carry it into the model or the log.
- otherwise (standalone: `python -m agent`) the agent generates its own and
  writes it to a 0600 file in a 0700 per-user directory, for same-user tools
  to read. "No token configured" never means "no authentication"; there is
  deliberately no switch that turns this off.

The token is never logged, printed or put in an argv.
"""

import atexit
import hmac
import http
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent.core.logging_util import log_event

TOKEN_ENV = "LEUTHERIA_TOKEN"
# Where a standalone agent's token file goes. Overridable (tests, a sandboxed
# home) but the directory must still be private to this user -- see
# _private_dir().
RUNTIME_DIR_ENV = "LEUTHERIA_RUNTIME_DIR"
DEFAULT_RUNTIME_DIR = Path.home() / "Library" / "Application Support" / "Leutheria"

# 32 hex chars = 128 bits. Electron and the generated token are 256; this is
# the floor for one supplied by hand, so a typo'd or placeholder value
# ("test", "1") stops the agent instead of becoming the lock on the door.
MIN_TOKEN_CHARS = 32

SOURCE_ENV = "env"
SOURCE_FILE = "file"

_BEARER = "bearer "


class AuthConfigError(RuntimeError):
    """The agent can't establish a token safely. Startup must fail: running
    without authentication is not a fallback."""


@dataclass(frozen=True)
class Auth:
    token: str = ""
    source: str = ""
    token_file: Optional[Path] = None

    def __post_init__(self):
        if len(self.token) < MIN_TOKEN_CHARS:
            raise AuthConfigError(f"token must be at least {MIN_TOKEN_CHARS} characters")

    def __repr__(self) -> str:  # a stray log/print of this must not leak it
        return f"Auth(source={self.source!r}, token_file={str(self.token_file) if self.token_file else None!r})"

    def describe(self) -> str:
        """For the startup line: which mode, never the secret."""
        if self.source == SOURCE_FILE:
            return f"token-file:{self.token_file}"
        return "token-env"


def token_file_path(port: int) -> Path:
    """One file per port, so a check's server on a side port never replaces
    the token of a standalone agent on 8765."""
    base = os.environ.get(RUNTIME_DIR_ENV)
    directory = Path(base).expanduser() if base else DEFAULT_RUNTIME_DIR
    return directory / f"agent-{int(port)}.token"


def _private_dir(directory: Path) -> None:
    """Create the directory 0700, or insist an existing one is ours and not
    writable by anyone else -- otherwise another user could swap the token
    file for one they know."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise AuthConfigError(f"{directory} is not a directory (a symlink is not accepted)")
    if info.st_uid != os.getuid():
        raise AuthConfigError(f"{directory} is not owned by this user")
    if info.st_mode & 0o022:
        raise AuthConfigError(f"{directory} is writable by other users; refusing to put the token there")


def _write_token_file(path: Path, token: str) -> None:
    _private_dir(path.parent)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    # O_EXCL|O_NOFOLLOW: never write through something that was put there
    # for us. The mode is set at creation, so it is never readable by others
    # even for an instant; rename makes the swap atomic for readers.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)  # the umask can only clear bits, but be explicit
        os.write(fd, token.encode("ascii") + b"\n")
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _remove_token_file(path: Path, token: str) -> None:
    """Best effort at exit. Only if it is still ours -- a newer agent on the
    same port has replaced it with its own. A file left behind by a killed
    agent is harmless: it names a token no running process accepts."""
    try:
        if path.read_text().strip() == token:
            path.unlink()
    except OSError:
        pass


def bearer(token: str) -> dict:
    """The header a client presents -- for the check scripts and any Python
    debugging client: websockets.connect(url, additional_headers=bearer(t))."""
    return {"Authorization": f"Bearer {token}"}


def read_token_file(port: int) -> str:
    """For same-user tools (check scripts, a debugging client)."""
    return token_file_path(port).read_text().strip()


def configure(port: int) -> Auth:
    """Decide this process's token. Called once, at startup, before the
    socket is bound."""
    supplied = os.environ.pop(TOKEN_ENV, None)
    if supplied:
        if len(supplied) < MIN_TOKEN_CHARS:
            raise AuthConfigError(
                f"{TOKEN_ENV} is set but shorter than {MIN_TOKEN_CHARS} characters; "
                "refusing to start with a guessable token (unset it to have the agent generate one)"
            )
        return Auth(supplied, SOURCE_ENV)

    token = secrets.token_hex(32)
    path = token_file_path(port)
    try:
        _write_token_file(path, token)
    except OSError as e:
        raise AuthConfigError(f"couldn't write the token file {path}: {e}") from e
    atexit.register(_remove_token_file, path, token)
    return Auth(token, SOURCE_FILE, path)


# --- the handshake ------------------------------------------------------------

# A page (or a broken client in a retry loop) can attempt thousands of
# handshakes a second; each rejection must not become a line in events.jsonl.
_LOG_INTERVAL_S = 10.0
_last_logged: dict = {}
_suppressed: dict = {}


def _log_rejection(reason: str, status: int) -> None:
    now = time.monotonic()
    if now - _last_logged.get(reason, -_LOG_INTERVAL_S) < _LOG_INTERVAL_S:
        _suppressed[reason] = _suppressed.get(reason, 0) + 1
        return
    _last_logged[reason] = now
    # The reason only. Never the Origin or Authorization values: both are
    # attacker-chosen text, and the second may be a near-miss of the token.
    log_event("ws_rejected", reason=reason, status=status, suppressed_since_last=_suppressed.pop(reason, 0))


def _presented_token(headers) -> Optional[str]:
    values = headers.get_all("Authorization")
    if len(values) != 1:
        return None
    value = values[0].strip()
    if value[: len(_BEARER)].lower() != _BEARER:
        return None
    return value[len(_BEARER):].strip()


def check_request(auth: Auth, headers) -> Optional[tuple]:
    """None to accept, or (status, reason, body). Separate from the
    websockets callback so it can be reasoned about (and tested) on its own."""
    if "Origin" in headers:
        return http.HTTPStatus.FORBIDDEN, "origin", "Browser origins are not accepted.\n"
    presented = _presented_token(headers)
    if presented is None:
        return http.HTTPStatus.UNAUTHORIZED, "no_token", "Authentication required.\n"
    # compare_digest on bytes: constant-time for equal lengths, and a str
    # with non-ASCII in it would raise rather than compare.
    if not hmac.compare_digest(presented.encode("utf-8"), auth.token.encode("utf-8")):
        return http.HTTPStatus.UNAUTHORIZED, "bad_token", "Authentication required.\n"
    return None


def make_process_request(auth: Auth):
    """The `process_request` hook for websockets.serve() (websockets 13+
    signature: (ServerConnection, Request) -> Response | None). Returning a
    response sends it and aborts the handshake; the handler never runs."""
    if not isinstance(auth, Auth):
        raise AuthConfigError("the server needs an Auth; there is no unauthenticated mode")

    def process_request(connection, request):
        verdict = check_request(auth, request.headers)
        if verdict is None:
            return None
        status, reason, body = verdict
        _log_rejection(reason, int(status))
        response = connection.respond(status, body)
        if status == http.HTTPStatus.UNAUTHORIZED:
            response.headers["WWW-Authenticate"] = 'Bearer realm="leutheria"'
        return response

    return process_request
