"""Opt-in browser transport with a human-controlled viewport in the local app.

This is deliberately not the JavaScript renderer: requests originate in Chromium,
and a person can complete a site's verification in the live app view. No cookies
are copied from another browser, and no automation-detection flags are changed.
"""
from __future__ import annotations

import hashlib
import contextlib
import json
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .network import SafeHTTP, canonical_url, public_addresses
from .locking import file_lock


class BrowserSessionClosed(ValueError):
    """The current page cannot continue; discovery must keep its checkpoint."""


def browser_was_closed(exc):
    return 'Target page, context or browser has been closed' in str(exc) or 'Browser has been closed' in str(exc)


class BrowserHTTP:
    def __init__(self, settings, domains, on_wait=None, active=None, job_id=None):
        self.settings = settings
        self.domains = {d.lower() for d in domains}
        # The site's visible verification screen requires this public service.
        # It is a subresource exception, never an archive/crawl destination.
        self.verification_domains = {'challenges.cloudflare.com'}
        self.on_wait = on_wait or (lambda message: None)
        self.active = active or (lambda: True)
        self.job_id = job_id
        self.robots = {}
        self.context = self.playwright = self.lock = self.page = None
        self.limiter = SafeHTTP(settings, domains)

    def _start(self):
        if self.context and self.page and not self.page.is_closed():
            return
        if self.context:
            self._dispose_browser()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ValueError("Browser-assisted discovery needs the browser extra. Run setup with browser support; see README.") from exc
        # Pin every permitted hostname to a validated public address before the
        # browser starts, including subresources. Unlisted names cannot resolve.
        rules = []
        for domain in sorted(self.domains | self.verification_domains):
            addresses = public_addresses(domain)
            address = next((a for a in addresses if ':' not in a), addresses[0])
            rules.append(f"MAP {domain} {'[' + address + ']' if ':' in address else address}")
        rules.append("MAP * ~NOTFOUND")
        profile_id = hashlib.sha256('\n'.join(sorted(self.domains)).encode()).hexdigest()[:24]
        profile = self.settings.data / '.browser' / ('stable-' + profile_id)
        self.lock = file_lock(profile.parent / (profile_id + '.lock'))
        self.lock.__enter__()
        try:
            self.playwright = sync_playwright().start()
            options = dict(headless=True, chromium_sandbox=True,
                           service_workers='block', accept_downloads=False,
                           viewport={'width': 1280, 'height': 850}, device_scale_factor=1,
                           args=['--host-resolver-rules=' + ', '.join(rules), '--disable-quic',
                                 '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
            try:
                self.context = self.playwright.chromium.launch_persistent_context(str(profile), channel='chrome', **options)
            except Exception as exc:
                if 'is not found' not in str(exc) and "doesn't exist" not in str(exc):
                    raise
                self.context = self.playwright.chromium.launch_persistent_context(str(profile), **options)
            self.context.add_init_script("for (const k of ['RTCPeerConnection','webkitRTCPeerConnection','WebTransport']) { try { Object.defineProperty(globalThis,k,{value:undefined,writable:false,configurable:false}); } catch {} }")
            self.context.route_web_socket('**/*', lambda ws: ws.close())
            self.context.route('**/*', self._route)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.bring_to_front()
        except Exception:
            self.close()
            raise

    def _route(self, route):
        request = route.request
        try:
            url = canonical_url(request.url)
            parsed = urlsplit(url)
            verification = parsed.hostname in self.verification_domains
            if parsed.hostname not in self.domains and not verification:
                raise ValueError('Browser destination is not on the allowlist')
            # Only the site's own verification endpoints may make a POST.
            if request.method != 'GET' and not (request.method == 'POST' and (verification or parsed.path.startswith('/cdn-cgi/'))):
                raise ValueError('Only read-only browsing is allowed')
            origin = f'{parsed.scheme}://{parsed.netloc}'
            robot = self.robots.get(origin)
            if request.is_navigation_request():
                if verification:
                    if self.page and request.frame == self.page.main_frame:
                        raise ValueError('Verification service is not a crawl destination')
                elif not robot and parsed.path != '/robots.txt':
                    raise ValueError('Redirect destination robots policy has not been checked')
                elif robot and not robot.can_fetch('Coverly', url):
                    raise ValueError('Access disallowed by robots.txt')
            route.continue_()
        except Exception:
            route.abort()

    @staticmethod
    def _challenge(page):
        try:
            state = page.evaluate('() => ({title: document.title, text: document.body?.innerText?.slice(0,5000) || ""})')
        except Exception as exc:
            # A human approval commonly replaces the document between polls.
            # Its old JavaScript context disappearing is successful navigation,
            # not a rejected request. Poll the replacement document next.
            if 'Execution context was destroyed' in str(exc) or 'Cannot find context with specified id' in str(exc):
                return True
            raise
        title, text = state['title'].lower(), state['text'].lower()
        return any(s in title for s in ('just a moment', 'attention required', 'verify you are human')) or any(
            s in text for s in ('performing security verification', 'verify you are human', 'enable javascript and cookies to continue'))

    def _verification_handoff(self):
        """Only relay explicit human input submitted through the local app."""
        if not self.job_id:
            return
        from .verification import directory, publish
        from .storage import atomic_write
        root = directory(self.settings, self.job_id)
        try:
            phase = self.page.evaluate('() => (document.body?.innerText || "").toLowerCase()')
        except Exception as exc:
            if 'Execution context was destroyed' in str(exc) or 'Cannot find context with specified id' in str(exc):
                return
            raise
        if getattr(self, '_human_clicked', False) and 'verifying you are human' in phase:
            self._saw_verifying = True
        if getattr(self, '_saw_verifying', False) and 'performing security verification' in phase:
            raise ValueError('Website verification was not accepted: the checkbox returned after your click. Coverly has not been granted access. Use normal-browser artwork capture or arrange permitted access with the website.')
        commands = root / 'commands'
        commands.mkdir(parents=True, exist_ok=True)
        for path in sorted(commands.glob('*.json'))[:10]:
            try:
                command = json.loads(path.read_text('utf-8'))
                if time.time() - command.get('created', 0) > 5:
                    continue
                if command['action'] == 'click' and 0 <= command['x'] < 1280 and 0 <= command['y'] < 850:
                    self._human_clicked = True
                    self.page.mouse.click(command['x'], command['y'])
                elif command['action'] == 'key' and command.get('key') in {'Tab', 'Space', 'Enter', 'Escape'}:
                    if command['key'] in {'Space', 'Enter'}:
                        self._human_clicked = True
                    self.page.keyboard.press(command['key'])
                elif command['action'] == 'focus':
                    self.page.bring_to_front()
            finally:
                path.unlink(missing_ok=True)
        atomic_write(root / 'screen.png', self.page.screenshot(type='png', timeout=5000))
        publish(self.settings, self.job_id, {'waiting': True, 'width': 1280, 'height': 850})

    def _navigate(self, url, *, robots=False, image=False):
        self._waiting = False
        for attempt in range(2):
            try:
                return self._navigate_once(url, robots=robots, image=image)
            except Exception as exc:
                if not browser_was_closed(exc):
                    raise
                self._dispose_browser()
                if not self.active():
                    raise BrowserSessionClosed('Browser request stopped. Resume this job to continue from its saved page.') from exc
                # Do not repeatedly restart a website's verification screen.
                if self._waiting or attempt:
                    raise BrowserSessionClosed('The website browser closed before this page finished. Discovery is paused at its saved page. Resume to open a new session; any website verification still needs your approval.') from exc
                self.on_wait('Reopening the closed website browser; retrying the current page once.')

    def _navigate_once(self, url, *, robots=False, image=False):
        self._start()
        self._human_clicked = self._saw_verifying = False
        parsed = urlsplit(url)
        if parsed.hostname not in self.domains:
            raise ValueError('Destination domain is not on this job\'s allowlist')
        self.limiter.throttle(parsed.hostname)
        responses = []
        def received(response):
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                responses.append(response)
        self.page.on('response', received)
        try:
            self.page.goto(url, wait_until='domcontentloaded', timeout=self.settings.request_timeout * 1000)
            deadline = time.monotonic() + 600
            notified = False
            while self._challenge(self.page) or (responses and responses[-1].headers.get('cf-mitigated') == 'challenge'):
                if not notified:
                    self._waiting = True
                    self.on_wait('Browser verification required. Click the verification checkbox in Coverly’s live browser view; discovery continues after the website accepts it.')
                    notified = True
                if not self.active():
                    raise ValueError('Browser request interrupted by pause or cancellation')
                if time.monotonic() >= deadline:
                    raise ValueError('Browser verification is still pending after 10 minutes. The website has not granted access; no catalogue pages were read.')
                self._verification_handoff()
                self.page.wait_for_timeout(500)
            if self.job_id:
                from .verification import publish
                publish(self.settings, self.job_id, {'waiting': False})
            response = responses[-1] if responses else None
            if response is None:
                raise ValueError('Browser returned no page response')
            if response.status == 404 and robots:
                return b'', url
            if response.status >= 400:
                raise ValueError(f'Browser access rejected ({response.status}): {self.page.title()[:160]} · {self.page.locator("body").inner_text()[:300]}')
            final = canonical_url(self.page.url)
            if urlsplit(final).hostname not in self.domains:
                raise ValueError('Browser redirected outside the allowlist')
            mime = response.headers.get('content-type', '').split(';')[0].lower()
            if robots and mime not in {'text/plain', 'text/html'}:
                raise ValueError('Unexpected robots.txt response type')
            if image and mime not in {'image/jpeg', 'image/png', 'image/webp', 'image/tiff', 'image/bmp', 'application/octet-stream'}:
                raise ValueError('Browser did not return a supported image')
            limit = (self.settings.max_image_mb if image else 5) * 1024 * 1024
            if int(response.headers.get('content-length', '0')) > limit:
                raise ValueError('Browser response exceeds configured size limit')
            raw = response.body() if robots or image else self.page.content().encode('utf-8')
            if len(raw) > limit:
                raise ValueError('Browser response exceeds configured size limit')
            if robots and b'user-agent:' not in raw.lower() and raw.strip():
                raise ValueError('robots.txt did not contain readable rules; browser crawl stopped')
            return raw, final
        finally:
            with contextlib.suppress(Exception):
                self.page.remove_listener('response', received)

    def permitted(self, url):
        parsed = urlsplit(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if origin not in self.robots:
            raw, _ = self._navigate(origin + '/robots.txt', robots=True)
            robot = RobotFileParser(origin + '/robots.txt')
            robot.parse(raw.decode('utf-8', errors='replace').splitlines())
            self.robots[origin] = robot
        robot = self.robots[origin]
        if not robot.can_fetch('Coverly', url):
            raise ValueError('Access disallowed by robots.txt')
        delay = robot.crawl_delay('Coverly') or robot.crawl_delay('*')
        if delay:
            if delay > 60:
                raise ValueError('Site requests a crawl delay over 60 seconds')
            time.sleep(delay)

    def get(self, url, image=False):
        url = canonical_url(url)
        if urlsplit(url).hostname not in self.domains:
            raise ValueError('Destination domain is not on this job\'s allowlist')
        self.permitted(url)
        return self._navigate(url, image=image)

    def _dispose_browser(self):
        if self.job_id:
            from .verification import publish
            publish(self.settings, self.job_id, {'waiting': False})
        if self.context:
            with contextlib.suppress(Exception):
                self.context.close()
        self.context = self.page = None
        if self.playwright:
            with contextlib.suppress(Exception):
                self.playwright.stop()
            self.playwright = None
        if self.lock:
            try:
                self.lock.__exit__(None, None, None)
            finally:
                self.lock = None

    def close(self):
        try:
            self._dispose_browser()
        finally:
            self.limiter.close()
