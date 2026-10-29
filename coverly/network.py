from __future__ import annotations

import ipaddress
import socket
import time
import sqlite3
import hashlib
from .locking import file_lock
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser
import httpx

USER_AGENT = "Coverly/1.0 (+local archival tool; respects robots.txt)"


def canonical_url(url):
    if not isinstance(url, str) or any(ord(c) < 32 for c in url) or "\\" in url:
        raise ValueError("Invalid URL")
    p = urlsplit(url.strip())
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Only public HTTP/HTTPS URLs without credentials are allowed")
    if p.port not in {None, 80, 443}:
        raise ValueError("Only standard web ports are allowed")
    host = p.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{p.port}" if p.port and p.port != (443 if p.scheme == "https" else 80) else "")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}))
    return urlunsplit((p.scheme, netloc, p.path or "/", query, ""))


def public_addresses(host, port=443):
    if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith((".localhost", ".local", ".internal")):
        raise ValueError("Private or internal destinations are blocked")
    addresses = sorted({record[4][0] for record in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    if not addresses:
        raise ValueError("Destination has no public address")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or getattr(ip, "ipv4_mapped", None) is not None:
            raise ValueError("Private or internal destinations are blocked")
    return addresses


class SafeHTTP:
    robots_cache = {}
    def __init__(self, settings, domains, client=None):
        self.settings = settings
        self.domains = {d.lower() for d in domains}
        self.client = client or httpx.Client(timeout=settings.request_timeout, follow_redirects=False, trust_env=False)
        self.injected_client = client is not None
        self.clients = {}
        self.robots = {}
        self.last = {}
        self.on_progress = None

    def close(self):
        self.client.close()
        for client in self.clients.values():
            client.close()

    def throttle(self, domain):
        # Reserve request slots transactionally across all worker processes.
        self.settings.data.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.settings.data / "coverly.sqlite3", timeout=30) as db:
            db.execute("CREATE TABLE IF NOT EXISTS domain_requests (domain TEXT PRIMARY KEY, next_at REAL NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT next_at FROM domain_requests WHERE domain=?", (domain,)).fetchone()
            slot = max(time.time(), row[0] if row else 0)
            db.execute("INSERT OR REPLACE INTO domain_requests VALUES (?, ?)", (domain, slot + self.settings.request_delay))
            db.commit()
        wait = slot - time.time()
        if wait > 0:
            time.sleep(wait)

    def _request(self, url, limit, check_robots=False, headers=None):
        for redirect in range(6):
            url = canonical_url(url)
            p = urlsplit(url)
            if p.hostname not in self.domains:
                raise ValueError("Destination domain is not on this job's allowlist: " + p.hostname)
            if check_robots:
                self.permitted(url)
            with file_lock(self.settings.data / ".locks" / (hashlib.sha256(p.hostname.encode()).hexdigest() + ".lock")):
                addresses = public_addresses(p.hostname, p.port or (443 if p.scheme == "https" else 80))
                self.throttle(p.hostname)
                # Connect to the validated IP; retain original Host and TLS SNI. This closes the DNS-rebinding gap.
                target = httpx.URL(url).copy_with(host=addresses[0])
                if self.injected_client:
                    client = self.client
                else:
                    client = self.clients.setdefault(p.hostname, None)
                    if client is None:
                        client = self.clients[p.hostname] = httpx.Client(timeout=self.settings.request_timeout, follow_redirects=False, trust_env=False)
                request = client.build_request("GET", target, headers={"Host": p.netloc, "User-Agent": USER_AGENT, **(headers or {})},
                                                    extensions={"sni_hostname": p.hostname})
                response = client.send(request, stream=True)
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        from urllib.parse import urljoin
                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    if int(response.headers.get("content-length", "0")) > limit:
                        raise ValueError("Response exceeds configured size limit")
                    chunks, count = [], 0
                    for chunk in response.iter_bytes():
                        count += len(chunk)
                        if count > limit:
                            raise ValueError("Response exceeds configured size limit")
                        chunks.append(chunk)
                        if self.on_progress:
                            self.on_progress(url, count, int(response.headers.get('content-length', '0')))
                    return b"".join(chunks), response.headers, url
                finally:
                    response.close()
        raise ValueError("Too many redirects")

    def permitted(self, url):
        p = urlsplit(canonical_url(url))
        origin = f"{p.scheme}://{p.netloc}"
        cache_key=(str(self.settings.data),origin)
        cached=self.robots_cache.get(cache_key) if not self.injected_client else None
        if cached and time.monotonic()-cached[0]<1800:
            self.robots[origin]=cached[1]
        if origin not in self.robots:
            robot = RobotFileParser(origin + "/robots.txt")
            try:
                data, _, _ = self._request(origin + "/robots.txt", 1024 * 1024)
                robot.parse(data.decode("utf-8", errors="replace").splitlines())
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    robot.parse([])
                else:
                    raise ValueError(f"robots.txt unavailable ({exc.response.status_code}); crawl stopped") from exc
            self.robots[origin] = robot
            if not self.injected_client:self.robots_cache[cache_key]=(time.monotonic(),robot)
        robot = self.robots[origin]
        if not robot.can_fetch("Coverly", url):
            raise ValueError("Access disallowed by robots.txt")
        delay = robot.crawl_delay("Coverly") or robot.crawl_delay("*")
        if delay:
            if delay > 60:
                raise ValueError("Site requests a crawl delay over 60 seconds; use manual import or arrange permitted access")
            time.sleep(delay)

    def get(self, url, image=False):
        last_error = None
        for attempt in range(3):
            try:
                # robots.txt is a network request too. A dropped connection
                # there must receive the same bounded retry as the artwork.
                self.permitted(url)
                data, headers, final = self._request(url, self.settings.max_image_mb * 1024 * 1024 if image else 5 * 1024 * 1024, check_robots=True)
                mime = headers.get("content-type", "").split(";")[0].lower()
                if image and mime not in {"image/jpeg", "image/png", "image/webp", "image/tiff", "image/bmp", "application/octet-stream"}:
                    raise ValueError("Server did not return a supported image MIME type")
                if not image and mime and mime not in {"text/html", "application/xhtml+xml", "text/plain", "application/javascript", "text/javascript", "application/json", "text/css"}:
                    raise ValueError("Unexpected response type")
                return data, final
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {429, 500, 502, 503, 504}:
                    raise
                if not self.injected_client and isinstance(exc, httpx.TransportError):
                    # Discard the pool containing a stale keep-alive socket.
                    client = self.clients.pop(urlsplit(url).hostname, None)
                    if client is not None:
                        client.close()
                if attempt < 2:
                    time.sleep(2 ** attempt)
        if isinstance(last_error, httpx.RemoteProtocolError):
            raise ValueError('The source website disconnected before completing its response after 3 attempts. Saved artwork is safe; retry failed items when the website is available.') from last_error
        raise last_error
