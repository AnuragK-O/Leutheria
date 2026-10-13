"""Fetch a public web page and return its readable text.

"Safe" because it only reads: one bounded GET, nothing written anywhere.
The bounds are the point, since the URL is whatever the model was asked for
(or was talked into asking for by a page it read earlier):

  scheme     http/https only -- urllib would otherwise happily open file:.
  host       public addresses only. Loopback, private, link-local and the
             like are refused on every hop, so a page can't redirect the
             agent into a service on this machine or the local network. The
             check is made on the addresses the name resolves to at connect
             time, and the socket is opened to one of those same addresses:
             there is no second lookup for a hostile DNS server to answer
             differently.
  redirects  followed by hand, MAX_REDIRECTS at most, each hop re-validated.
  time       TIMEOUT per lookup / connect / read and DEADLINE for the whole
             call. DEADLINE, plus at most one TIMEOUT of slack for an
             operation already under way, is the longest this can hold a
             request open (it runs on a worker thread -- dispatcher._run_tool
             -- so the read loop itself is not held up).
  size       at most MAX_BYTES read off the wire (and at most that many after
             decompression), at most MAX_CHARS of text returned.
  type       text only; anything else is refused before the body is read.
"""

import codecs
import concurrent.futures
import html.parser
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

MAX_REDIRECTS = 5
TIMEOUT = 6.0  # seconds, per connect / read
DEADLINE = 12.0  # seconds, whole call including redirects
MAX_BYTES = 1_000_000
DEFAULT_CHARS = 8000
MAX_CHARS = 20000
USER_AGENT = "Leutheria/1.0 (local voice assistant; text fetch)"

_TEXT_TYPES = {
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
    "application/javascript",
    "application/x-ndjson",
}
_HTML_TYPES = {"text/html", "application/xhtml+xml"}


class _FetchError(Exception):
    pass


def _address_allowed(address: str, port: int) -> bool:
    """Whether a resolved IP address may be connected to. The one policy
    hook: scripts/skills_check_dev.py replaces it to let its own server on
    127.0.0.1 through, and nothing in the agent does."""
    ip = ipaddress.ip_address(address.split("%")[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_global


# getaddrinfo has no timeout of its own, so it runs on a worker thread and
# the caller stops waiting at the deadline. A stuck lookup keeps its thread
# until the system resolver gives up; two is enough for redirects.
_resolver = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="fetch-url-dns")
_call = threading.local()  # .deadline for the fetch running on this thread


def _remaining() -> float:
    left = getattr(_call, "deadline", time.monotonic() + TIMEOUT) - time.monotonic()
    if left <= 0:
        raise _FetchError(f"timed out after {DEADLINE:.0f} seconds")
    return min(TIMEOUT, left)


def _resolve(host: str, port: int) -> list:
    future = _resolver.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    try:
        return future.result(_remaining())
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise _FetchError(f"timed out looking up {host}")
    except (OSError, UnicodeError) as e:
        raise _FetchError(f"could not look up {host}: {e}")


def _connect_checked(address, timeout=None, source_address=None):
    """Stand-in for socket.create_connection: resolve once, refuse unless
    every address is allowed, then connect to those addresses and no others."""
    host, port = address
    infos = _resolve(host, port)
    if not infos:
        raise _FetchError(f"could not look up {host}")
    for info in infos:
        if not _address_allowed(info[4][0], port):
            raise _FetchError(f"{host} is not a public address; only public web pages can be fetched")
    error = None
    for family, kind, proto, _, sockaddr in infos[:3]:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(_remaining())
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            error = e
            sock.close()
    raise error


class _CheckedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_checked


class _CheckedHTTPSConnection(http.client.HTTPSConnection):
    # TLS is still negotiated and verified against the host name: only the
    # TCP connect underneath is replaced.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_checked


class _HTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_CheckedHTTPConnection, req)


class _HTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_CheckedHTTPSConnection, req, context=_tls)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the 3xx surfaces as an HTTPError; _fetch follows it by hand


# No ProxyHandler: through a proxy the address checked above would be the
# proxy's, not the page's. No FileHandler or FTPHandler either.
_tls = ssl.create_default_context()
_opener = urllib.request.OpenerDirector()
for _handler in (
    _HTTP(),
    _HTTPS(context=_tls),
    _NoRedirect(),
    urllib.request.HTTPDefaultErrorHandler(),
    urllib.request.HTTPErrorProcessor(),
):
    _opener.add_handler(_handler)


