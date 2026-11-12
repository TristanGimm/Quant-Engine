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
        # The public landing page and its videos. Served to anonymous visitors
        # and to anyone who asks for an asset; they carry no account data.
        web = Path(__file__).parent / 'web'
        self.web = web
        self.landing = (web / 'landing.html').read_bytes()
        # Register/reset pages share the sign-in stylesheet.
        self.pages = {
            b'/login/register': 'register.html',
            b'/login/reset': 'reset.html',
        }
        self.video_dir = (web / 'videos').resolve()
        self.video_types = {
            '.mp4': b'video/mp4',
            '.jpg': b'image/jpeg',
            '.jpeg': b'image/jpeg',
            '.png': b'image/png',
        }

    def asset(self, name, fallback=b''):
        """Read a small text asset from disk.

        Reading per request keeps a regenerated landing page or stylesheet from
        being masked by a stale copy held since startup; these files are a few
        kilobytes and only served to local visitors.
        """
        try:
            return (self.web / name).read_bytes()
        except OSError:
            return fallback

    def active(self, request):
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v > now}
        return request.getCookie(b'quant_session') in self.sessions

    def render_video(self, request):
        """Serve the landing page's media from web/videos.

        The path is resolved and then checked against the media directory, so a
        request like /videos/../../live_quant_engine.py cannot escape it.
        """
        try:
            requested = (self.video_dir / Path(request.path[len(b'/videos/'):].decode('utf-8', 'replace')).name).resolve()
        except (ValueError, OSError):
            request.setResponseCode(400)
            return b'Invalid path'
        if not str(requested).startswith(str(self.video_dir)) or not requested.is_file():
            request.setResponseCode(404)
            return b'Not found'
        content_type = self.video_types.get(requested.suffix.lower())
        if content_type is None:
            request.setResponseCode(404)
            return b'Not found'
        request.setHeader(b'Content-Type', content_type)
        request.setHeader(b'Cache-Control', b'public, max-age=604800')
        request.setHeader(b'X-Content-Type-Options', b'nosniff')
        return requested.read_bytes()

    def render_GET(self, request):
        if request.path == b'/nav.js':
            request.setHeader(b'Content-Type', b'text/javascript; charset=utf-8')
            request.setHeader(b'Cache-Control', b'no-cache')
            request.setHeader(b'X-Content-Type-Options', b'nosniff')
            return self.asset('nav.js')
        if request.path == b'/auth.css':
            request.setHeader(b'Content-Type', b'text/css; charset=utf-8')
            request.setHeader(b'Cache-Control', b'no-cache')
            return self.asset('auth.css')
        if request.path in self.pages:
            request.setHeader(b'Content-Type', b'text/html; charset=utf-8')
            request.setHeader(b'Cache-Control', b'no-store')
            return self.asset(self.pages[request.path]).replace(b'<!-- ERROR -->', b'')
        if request.path == b'/landing.js':
            request.setHeader(b'Content-Type', b'text/javascript; charset=utf-8')
            request.setHeader(b'Cache-Control', b'no-cache')
            request.setHeader(b'X-Content-Type-Options', b'nosniff')
            return self.asset('landing.js')
        if request.path.startswith(b'/videos/'):
            # Public, cacheable, and identical for every visitor.
            return self.render_video(request)
        request.setHeader(b'Cache-Control', b'no-store')
        request.setHeader(b'X-Frame-Options', b'DENY')
        if request.path in (b'/healthz', b'/favicon.svg'):
            return self.dashboard.render_GET(request)
        if request.path == b'/login':
            request.setHeader(b'Content-Type', b'text/html; charset=utf-8')
            return self.html
        if not self.active(request):
            if request.path == b'/':
                # Anonymous visitors get the shared landing page instead of a
                # bare redirect to the password form.
                request.setHeader(b'Content-Type', b'text/html; charset=utf-8')
                return self.asset('landing.html', self.landing)
            if request.path.startswith(b'/api/') or request.path == b'/readyz':
                request.setResponseCode(401)
                return b'Authentication required'
            request.redirect(b'/login')
            return b''
        return self.dashboard.render_GET(request)

    def account_action(self, request, path):
        """Create an account or change a password, gated by DASHBOARD_SETUP_TOKEN.

        The workspace is private, so an open sign-up form would be a hole. The
        token is a server-side secret, the same idea the Copier Engine uses.

        An account has a username and a contact email. Either one signs in, and
        either one can reset the password, so a forgotten username is not a
        lockout.
        """
        from dashboard_users import (
            create, email_taken, exists, set_password, token_ok, username_taken, valid_email,
        )
        from dashboard_auth import password_hash

        registering = path == b'/login/register'
        page = b'/login/register' if registering else b'/login/reset'
        args = {
            key: request.args.get(key, [b''])[0].decode('utf-8', 'replace')
            for key in (b'username', b'password', b'token', b'email')
        }
        username = args[b'username'].strip()
        password = args[b'password']
        email = args[b'email'].strip()
        message = b''
        if not token_ok(args[b'token']):
            message = b'Invalid setup token.'
        elif len(username) < 2 or len(username) > 254:
            message = b'Use a username of 2 to 254 characters.'
        elif len(password) < 12:
            message = b'Use a password of at least 12 characters.'
        elif registering and not valid_email(email):
            message = b'Enter a valid email address.'
        elif registering and username_taken(username):
            message = b'That username is already taken.'
        elif registering and email_taken(email):
            message = b'That email address is already registered.'
        elif not registering and not exists(username):
            message = b'No account with that username or email.'

        if not message:
            try:
                if registering:
                    create(username, email, password, password_hash)
                elif not set_password(username, password, password_hash):
                    message = b'No account with that username or email.'
            except Exception:  # sqlite errors, hashing failures
                message = b'Could not update the account. Try again.'
            else:
                # Return the body and let Twisted write it. Calling finish()
                # here made render_POST return None, which Twisted treats as a
                # failed render and then writes again — "write after finish".
                request.redirect(b'/login')
                return b''

        request.setResponseCode(400)
        request.setHeader(b'Content-Type', b'text/html; charset=utf-8')
        request.setHeader(b'Cache-Control', b'no-store')
        return self.asset(self.pages[page]).replace(b'<!-- ERROR -->', b'<p role="alert">' + message + b'</p>')

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
        if request.path in (b'/login/register', b'/login/reset'):
            # /login itself is handled below; routing it here swallowed the
            # sign-in form and answered every attempt with 400.
            return self.account_action(request, request.path)
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
        def check_account():
            """Environment credential first, then the accounts created here."""
            from dashboard_users import verify as verify_account
            supplied = raw.decode('utf-8', 'replace')
            if hmac.compare_digest(user.encode(), self.username.encode()) and verify(supplied, self.encoded):
                return True
            try:
                return verify_account(user, supplied, password_hash)
            except Exception:
                # A missing or unreadable user table must not break the env login.
                return False

        deferred = deferToThread(check_account)
        def finish(result):
            self.pending -= 1
            if getattr(request, '_disconnected', False):
                return
            if result is True:
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
                request.write(self.asset('login.html', self.html).replace(b'<!-- ERROR -->', b'<p role="alert">Incorrect username or password.</p>'))
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
