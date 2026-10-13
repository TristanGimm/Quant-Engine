import os
import time
import unittest
from unittest.mock import Mock, patch
from twisted.internet.defer import succeed
from twisted.web.test.requesthelper import DummyRequest
from twisted.web.http import Request
from types import SimpleNamespace
from dashboard_auth import Login, password_hash, verify


class AuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.encoded = password_hash('a long test password')

    def setUp(self):
        with patch.dict(os.environ, {'DASHBOARD_LOGIN_HASH': self.encoded}):
            self.auth = Login(Mock())

    def request(self, path, origin='https://quant.example', cookie=None):
        req = DummyRequest([])
        req.path = path
        req.requestHeaders.addRawHeader(b'host', b'quant.example')
        req.requestHeaders.addRawHeader(b'origin', origin.encode())
        req.getCookie = lambda name: cookie
        cookie_request = SimpleNamespace(cookies=[])
        req.addCookie = Mock(side_effect=lambda *a, **kw: Request.addCookie(cookie_request, *a, **kw))
        req.serialized_cookies = cookie_request.cookies
        return req

    def test_hash_verification(self):
        self.assertTrue(verify('a long test password', self.encoded))
        self.assertFalse(verify('wrong', self.encoded))
        self.assertFalse(verify('wrong', 'invalid'))

    def test_protection_expiry_and_health(self):
        req = self.request(b'/api/state')
        self.auth.render_GET(req)
        self.assertEqual(req.responseCode, 401)
        self.auth.render_GET(self.request(b'/healthz'))
        self.auth.dashboard.render_GET.assert_called_once()
        self.auth.sessions[b'token'] = time.time() - 1
        self.assertFalse(self.auth.active(self.request(b'/', cookie=b'token')))

    def test_login_cookie_and_logout(self):
        req = self.request(b'/login')
        req.args = {b'username': [b'quant'], b'password': [b'a long test password']}
        with patch('dashboard_auth.deferToThread', side_effect=lambda f,*a: succeed(f(*a))):
            self.auth.render_POST(req)
        args, kwargs = req.addCookie.call_args
        self.assertTrue(kwargs['secure'])
        self.assertTrue(kwargs['httpOnly'])
        self.assertIn(b'Max-Age=28800', req.serialized_cookies[0])
        self.assertTrue(req.finished)
        self.assertEqual(kwargs['sameSite'], b'Strict')
        self.assertTrue(self.auth.active(self.request(b'/', cookie=args[1])))
        logout = self.request(b'/logout', cookie=args[1])
        self.auth.render_POST(logout)
        self.assertFalse(self.auth.sessions)

    def test_origin_and_rate_limit(self):
        req = self.request(b'/login', 'https://evil.example')
        self.auth.render_POST(req)
        self.assertEqual(req.responseCode, 403)
        self.auth.failures = [time.time()] * 10
        req = self.request(b'/login')
        self.auth.render_POST(req)
        self.assertEqual(req.responseCode, 429)

    def test_wrong_password_no_session(self):
        req = self.request(b'/login')
        req.args = {b'username': [b'quant'], b'password': [b'wrong']}
        with patch('dashboard_auth.deferToThread', side_effect=lambda f,*a: succeed(f(*a))):
            self.auth.render_POST(req)
        self.assertEqual(req.responseCode, 401)
        self.assertFalse(self.auth.sessions)

    def test_callback_failure_finishes_request(self):
        req = self.request(b'/login')
        req.args = {b'username': [b'quant'], b'password': [b'a long test password']}
        req.addCookie.side_effect = RuntimeError('cookie failure')
        with patch('dashboard_auth.deferToThread', side_effect=lambda f,*a: succeed(True)):
            self.auth.render_POST(req)
        self.assertEqual(req.responseCode, 500)
        self.assertTrue(req.finished)