def _check_url(url: str) -> urllib.parse.SplitResult:
    try:
        parts = urllib.parse.urlsplit(url)
        host, _ = parts.hostname, parts.port  # .port raises on a bad port
    except ValueError:
        raise _FetchError(f"not a valid URL: {url[:200]}")
    if parts.scheme.lower() not in ("http", "https"):
        shown = parts.scheme or "none"
        raise _FetchError(f"only http and https URLs can be fetched (got scheme: {shown})")
    if not host:
        raise _FetchError(f"the URL has no host: {url[:200]}")
    if parts.username or parts.password:
        raise _FetchError("URLs with an embedded username or password are not fetched")
    return parts


def _media_type(headers) -> tuple:
    raw = headers.get("Content-Type") or ""
    media = raw.split(";")[0].strip().lower()
    match = re.search(r"charset\s*=\s*\"?([\w.:-]+)", raw, re.I)
    return media, (match.group(1) if match else None)


def _is_text(media: str) -> bool:
    return (
        media.startswith("text/")
        or media in _TEXT_TYPES
        or media.endswith("+json")
        or media.endswith("+xml")
    )


def _read_body(response, deadline: float) -> tuple:
    """(bytes, truncated). Reads at most MAX_BYTES off the wire and inflates
    to at most MAX_BYTES, so neither a huge body nor a small compressed one
    that expands to a huge one can use more memory than that."""
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
    inflater = None
    if encoding in ("gzip", "x-gzip", "deflate"):
        inflater = zlib.decompressobj(47 if "gzip" in encoding else 15)
    elif encoding not in ("", "identity"):
        raise _FetchError(f"the page uses an unsupported encoding ({encoding})")

    out = bytearray()
    read = 0
    truncated = False
    while True:
        if time.monotonic() > deadline:
            raise _FetchError(f"timed out after {DEADLINE:.0f} seconds")
        if read >= MAX_BYTES:
            truncated = True
            break
        # read1: return what has arrived rather than blocking until 16 KB
        # has, so a slow drip is measured against the deadline between reads.
        chunk = response.read1(min(16384, MAX_BYTES - read))
        if not chunk:
            break
        read += len(chunk)
        if inflater is None:
            out += chunk
            continue
        try:
            out += inflater.decompress(chunk, MAX_BYTES - len(out))
        except zlib.error:
            raise _FetchError("the page's compressed content could not be decoded")
        if len(out) >= MAX_BYTES:
            truncated = True
            break
    return bytes(out), truncated


def _decode(body: bytes, charset) -> str:
    if not charset:
        match = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?([\w.:-]+)", body[:4096], re.I)
        charset = match.group(1).decode("ascii", "replace") if match else "utf-8"
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = "utf-8"
    return body.decode(charset, errors="replace")


# Never text: skipped outright.
_SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "object", "canvas"}
# Text, but page furniture rather than content: menus, sidebars, footers.
_CHROME = {"nav", "aside", "footer", "button", "select", "dialog"}
# Where a page says its content is.
_MAIN = {"main", "article"}
_BLOCK = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header",
    "footer", "nav", "aside", "main", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "pre", "hr", "dt", "dd", "form", "figure", "figcaption", "title",
}
# Below this many characters a candidate is too thin to trust over a fuller one.
_MIN_CONTENT = 200


