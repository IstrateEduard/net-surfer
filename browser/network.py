"""URLs and networking.

The browser owns URL parsing/resolution, redirects, cookies, caching,
content decoding and error handling. The raw socket/TLS/HTTP-framing work
is delegated to Python's standard http.client + ssl modules.
"""
import base64
import gzip
import http.client
import os
import socket
import ssl
import threading
import time
import urllib.parse
import zlib

USER_AGENT = "NetSurfer/1.0 (educational; Python/Tk)"
# With the JavaScript engine available we say we're Chrome-compatible, as every
# real browser does: sites like google.com otherwise send a decades-old page
# meant for unknown browsers (see engine.load).
BROWSER_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/141.0.0.0 Safari/537.36")
MAX_REDIRECTS = 10
TIMEOUT = 15


class URL:
    """A parsed absolute URL. Supports http, https, file, data, about."""

    def __init__(self, url):
        url = url.strip()
        self.raw = url
        if url.startswith("data:"):
            self.scheme = "data"
            self.host = ""
            self.port = None
            self.path = url[5:]
            self.query = ""
            self.fragment = ""
            return
        if url.startswith("about:"):
            self.scheme = "about"
            self.host = ""
            self.port = None
            self.path = url[6:]
            self.query = ""
            self.fragment = ""
            return
        if "://" not in url:
            raise ValueError("not an absolute URL: %r" % url)
        scheme, rest = url.split("://", 1)
        self.scheme = scheme.lower()
        if self.scheme not in ("http", "https", "file"):
            raise ValueError("unsupported scheme: %s" % self.scheme)
        rest, _, self.fragment = rest.partition("#")
        if "/" in rest:
            hostport, path = rest.split("/", 1)
            path = "/" + path
        else:
            hostport, path = rest, "/"
        path, sep, query = path.partition("?")
        self.query = query if sep else ""
        self.has_query = bool(sep)
        if "@" in hostport:
            hostport = hostport.rsplit("@", 1)[1]
        self.port = {"http": 80, "https": 443, "file": None}[self.scheme]
        if hostport.startswith("["):  # IPv6 literal
            end = hostport.find("]")
            self.host = hostport[1:end]
            if hostport[end + 1:].startswith(":") and hostport[end + 2:]:
                self.port = int(hostport[end + 2:])
        elif ":" in hostport:
            h, p = hostport.rsplit(":", 1)
            self.host = h.lower()
            if p:
                self.port = int(p)
        else:
            self.host = hostport.lower()
        self.path = path

    @property
    def origin(self):
        return "%s://%s" % (self.scheme, self.host)

    def request_target(self):
        target = self.path or "/"
        if self.query or getattr(self, "has_query", False):
            target += "?" + self.query
        # Percent-encode characters that can't go on the request line.
        return urllib.parse.quote(target, safe="/?&=%:@!$'()*+,;~#[]-._")

    def without_fragment(self):
        return str(self).split("#", 1)[0]

    def resolve(self, link):
        """Resolve a (possibly relative) link against this URL."""
        link = link.strip().replace("\n", "").replace("\t", "").replace("\r", "")
        if not link:
            return URL(self.without_fragment())
        lower = link.lower()
        if "://" in link.split("?", 1)[0].split("#", 1)[0] or lower.startswith(("data:", "about:")):
            return URL(link)
        if lower.startswith(("javascript:", "mailto:", "tel:")):
            raise ValueError("unsupported link: %s" % link)
        if self.scheme in ("data", "about"):
            raise ValueError("cannot resolve relative to %s" % self.scheme)
        if link.startswith("//"):
            return URL(self.scheme + ":" + link)
        if link.startswith("#"):
            return URL(self.without_fragment() + link)
        if link.startswith("?"):
            return URL("%s%s%s" % (self.base(), self.path, link))
        if link.startswith("/"):
            path_and_rest = link
        else:
            directory = self.path.rsplit("/", 1)[0]
            path_and_rest = directory + "/" + link
        # Normalise ./ and ../ in the path part only.
        path, sep, rest = _split_path(path_and_rest)
        return URL(self.base() + normalize_path(path) + sep + rest)

    def base(self):
        default = {"http": 80, "https": 443}.get(self.scheme)
        host = self.host if ":" not in self.host else "[%s]" % self.host
        if self.port and self.port != default:
            return "%s://%s:%d" % (self.scheme, host, self.port)
        return "%s://%s" % (self.scheme, host)

    def __str__(self):
        if self.scheme == "data":
            return "data:" + self.path
        if self.scheme == "about":
            return "about:" + self.path
        s = self.base() + self.path
        if self.query or getattr(self, "has_query", False):
            s += "?" + self.query
        if self.fragment:
            s += "#" + self.fragment
        return s

    def __repr__(self):
        return "URL(%r)" % str(self)


