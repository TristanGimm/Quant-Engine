"""Interactive read-only OAuth flow; secrets are stored, never printed."""
import getpass
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_quant_engine import save_tokens


def parse_code(value):
    value = value.strip()
    if "://" in value:
        parsed = urllib.parse.urlsplit(value)
        values = urllib.parse.parse_qs(parsed.query)
        value = values.get("code", [""])[0]
    elif value.startswith("code=") or value.startswith("?code="):
        value = urllib.parse.parse_qs(value.lstrip("?")).get("code", [""])[0]
    if not value or any(char.isspace() for char in value) or "&" in value:
        raise ValueError("Invalid code: paste the complete redirect URL or the code alone")
    return value


def api_error(result, sensitive):
    # Only expose a structured error identifier, never the response description or URL.
    code = str(result.get("errorCode") or result.get("error") or "")
    safe = re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", code)
    if not safe or "_" not in code or any(value and value in code for value in sensitive):
        code = "UNKNOWN_TOKEN_RESPONSE"
    return "cTrader rejected the token exchange: " + code + ". Check the client secret, a fresh code, and the redirect URI."


def app_auth_result(message, sensitive):
    from ctrader_open_api import Protobuf
    response = Protobuf.extract(message)
    kind = type(response).__name__
    if kind == "ProtoOAApplicationAuthRes":
        return 0, "APP_LOGIN_OK: cTrader accepts this client ID and client secret."
    if kind in ("ProtoOAErrorRes", "ProtoErrorRes"):
        error = api_error({"errorCode": response.errorCode}, sensitive)
        identifier = error.split(": ", 1)[-1].split(". ", 1)[0]
        return 1, "APP_LOGIN_REJECTED: " + identifier + ". Check the app status and matching client credentials."
    return None


def account_lines(response):
    return ["CTRADER_ACCOUNT_ID=" + str(account.ctidTraderAccountId)
            + " | CTRADER_ENV=" + ("live" if account.isLive else "demo")
            + (" | Account login=" + str(account.traderLogin) if account.HasField("traderLogin") else "")
            for account in response.ctidTraderAccount]


def check_app(client_id, client_secret, list_accounts=False):
    from twisted.internet import reactor
    from twisted.application.internet import ClientService
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAApplicationAuthReq, ProtoOAGetAccountListByAccessTokenReq
    from ctrader_open_api import Protobuf
    from ctrader_open_api.endpoints import EndPoints
    from live_quant_engine import VerifiedClient

    access_token = os.getenv("CTRADER_ACCESS_TOKEN", "")
    if list_accounts:
        token_path = Path(os.getenv("TOKEN_FILE", "data/tokens.json"))
        try:
            if token_path.exists():
                access_token = json.loads(token_path.read_text()).get("accessToken", "")
        except (OSError, ValueError, AttributeError):
            print("Could not read the token file; import the tokens again.", file=sys.stderr)
            return 1
        if not access_token:
            print("No access token found; use --import-tokens first.", file=sys.stderr)
            return 1
    environment = os.getenv("CTRADER_ENV", "demo")
    if environment not in ("demo", "live"):
        print("CTRADER_ENV must be demo or live.", file=sys.stderr)
        return 1
    host = EndPoints.PROTOBUF_LIVE_HOST if environment == "live" else EndPoints.PROTOBUF_DEMO_HOST
    client = VerifiedClient(host)
    outcome = [1]
    finished = [False]

    def finish(status, message):
        if finished[0]:
            return
        finished[0] = True
        outcome[0] = status
        print(message)
        if timeout.active():
            timeout.cancel()
        ClientService.stopService(client)
        reactor.callLater(0, reactor.stop)

    def connected(service):
        service.send(ProtoOAApplicationAuthReq(clientId=client_id, clientSecret=client_secret),
                     responseTimeoutInSeconds=15).addErrback(
            lambda failure: finish(1, "APP_TEST_FAILED: " + failure.type.__name__))

    def received(service, message):
        response = Protobuf.extract(message)
        kind = type(response).__name__
        if list_accounts and kind == "ProtoOAApplicationAuthRes":
            service.send(ProtoOAGetAccountListByAccessTokenReq(accessToken=access_token),
                         responseTimeoutInSeconds=15).addErrback(
                lambda failure: finish(1, "ACCOUNT_LIST_FAILED: " + failure.type.__name__))
            return
        if list_accounts and kind == "ProtoOAGetAccountListByAccessTokenRes":
            lines = account_lines(response)
            finish(0 if lines else 1, "\n".join(lines) if lines else "No authorized accounts found for this token.")
            return
        result = app_auth_result(message, (client_id, client_secret, access_token))
        if result is not None:
            finish(*result)

    client.setConnectedCallback(connected)
    client.setMessageReceivedCallback(received)
    timeout = reactor.callLater(25, finish, 1,
                                "APP_TEST_TIMEOUT: Broker connection or response missing; check outbound port 5035.")
    print("Reading authorized accounts over TLS; no orders or token refresh." if list_accounts
          else "Checking application login over TLS. No tokens or orders are requested.")
    client.startService()
    reactor.run()
    return outcome[0]


