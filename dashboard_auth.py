"""Password login with bounded, expiring server-side sessions."""
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from twisted.internet.threads import deferToThread
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()
    return 'pbkdf2_sha256$600000$' + salt + '$' + digest


def verify(password, encoded):
    try:
        algorithm, rounds, salt, digest = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or rounds != '600000':
            return False
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


class Login(Resource):
    isLeaf = True

    def __init__(self, dashboard):
        super().__init__()
        self.dashboard = dashboard
        self.encoded = os.environ['DASHBOARD_LOGIN_HASH']
        if not self.encoded.startswith('pbkdf2_sha256$600000$'):
            raise ValueError('Generate DASHBOARD_LOGIN_HASH using scripts/dashboard-password.py')
        self.username = os.getenv('DASHBOARD_USER', 'quant')
        self.secure = os.getenv('DASHBOARD_COOKIE_SECURE', 'true').lower() != 'false'
        self.sessions = {}
        self.failures = []
        self.pending = 0
        self.html = (Path(__file__).parent / 'web/login.html').read_bytes()

    def active(self, request):
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v > now}
        return request.getCookie(b'quant_session') in self.sessions

    def render_GET(self, request):
        request.setHeader(b'Cache-Control', b'no-store')
        request.setHeader(b'X-Frame-Options', b'DENY')
        if request.path in (b'/healthz', b'/favicon.svg'):
            return self.dashboard.render_GET(request)
        if request.path == b'/login':
            request.setHeader(b'Content-Type', b'text/html; charset=utf-8')
            return self.html
        if not self.active(request):
            if request.path.startswith(b'/api/') or request.path == b'/readyz':
                request.setResponseCode(401)
                return b'Authentication required'
            request.redirect(b'/login')
            return b''
        return self.dashboard.render_GET(request)

    def render_POST(self, request):
        request.setHeader(b'Cache-Control', b'no-store')
        # Reject cross-origin submissions even behind the HTTPS reverse proxy.
        origin = request.getHeader('origin')
        scheme = 'https' if self.secure else 'http'
        if origin != scheme + '://' + (request.getHeader('host') or ''):
            request.setResponseCode(403)
            return b'Invalid origin'
        if request.path == b'/logout':
            self.sessions.pop(request.getCookie(b'quant_session'), None)
            request.addCookie(b'quant_session', b'', path=b'/', max_age=b'0',
                              secure=self.secure, httpOnly=True, sameSite=b'Strict')
            request.redirect(b'/login')
            return b''
        if request.path != b'/login':
            request.setResponseCode(404)
            return b'Not found'
        now = time.time()
        self.failures = [t for t in self.failures if t > now - 60]
        if len(self.failures) >= 10 or self.pending >= 2:
            request.setResponseCode(429)
            request.setHeader(b'Retry-After', b'60')
            return b'Too many attempts. Please wait a minute.'
        user = request.args.get(b'username', [b''])[0][:256].decode('utf-8', 'replace')
        raw = request.args.get(b'password', [b''])[0]
        if len(raw) > 1024:
            request.setResponseCode(400)
            return b'Invalid login'
        self.failures.append(now)
        self.pending += 1
        deferred = deferToThread(verify, raw.decode('utf-8', 'replace'), self.encoded)
        def finish(result):
            self.pending -= 1
            if getattr(request, '_disconnected', False):
                return
            if result is True and hmac.compare_digest(user.encode(), self.username.encode()):
                if len(self.sessions) >= 100:
                    self.sessions.pop(next(iter(self.sessions)))
                token = secrets.token_urlsafe(32).encode()
                self.sessions[token] = time.time() + 8 * 3600
                request.addCookie(b'quant_session', token, path=b'/', max_age=b'28800',
                                  secure=self.secure, httpOnly=True, sameSite=b'Strict')
                request.redirect(b'/')
                request.write(b'')
            else:
                request.setResponseCode(401)
                request.setHeader(b'Content-Type', b'text/html; charset=utf-8')
                request.write(self.html.replace(b'<!-- ERROR -->', b'<p role="alert">Incorrect username or password.</p>'))
            request.finish()
        def failed(failure):
            # Always complete the HTTP response if verification or cookie handling fails.
            if not getattr(request, '_disconnected', False) and not request.finished:
                request.setResponseCode(500)
                request.write(b'Sign-in failed. Please try again.')
                request.finish()
            return None
        deferred.addCallback(finish)
        deferred.addErrback(failed)
        return NOT_DONE_YET