def _split_path(s):
    for i, c in enumerate(s):
        if c in "?#":
            return s[:i], c, s[i + 1:]
    return s, "", ""


def normalize_path(path):
    parts = path.split("/")
    out = []
    for i, part in enumerate(parts):
        if part == ".":
            if i == len(parts) - 1:
                out.append("")
            continue
        if part == "..":
            if len(out) > 1:
                out.pop()
            if i == len(parts) - 1:
                out.append("")
            continue
        out.append(part)
    result = "/".join(out)
    return result if result.startswith("/") else "/" + result


class Response:
    def __init__(self, url, status, headers, body, reason=""):
        self.url = url              # final URL after redirects
        self.status = status
        self.reason = reason
        self.headers = headers      # dict, lower-case keys
        self.body = body            # bytes

    @property
    def content_type(self):
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def charset(self):
        ct = self.headers.get("content-type", "")
        for part in ct.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.strip().lower() == "charset":
                return v.strip().strip('"\'')
        return None

    def ok(self):
        return 200 <= self.status < 300

    def text(self, fallback_charset=None):
        return decode_text(self.body, self.charset or fallback_charset)


def decode_text(body, charset=None):
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", errors="replace")
    if charset is None:
        # sniff <meta charset> in the first 2 KB
        head = body[:2048].lower()
        idx = head.find(b"charset=")
        if idx != -1:
            rest = head[idx + 8:idx + 40].lstrip(b"\"' ")
            name = bytearray()
            for b in rest:
                if chr(b).isalnum() or chr(b) in "-_":
                    name.append(b)
                else:
                    break
            charset = name.decode("ascii", errors="ignore") or None
    for cs in (charset, "utf-8"):
        if not cs:
            continue
        try:
            return body.decode(cs, errors="replace")
        except LookupError:
            continue
    return body.decode("latin-1", errors="replace")


class NetworkError(Exception):
    pass


class CookieJar:
    """Minimal cookie store: name=value per host, with Domain and Path."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cookies = {}  # (domain, path, name) -> (value, host_only, expires)

    def store(self, url, header_value):
        parts = [p.strip() for p in header_value.split(";")]
        if not parts or "=" not in parts[0]:
            return
        name, _, value = parts[0].partition("=")
        domain = url.host
        host_only = True
        path = "/"
        expires = None
        for attr in parts[1:]:
            k, _, v = attr.partition("=")
            k = k.strip().lower()
            if k == "domain" and v:
                d = v.strip().lstrip(".").lower()
                if url.host == d or url.host.endswith("." + d):
                    domain = d
                    host_only = False
            elif k == "path" and v.startswith("/"):
                path = v
            elif k == "max-age":
                try:
                    expires = time.time() + int(v)
                except ValueError:
                    pass
        with self.lock:
            key = (domain, path, name.strip())
            if expires is not None and expires <= time.time():
                self.cookies.pop(key, None)
            else:
                self.cookies[key] = (value.strip(), host_only, expires)

    def header_for(self, url):
        now = time.time()
        pairs = []
        with self.lock:
            for (domain, path, name), (value, host_only, expires) in self.cookies.items():
                if expires is not None and expires < now:
                    continue
                if host_only and url.host != domain:
                    continue
                if not host_only and not (url.host == domain or url.host.endswith("." + domain)):
                    continue
                if not url.path.startswith(path):
                    continue
                pairs.append("%s=%s" % (name, value))
        return "; ".join(pairs)


COOKIES = CookieJar()


class Cache:
    """In-memory cache for subresources (stylesheets, images)."""

    def __init__(self, max_entries=500):
        self.lock = threading.Lock()
        self.entries = {}
        self.max_entries = max_entries

    def get(self, key):
        with self.lock:
            return self.entries.get(key)

    def put(self, key, response):
        with self.lock:
            if len(self.entries) >= self.max_entries:
                self.entries.pop(next(iter(self.entries)))
            self.entries[key] = response


CACHE = Cache()

_ssl_context = ssl.create_default_context()


def _decompress(body, encoding):
    encoding = (encoding or "").lower().strip()
    if encoding in ("gzip", "x-gzip"):
        return gzip.decompress(body)
    if encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    if encoding == "br":
        try:
            import brotli  # optional
            return brotli.decompress(body)
        except ImportError:
            raise NetworkError("server sent brotli-compressed data")
    return body


def fetch(url, method="GET", body=None, headers=None, use_cache=False, referrer=None, progress=None):
    """Fetch a URL, following redirects. Returns a Response or raises NetworkError.

    If given, progress(received, total) is called once the response headers
    arrive and again as body bytes come in; total is the Content-Length, or
    None when the server didn't send one (e.g. chunked responses)."""
    if isinstance(url, str):
        url = URL(url)
    if use_cache and method == "GET":
        cached = CACHE.get(str(url))
        if cached is not None:
            return cached

    if url.scheme == "data":
        return _fetch_data(url)
    if url.scheme == "about":
        return Response(url, 200, {"content-type": "text/html"}, b"<html><body></body></html>")
    if url.scheme == "file":
        return _fetch_file(url)

    original = url
    for _ in range(MAX_REDIRECTS + 1):
        resp = _http_request(url, method, body, headers, referrer, progress)
        if resp.status in (301, 302, 303, 307, 308) and "location" in resp.headers:
            try:
                url = url.resolve(resp.headers["location"])
            except ValueError as e:
                raise NetworkError("bad redirect: %s" % e)
            if resp.status == 303 or (resp.status in (301, 302) and method == "POST"):
                method, body = "GET", None
            continue
        if use_cache and method == "GET" and resp.ok():
            CACHE.put(str(original), resp)
        return resp
    raise NetworkError("too many redirects")