def main():
    if "--import-tokens" in sys.argv[1:]:
        print("Paste freshly generated tokens from the cTrader Playground. Both inputs remain hidden.")
        access = getpass.getpass("Access Token: ").strip()
        refresh = getpass.getpass("Refresh Token: ").strip()
        if not access or not refresh or access == refresh or any(c.isspace() for c in access + refresh):
            print("Two different, complete tokens without whitespace are required.", file=sys.stderr)
            return 1
        path = Path(os.getenv("TOKEN_FILE", "data/tokens.json"))
        try:
            # Unknown expiry: renew once at next startup rather than guess remaining lifetime.
            save_tokens(path, {"accessToken": access, "refreshToken": refresh, "expires_at": 0})
        except OSError:
            print("Could not save tokens; check the path and write permissions.", file=sys.stderr)
            return 1
        print("Tokens saved to", path, "(excluded from Git). The engine checks/refreshes them on the next startup.")
        return 0
    list_accounts = "--list-accounts" in sys.argv[1:]
    check_only = "--check-app" in sys.argv[1:]
    client_id = ("" if check_only else os.getenv("CTRADER_CLIENT_ID")) or input("Client ID: ").strip()
    client_secret = (("" if check_only else os.getenv("CTRADER_CLIENT_SECRET")) or getpass.getpass("Client Secret: ")).strip()
    if not client_id or not client_secret:
        print("Client ID and client secret must not be empty.", file=sys.stderr)
        return 1
    if check_only or list_accounts:
        return check_app(client_id, client_secret, list_accounts=list_accounts)
    redirect = os.getenv("CTRADER_REDIRECT_URI", "http://localhost/")
    params = urllib.parse.urlencode({"client_id": client_id, "redirect_uri": redirect,
                                    "scope": "accounts", "product": "web"})
    print("Open this URL and authorize account access (no trading permission):")
    print("https://id.ctrader.com/my/settings/openapi/grantingaccess/?" + params)
    print("After authorization, a localhost error page is expected.")
    print("Copy the complete browser URL and paste it here (input remains hidden).")
    print("Use the code within one minute; authorize again for each attempt.")
    try:
        code = parse_code(getpass.getpass("Redirect URL or code: "))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    query = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                   "client_id": client_id, "client_secret": client_secret,
                                   "redirect_uri": redirect})
    try:
        with urllib.request.urlopen("https://openapi.ctrader.com/apps/token?" + query, timeout=20) as response:
            result = json.load(response)
        if not result.get("accessToken") or not result.get("refreshToken"):
            print(api_error(result, (client_id, client_secret, code)), file=sys.stderr)
            return 1
        result = {"accessToken": result["accessToken"], "refreshToken": result["refreshToken"],
                  "expires_at": time.time() + int(result["expiresIn"])}
        path = Path(os.getenv("TOKEN_FILE", "data/tokens.json"))
        save_tokens(path, result)
        print("Tokens securely saved to", path, "— do not commit this file to Git.")
    except urllib.error.HTTPError as exc:
        print("Token endpoint returned HTTP " + str(exc.code) + ".", file=sys.stderr)
        try:
            result = json.load(exc)
            print(api_error(result, (client_id, client_secret, code)), file=sys.stderr)
        except (ValueError, TypeError, AttributeError):
            print("Check the client secret, a fresh code, and the registered redirect URI.", file=sys.stderr)
        return 1
    except Exception as exc:
        # HTTP exception messages can contain the full credential-bearing URL.
        print("OAuth failed (" + type(exc).__name__ + "). Check app authorization, redirect URI, and code.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