class _TextExtractor(html.parser.HTMLParser):
    """Collects three candidates in one pass, widest to narrowest: all text
    (`loose`), text outside menus and footers (`body`), and text inside
    <main>/<article> (`main`). html_to_text picks the narrowest with real
    content, so a long menu doesn't use up the character budget, while a
    page that marks nothing up -- or never closes its <nav> -- still comes
    back whole."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.loose, self.body, self.main = [], [], []
        self.title_parts = []
        self._skip = self._chrome = self._main = 0
        self._in_title = self._title_done = False

    def _emit(self, text):
        self.loose.append(text)
        if not self._chrome:
            self.body.append(text)
            if self._main:
                self.main.append(text)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag in _CHROME:
            self._chrome += 1
        elif tag in _MAIN:
            self._main += 1
        elif tag == "title" and not self._skip and not self._title_done:
            # The document's own title only: an inline <svg> has <title>
            # elements too, and they are not the page's name.
            self._in_title = True
        if tag in _BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in _BLOCK:
            self._emit("\n")
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _CHROME:
            self._chrome = max(0, self._chrome - 1)
        elif tag in _MAIN:
            self._main = max(0, self._main - 1)
        elif tag == "title" and self._in_title:
            self._in_title, self._title_done = False, True

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip:
            self._emit(data)


def _tidy(parts: list) -> str:
    lines = [re.sub(r"[ \t\r\f\v\u00a0]+", " ", line).strip() for line in "".join(parts).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(markup: str) -> tuple:
    """(title, text) with tags, scripts and styles removed. Never raises:
    whatever the parser managed before giving up is what's returned."""
    extractor = _TextExtractor()
    try:
        extractor.feed(markup)
        extractor.close()
    except Exception:
        pass
    title = re.sub(r"\s+", " ", "".join(extractor.title_parts)).strip()
    loose, body, main = _tidy(extractor.loose), _tidy(extractor.body), _tidy(extractor.main)
    if len(main) >= _MIN_CONTENT:
        return title, main
    if len(body) >= _MIN_CONTENT or len(body) == len(loose):
        return title, body
    return title, loose


def _fetch(url: str, deadline: float) -> dict:
    redirects = 0
    while True:
        parts = _check_url(url)
        remaining = _remaining()
        request = urllib.request.Request(
            urllib.parse.urlunsplit(parts._replace(fragment="")),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,text/plain;q=0.9,application/json;q=0.8,*/*;q=0.1",
                "Accept-Encoding": "gzip, identity",
            },
        )
        try:
            response = _opener.open(request, timeout=remaining)
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location") if e.headers else None
            status = e.code
            e.close()
            if status in (301, 302, 303, 307, 308) and location:
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise _FetchError(f"too many redirects (more than {MAX_REDIRECTS})")
                url = urllib.parse.urljoin(url, location)
                continue
            raise _FetchError(f"the server answered with HTTP {status}")

        with response:
            media, charset = _media_type(response.headers)
            if media and not _is_text(media):
                raise _FetchError(f"not a text page (content type {media}); only text can be fetched")
            body, truncated = _read_body(response, deadline)
            status = response.status
        if not media and b"\x00" in body[:2048]:
            raise _FetchError("not a text page (binary content with no content type)")
        return {
            "url": url,
            "status": status,
            "content_type": media or "unknown",
            "charset": charset,
            "body": body,
            "truncated": truncated,
            "redirects": redirects,
        }


def fetch_url(url: str, max_chars: int = DEFAULT_CHARS) -> dict:
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "error": "no URL given"}
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        max_chars = DEFAULT_CHARS
    max_chars = max(200, min(max_chars, MAX_CHARS))

    deadline = _call.deadline = time.monotonic() + DEADLINE
    try:
        fetched = _fetch(url.strip(), deadline)
    except _FetchError as e:
        return {"ok": False, "error": str(e)}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "error": "timed out waiting for the server"}
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return {"ok": False, "error": "timed out waiting for the server"}
        return {"ok": False, "error": f"could not reach the server: {reason}"}
    except (OSError, ValueError) as e:
        # http.client's own errors (bad status line, incomplete read) are
        # OSError or ValueError subclasses.
        return {"ok": False, "error": f"could not fetch the page: {e or type(e).__name__}"}
    except Exception as e:  # e.g. http.client.HTTPException
        return {"ok": False, "error": f"could not fetch the page: {type(e).__name__}"}

    text = _decode(fetched["body"], fetched["charset"])
    title = ""
    if fetched["content_type"] in _HTML_TYPES or (
        fetched["content_type"] == "unknown" and re.search(r"<html|<!doctype html", text[:2048], re.I)
    ):
        title, text = html_to_text(text)
    else:
        text = text.strip()

    truncated = fetched["truncated"] or len(text) > max_chars
    return {
        "ok": True,
        "url": fetched["url"],
        "status": fetched["status"],
        "content_type": fetched["content_type"],
        "title": title[:300],
        "text": text[:max_chars],
        "truncated": truncated,
        "redirects": fetched["redirects"],
    }
