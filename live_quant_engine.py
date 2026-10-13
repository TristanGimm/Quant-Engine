"""Read-only cTrader market dashboard; all callbacks run on the Twisted reactor."""
import json
import logging
import os
import time
from collections import deque
from pathlib import Path

import numpy as np
import pywt
from dotenv import load_dotenv
from twisted.internet import reactor, task
from twisted.internet.error import CannotListenError
from twisted.internet.endpoints import SSL4ClientEndpoint
from twisted.internet.ssl import optionsForClientTLS
from twisted.application.internet import ClientService
from twisted.web.resource import Resource
from twisted.web.server import Site
from ctrader_open_api import Client, Protobuf, TcpProtocol
from ctrader_open_api.endpoints import EndPoints
from ctrader_open_api.factory import Factory
from ctrader_open_api.messages import OpenApiMessages_pb2 as oa
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent

LOG = logging.getLogger("quant-engine")
load_dotenv(Path(__file__).parent / ".env")


class VerifiedClient(Client):
    def __init__(self, host):
        super().__init__(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        endpoint = SSL4ClientEndpoint(reactor, host, EndPoints.PROTOBUF_PORT,
                                     optionsForClientTLS(host))
        ClientService.__init__(self, endpoint, Factory.forProtocol(TcpProtocol, client=self))


def advanced_denoise(data):
    if len(data) < 64 or np.std(data[-64:]) < 0.00001:
        return float(data[-1])
    window = np.asarray(list(data)[-64:])
    coeffs = pywt.swt(window, "sym4", level=3)
    filtered = [(a, pywt.threshold(d, np.std(d) * np.sqrt(2 * np.log(64)), mode="soft"))
                for a, d in coeffs]
    return float(pywt.iswt(filtered, "sym4")[-1])


def get_hurst(data):
    data = np.asarray(data, dtype=float)
    if len(data) < 20 or np.ptp(data) < 1e-10:
        return 0.5
    lags = np.arange(2, 20)
    tau = np.array([np.sqrt(np.std(data[lag:] - data[:-lag])) for lag in lags])
    valid = tau > 1e-12
    if valid.sum() < 2:
        return 0.5
    return float(np.clip(np.polyfit(np.log(lags[valid]), np.log(tau[valid]), 1)[0] * 2, 0, 1))


def estimate_ou_process(data):
    data = np.asarray(data, dtype=float)
    if len(data) < 20 or np.ptp(data[:-1]) < 1e-10:
        return 0.0
    slope, _ = np.polyfit(data[:-1], np.diff(data), 1)
    return float(max(-slope, 0))


def regime_for(hurst, mean_reversion_below=0.4, trend_above=0.6):
    if hurst > trend_above:
        return "TRENDING", "#38bdf8", float(np.clip((hurst - 0.5) * 200, 0, 100))
    if hurst < mean_reversion_below:
        return "MEAN REVERTING", "#c084fc", float(np.clip((0.5 - hurst) * 200, 0, 100))
    return "RANDOM WALK", "#fbbf24", 0.0


def efficiency_ratio(data):
    """Kaufman ER: net displacement / total absolute path, no direction sign."""
    data = np.asarray(data, dtype=float)
    if len(data) < 2:
        return None
    path = float(np.abs(np.diff(data)).sum())
    return float(np.clip(abs(data[-1] - data[0]) / path, 0, 1)) if path > 1e-12 else 0.0


def fit_ou(data, dt):
    """Conditional AR(1) fit mapped to an OU process at nominal fixed dt.

    This is a local model fit, not a stationarity test. Keep the original
    Euler-style lambda estimate separately in estimate_ou_process().
    """
    result = {"status": "insufficient", "phi": None, "mean": None,
              "kappa_per_second": None, "half_life_seconds": None, "zscore": None}
    data = np.asarray(data, dtype=float)
    if dt <= 0 or not np.isfinite(dt) or len(data) < 50 or not np.isfinite(data).all():
        return result
    x, y = data[:-1], data[1:]
    centered_x, centered_y = x - x.mean(), y - y.mean()
    denominator = float(centered_x @ centered_x)
    if denominator < 1e-16:
        result["status"] = "constant"
        return result
    phi = float(centered_x @ centered_y / denominator)
    result["phi"] = phi
    if not 0 < phi < 1:
        result["status"] = "non_reverting" if phi >= 1 else "not_ou"
        return result
    kappa = float(-np.log(phi) / dt)
    mean = float(x.mean() + (y.mean() - x.mean()) / (1 - phi))
    residual = centered_y - phi * centered_x
    stationary_sigma = float(np.sqrt(np.mean(residual ** 2) / (1 - phi ** 2)))
    result.update(status="fitted", mean=mean, kappa_per_second=kappa,
                  half_life_seconds=float(np.log(2) / kappa),
                  zscore=float((data[-1] - mean) / stationary_sigma) if stationary_sigma > 1e-12 else None)
    return result


class Engine:
    def __init__(self):
        self.environment = os.getenv("CTRADER_ENV", "demo")
        if self.environment not in ("demo", "live"):
            raise ValueError("CTRADER_ENV must be demo or live")
        self.client_id = os.getenv("CTRADER_CLIENT_ID", "")
        self.client_secret = os.getenv("CTRADER_CLIENT_SECRET", "")
        self.account_id = int(os.getenv("CTRADER_ACCOUNT_ID") or "0")
        self.symbol = os.getenv("CTRADER_SYMBOL", "XAUUSD")
        self.sample_interval = float(os.getenv("SAMPLE_INTERVAL_SECONDS", "0.05"))
        if not 0.05 <= self.sample_interval <= 60:
            raise ValueError("SAMPLE_INTERVAL_SECONDS must be between 0.05 and 60")
        self.hurst_low = float(os.getenv("HURST_MEAN_REVERSION_BELOW", "0.4"))
        self.hurst_high = float(os.getenv("HURST_TREND_ABOVE", "0.6"))
        if not 0 < self.hurst_low < 0.5 < self.hurst_high < 1:
            raise ValueError("Hurst thresholds must satisfy 0 < lower < 0.5 < upper < 1")
        self.token_path = Path(os.getenv("TOKEN_FILE", "data/tokens.json"))
        self.tokens = {"accessToken": os.getenv("CTRADER_ACCESS_TOKEN", ""),
                       "refreshToken": os.getenv("CTRADER_REFRESH_TOKEN", "")}
        if self.token_path.exists():
            self.tokens = json.loads(self.token_path.read_text())
        self.status = "Configuration missing"
        self.error = None
        self.client = None
        self.app_authenticated = False
        self.refreshing = False
        self.refresh_retry_after = 0
        self.symbol_id = None
        self.last_tick = None
        self.price = None
        self.points = deque(maxlen=256)
        self.prices = deque(maxlen=256)
        self.clean_prices = deque(maxlen=256)
        self.noise_history = deque([0.0] * 60, maxlen=60)
        self.hurst_history = deque([0.5] * 60, maxlen=60)
        self.ou_history = deque([0.0] * 60, maxlen=60)
        self.observed_samples = 0
        self.sample_tick_times = deque(maxlen=100)
        self.started = time.time()

    def send(self, message):
        self.client.send(message, responseTimeoutInSeconds=15).addErrback(self.request_failed)

    def request_failed(self, failure):
        self.refreshing = False
        self.refresh_retry_after = time.time() + 60
        self.error = "API request failed: " + failure.type.__name__
        self.status = "API request failed"
        LOG.warning(self.error)
        if self.client and self.client.isConnected:
            # A lost auth/subscription response otherwise leaves an idle socket forever.
            self.client.whenConnected(failAfterFailures=1).addCallback(
                lambda protocol: protocol.transport.loseConnection()
            ).addErrback(lambda ignored: None)
        return None

    def start(self):
        if not all((self.client_id, self.client_secret, self.account_id, self.tokens.get("accessToken"))):
            LOG.warning("cTrader configuration is missing; dashboard is showing setup status")
            return
        host = EndPoints.PROTOBUF_LIVE_HOST if self.environment == "live" else EndPoints.PROTOBUF_DEMO_HOST
        self.client = VerifiedClient(host)
        self.client.setConnectedCallback(self.connected)
        self.client.setDisconnectedCallback(self.disconnected)
        self.client.setMessageReceivedCallback(self.message)
        self.status = "Connecting to cTrader"
        self.client.startService()
        task.LoopingCall(self.maintenance).start(5, now=False)

    def connected(self, client):
        self.status = "Authorizing application"
        self.error = None
        self.app_authenticated = False
        self.refreshing = False
        self.symbol_id = None
        self.last_tick = None
        self.send(oa.ProtoOAApplicationAuthReq(clientId=self.client_id, clientSecret=self.client_secret))

    def disconnected(self, client, reason):
        self.app_authenticated = False
        self.refreshing = False
        self.status = "Disconnected; reconnecting automatically"
        self.error = "Broker connection interrupted"
        LOG.warning(self.status)

    def authenticate_account(self):
        self.status = "Authorizing account"
        self.send(oa.ProtoOAAccountAuthReq(ctidTraderAccountId=self.account_id,
                                         accessToken=self.tokens["accessToken"]))

    def refresh(self):
        if self.refreshing or time.time() < self.refresh_retry_after:
            return
        if not self.tokens.get("refreshToken"):
            self.error = "Token expired: authorize again or provide a refresh token"
            return
        self.refreshing = True
        self.send(oa.ProtoOARefreshTokenReq(refreshToken=self.tokens["refreshToken"]))

    def maintenance(self):
        if self.client and self.client.isConnected:
            # Explicit heartbeat every five seconds, independent of SDK defaults.
            self.client.whenConnected().addCallback(lambda p: p.send(ProtoHeartbeatEvent(), instant=True))
            if self.app_authenticated and self.tokens.get("expires_at", float("inf")) < time.time() + 300:
                self.refresh()
        elif time.time() - self.started > 30 and self.status == "Connecting to cTrader":
            self.status = "Broker unavailable; check DNS and outbound TCP port 5035"

    def message(self, client, message):
        res = Protobuf.extract(message)
        kind = type(res).__name__
        if kind in ("ProtoOAErrorRes", "ProtoErrorRes"):
            # Only publish the code; descriptions can contain sensitive request data.
            code = res.errorCode
            self.error = "cTrader: " + code
            self.status = "API error: check account, environment and credentials"
            LOG.warning("cTrader error code: %s", code)
            self.refreshing = False
            if code in ("ACCESS_TOKEN_EXPIRED", "CH_ACCESS_TOKEN_EXPIRED"):
                self.refresh()
            else:
                self.refresh_retry_after = time.time() + 60
            return
        if kind == "ProtoOAApplicationAuthRes":
            self.app_authenticated = True
            if self.tokens.get("expires_at", float("inf")) < time.time() + 300:
                self.refresh()
            else:
                self.authenticate_account()
        elif kind == "ProtoOARefreshTokenRes":
            self.tokens = {"accessToken": res.accessToken, "refreshToken": res.refreshToken,
                           "expires_at": time.time() + res.expiresIn}
            self.refreshing = False
            try:
                save_tokens(self.token_path, self.tokens)
            except OSError:
                self.error = "Could not save token; check write permissions"
                LOG.error(self.error)
                return
            self.authenticate_account()
        elif kind == "ProtoOAAccountAuthRes":
            self.error = None
            self.status = "Loading symbol list"
            self.send(oa.ProtoOASymbolsListReq(ctidTraderAccountId=self.account_id))
        elif kind == "ProtoOASymbolsListRes":
            symbols = [s for s in res.symbol if s.symbolName == self.symbol]
            if not symbols:
                self.error = "Symbol not found: check CTRADER_SYMBOL with your broker"
                self.status = "No market feed"
                return
            self.symbol_id = symbols[0].symbolId
            self.send(oa.ProtoOASubscribeSpotsReq(ctidTraderAccountId=self.account_id,
                                               symbolId=[self.symbol_id]))
            self.status = "Waiting for market data (market may be closed)"
        elif kind == "ProtoOASpotEvent" and res.symbolId == self.symbol_id and res.ctidTraderAccountId == self.account_id:
            if res.HasField("bid") and res.bid > 0:
                # cTrader spot prices use a fixed scale of 100000; never guess magnitude.
                self.price = res.bid / 100000.0
                self.last_tick = time.time()
                self.status = "Live"
                self.error = None
        elif kind in ("ProtoOAAccountDisconnectEvent", "ProtoOAAccountsTokenInvalidatedEvent"):
            self.error = "Account authorization ended; check OAuth and permissions"
            self.status = "Authorization invalid"
            self.last_tick = None

    def sample(self):
        if self.price is None or self.last_tick is None or self.status != "Live" or time.time() - self.last_tick > 120:
            return
        # Preserve the original fixed-time sampling, holding the last broker bid.
        # The old Matplotlib loop used plt.pause(0.05), including unchanged prices.
        if not self.prices:
            self.prices.extend([self.price] * 256)
            self.clean_prices.extend([self.price] * 256)
        self.prices.append(self.price)
        self.observed_samples += 1
        self.sample_tick_times.append(self.last_tick)
        prices = list(self.prices)
        clean = advanced_denoise(prices)
        self.clean_prices.append(clean)
        hurst = get_hurst(prices[-100:])
        ou = estimate_ou_process(prices[-50:])
        noise = float(np.mean(np.abs(np.array(prices[-20:]) - np.array(list(self.clean_prices)[-20:]))))
        regime, color, confidence = regime_for(hurst, self.hurst_low, self.hurst_high)
        actual = prices[-min(self.observed_samples, 100):]
        diagnostics = {"efficiency_ratio": efficiency_ratio(actual),
                       "ou_fit": fit_ou(actual[-50:], self.sample_interval)}
        self.noise_history.append(noise)
        self.hurst_history.append(hurst)
        self.ou_history.append(ou)
        self.points.append({"time": time.time(), "price": self.price, "clean": clean,
                            "hurst": hurst, "ou": ou, "noise": noise,
                            "regime": regime, "color": color, "confidence": confidence,
                            **diagnostics})

    def snapshot(self):
        age = None if self.last_tick is None else max(0, time.time() - self.last_tick)
        warming_up = self.observed_samples < 100
        has_variation = bool(self.prices) and np.ptp(list(self.prices)[-100:]) > 1e-10
        return {"status": self.status, "error": self.error, "environment": self.environment,
                "symbol": self.symbol, "price": self.price, "tick_age_seconds": age,
                "ready": self.status == "Live" and age is not None and age < 120,
                "sample_interval_seconds": self.sample_interval,
                "hurst_thresholds": {"mean_reversion_below": self.hurst_low, "trend_above": self.hurst_high},
                "analysis_ready": not warming_up and bool(has_variation),
                "analysis_status": ("Warming up: " + str(self.observed_samples) + "/100" if warming_up
                                    else "Short-window estimate" if has_variation else "No price variation"),
                "distinct_ticks_in_window": len(set(self.sample_tick_times)),
                "hurst_window_seconds": 99 * self.sample_interval,
                "ou_window_seconds": 49 * self.sample_interval,
                "price_history": list(self.prices)[-100:],
                "clean_history": list(self.clean_prices)[-100:],
                "noise_history": list(self.noise_history),
                "hurst_history": list(self.hurst_history),
                "ou_history": list(self.ou_history),
                "points": list(self.points)}


def save_tokens(path, tokens):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(tokens, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class Dashboard(Resource):
    isLeaf = True

    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.html = (Path(__file__).parent / "web" / "index.html").read_bytes()

    def render_GET(self, request):
        request.setHeader(b"Cache-Control", b"no-store")
        request.setHeader(b"X-Content-Type-Options", b"nosniff")
        if request.path == b"/healthz":
            request.setHeader(b"Content-Type", b"application/json")
            return b'{"ok":true}'
        if request.path in (b"/api/state", b"/readyz"):
            state = self.engine.snapshot()
            if request.path == b"/readyz" and not state["ready"]:
                request.setResponseCode(503)
            request.setHeader(b"Content-Type", b"application/json")
            return json.dumps(state, allow_nan=False).encode()
        if request.path == b"/":
            request.setHeader(b"Content-Type", b"text/html; charset=utf-8")
            return self.html
        request.setResponseCode(404)
        return b"Not found"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = Engine()
    port = int(os.getenv("PORT", "8080"))
    bind_host = os.getenv("BIND_HOST", "127.0.0.1")
    try:
        dashboard = Dashboard(engine)
        if os.getenv("DASHBOARD_LOGIN_HASH"):
            from dashboard_auth import Login
            dashboard = Login(dashboard)
        elif bind_host != "127.0.0.1":
            raise ValueError("DASHBOARD_LOGIN_HASH is required when exposing the dashboard")
        reactor.listenTCP(port, Site(dashboard), interface=bind_host)
    except CannotListenError:
        LOG.error("Dashboard cannot start on %s:%s. Check port availability and permissions; "
                  "set PORT in .env to use a different local port.", bind_host, port)
        raise SystemExit(1)
    engine.start()
    task.LoopingCall(engine.sample).start(engine.sample_interval, now=False)
    LOG.info("Dashboard started")
    reactor.run()


if __name__ == "__main__":
    main()
