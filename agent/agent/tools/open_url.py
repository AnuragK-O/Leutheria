"""Open a web page in the default browser.

`open <anything>` is not a safe primitive. Given a plain path it opens the
file -- and for a .command, a unix executable or an app bundle "opens" means
*runs*, which would make this run_command with no confirmation. Given a URL
it hands it to whatever app claims the scheme: shortcuts:// runs a
shortcut, ssh:// and telnet:// start a terminal session, smb:// and afp://
mount a share, vscode:// and x-apple.systempreferences: drive other apps.
The caller is a model that may have just read a hostile web page, so this is
an allowlist with one entry:

  scheme   http or https, nothing else. No path, no file:, no custom scheme.
  host     must be present; no user:password@ in front of it (that form
           exists to make evil.example look like a login to somewhere else).
  text     no whitespace, control or invisible formatting characters, no
           backslash, no leading "-", and a length cap.
  argv     ["open", "--", url] with the URL rebuilt from its parsed parts,
           so what was checked is what is opened and nothing can be read as
           an option.

Local files go through open_file, which decides by what macOS says the file
is. Applications go through open_app.
"""

import unicodedata
import urllib.parse

from agent.core import bounded

TIMEOUT = 15.0
MAX_LENGTH = 4000
ALLOWED_SCHEMES = ("http", "https")

# Injection point: the check scripts record the argv instead of opening a
# browser window on the user's screen.
_launch = bounded.run

WEB_ONLY = (
    "open_url opens web pages only (http or https). "
    "Use open_file for a file or folder on this Mac and open_app for an application."
)


def checked_url(url) -> tuple:
    """(normalised URL, None) or (None, why it is refused)."""
    if not isinstance(url, str) or not url.strip(" "):
        return None, "no web address was given"
    url = url.strip(" ")
    if len(url) > MAX_LENGTH:
        return None, f"that address is longer than {MAX_LENGTH} characters"
    if url.startswith("-"):
        return None, "a web address cannot start with a dash. " + WEB_ONLY
    # Before parsing: urlsplit silently deletes tabs and newlines, so a
    # check made afterwards would be on a different string.
    for char in url:
        if char.isspace() or unicodedata.category(char) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            return None, "a web address cannot contain spaces, line breaks or control characters"
    if "\\" in url:
        return None, "a web address cannot contain a backslash"
    try:
        parts = urllib.parse.urlsplit(url)
        host, _ = parts.hostname, parts.port  # .port raises on a bad port
    except ValueError:
        return None, f"that is not a valid web address: {url[:120]}"
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        if not scheme or not parts.netloc and url.startswith(("/", "~", ".")):
            return None, (
                "that is not a full web address (it must start with https:// or http://). " + WEB_ONLY
            )
        return None, f"{scheme}: links are not opened. " + WEB_ONLY
    if not host:
        return None, f"that web address has no site name: {url[:120]}"
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        return None, "web addresses with a username or password in front of the site name are not opened"
    return urllib.parse.urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment)), None


def open_url(url: str) -> dict:
    target, error = checked_url(url)
    if error:
        return {"ok": False, "error": error}
    code, _, stderr = _launch(["open", "--", target], TIMEOUT)
    if code != 0:
        return {"ok": False, "error": stderr or f"could not open {target}"}
    return {"ok": True, "url": target, "message": f"opened {target}"}
