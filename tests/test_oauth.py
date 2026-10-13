import unittest
import os
import json
import tempfile
import io
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from Generating_Keys.get_token import api_error, parse_code, app_auth_result
from Generating_Keys.get_token import main, account_lines
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetAccountListByAccessTokenRes
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAApplicationAuthRes, ProtoOAErrorRes
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage, ProtoHeartbeatEvent


class OAuthTest(unittest.TestCase):
    def test_account_ids_are_internal_ids_with_matching_environment(self):
        response = ProtoOAGetAccountListByAccessTokenRes(accessToken='private-access')
        response.ctidTraderAccount.add(ctidTraderAccountId=123, isLive=False, traderLogin=999)
        response.ctidTraderAccount.add(ctidTraderAccountId=456, isLive=True)
        lines = account_lines(response)
        self.assertIn('CTRADER_ACCOUNT_ID=123 | CTRADER_ENV=demo | Account login=999', lines)
        self.assertIn('CTRADER_ACCOUNT_ID=456 | CTRADER_ENV=live', lines)
        self.assertNotIn('private-access', '\n'.join(lines))

    def test_playground_import_saves_without_printing_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tokens.json'
            output = io.StringIO()
            with patch.dict(os.environ, {'TOKEN_FILE': str(path)}), \
                    patch('sys.argv', ['get_token.py', '--import-tokens']), \
                    patch('getpass.getpass', side_effect=['private-access', 'private-refresh']), \
                    redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads(path.read_text())['refreshToken'], 'private-refresh')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('private-access', output.getvalue())
            self.assertNotIn('private-refresh', output.getvalue())

    def test_app_auth_distinguishes_credentials_from_oauth(self):
        def message(payload):
            return ProtoMessage(payloadType=payload.payloadType, payload=payload.SerializeToString())
        status, text = app_auth_result(message(ProtoOAApplicationAuthRes()), ('private-secret',))
        self.assertEqual(status, 0)
        self.assertIn('APP_LOGIN_OK', text)
        status, text = app_auth_result(message(ProtoOAErrorRes(errorCode='ACCESS_DENIED',
                                                             description='private-secret')), ('private-secret',))
        self.assertEqual(status, 1)
        self.assertIn('ACCESS_DENIED', text)
        self.assertNotIn('private-secret', text)
        self.assertIsNone(app_auth_result(message(ProtoHeartbeatEvent()), ('private-secret',)))

    def test_accepts_url_or_code(self):
        for value in ('sample-code', 'http://localhost/?code=sample-code&other=value',
                      'code=sample-code', '?code=sample-code'):
            self.assertEqual(parse_code(value), 'sample-code')

    def test_missing_code_is_rejected(self):
        for value in ('', 'http://localhost/', 'http://localhost/?error=denied'):
            with self.assertRaises(ValueError):
                parse_code(value)

    def test_errors_never_include_response_description_or_credentials(self):
        message = api_error({'errorCode': 'INVALID_CLIENT', 'description': 'private-secret'}, ('private-secret',))
        self.assertIn('INVALID_CLIENT', message)
        self.assertNotIn('private-secret', message)
        message = api_error({'errorCode': 'private_secret'}, ('private_secret',))
        self.assertNotIn('private_secret', message)


if __name__ == '__main__':
    unittest.main()