def _proxy_for(url):
    """Honour HTTPS_PROXY / NO_PROXY (tunnelled with CONNECT) if set."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return None
    no_proxy = (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",")
    for pattern in no_proxy:
        pattern = pattern.strip().lstrip("*").lstrip(".")
        if pattern and (url.host == pattern or url.host.endswith("." + pattern)):
            return None
    try:
        p = URL(proxy if "://" in proxy else "http://" + proxy)
    except ValueError:
        return None
    return p.host, p.port or 8080


def _read_body(r, progress):
    """Read the whole response body, reporting bytes received as we go."""
    if progress is None:
        return r.read()
    length = r.getheader("content-length")
    total = int(length) if length and length.isdigit() else None
    progress(0, total)
    chunks = []
    received = 0
    while True:
        chunk = r.read(16384)
        if not chunk:
            break
        chunks.append(chunk)
        received += len(chunk)
        progress(received, total)
    return b"".join(chunks)


def _http_request(url, method, body, extra_headers, referrer, progress=None):
    proxy = _proxy_for(url)
    host, port = (proxy if proxy else (url.host, url.port))
    if url.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=_ssl_context)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    if proxy:
        conn.set_tunnel(url.host, url.port)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/css,image/*;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.8",
        "Connection": "close",
    }
    cookie = COOKIES.header_for(url)
    if cookie:
        headers["Cookie"] = cookie
    if referrer:
        headers["Referer"] = referrer
    if extra_headers:
        headers.update(extra_headers)
    try:
        conn.request(method, url.request_target(), body=body, headers=headers)
        r = conn.getresponse()
        raw = _read_body(r, progress)
        header_dict = {}
        for k, v in r.getheaders():
            k = k.lower()
            if k == "set-cookie":
                COOKIES.store(url, v)
            header_dict[k] = v if k not in header_dict else header_dict[k] + ", " + v
        data = _decompress(raw, header_dict.get("content-encoding"))
        return Response(url, r.status, header_dict, data, r.reason)
    except (socket.timeout, TimeoutError):
        raise NetworkError("connection to %s timed out" % url.host)
    except socket.gaierror:
        raise NetworkError("could not resolve host %s" % url.host)
    except ssl.SSLError as e:
        raise NetworkError("TLS error with %s: %s" % (url.host, e.reason or e))
    except (OSError, http.client.HTTPException, zlib.error, EOFError) as e:
        raise NetworkError("%s: %s" % (type(e).__name__, e))
    finally:
        conn.close()


def _fetch_data(url):
    meta, _, data = url.path.partition(",")
    is_b64 = meta.endswith(";base64")
    if is_b64:
        meta = meta[:-7]
    try:
        payload = base64.b64decode(urllib.parse.unquote(data) + "===") if is_b64 \
            else urllib.parse.unquote_to_bytes(data)
    except ValueError as e:
        raise NetworkError("bad data URL: %s" % e)
    return Response(url, 200, {"content-type": meta or "text/plain"}, payload)


def _fetch_file(url):
    path = urllib.parse.unquote(url.path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    try:
        if os.path.isdir(path):
            entries = sorted(os.listdir(path))
            items = "".join('<li><a href="%s">%s</a></li>' % (urllib.parse.quote(e) + ("/" if os.path.isdir(os.path.join(path, e)) else ""), e)
                            for e in entries)
            html = "<html><body><h1>Index of %s</h1><ul>%s</ul></body></html>" % (path, items)
            return Response(url, 200, {"content-type": "text/html"}, html.encode())
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise NetworkError("cannot open %s: %s" % (path, e.strerror))
    ext = os.path.splitext(path)[1].lower()
    ctype = {".html": "text/html", ".htm": "text/html", ".css": "text/css",
             ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".txt": "text/plain", ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
    return Response(url, 200, {"content-type": ctype}, data)
