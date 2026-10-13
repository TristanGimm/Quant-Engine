import json
import os
import tempfile
import time
import unittest
import numpy as np
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
from twisted.internet.defer import succeed
from twisted.python.failure import Failure

from ctrader_open_api.messages import OpenApiMessages_pb2 as oa
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from twisted.web.test.requesthelper import DummyRequest

from live_quant_engine import Dashboard, Engine, advanced_denoise, estimate_ou_process, get_hurst, save_tokens, regime_for, efficiency_ratio, fit_ou


def envelope(payload):
    return ProtoMessage(payloadType=payload.payloadType, payload=payload.SerializeToString())


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"TOKEN_FILE": self.temp.name + "/tokens.json",
                                          "CTRADER_ENV": "demo", "CTRADER_ACCOUNT_ID": "123",
                                          "SAMPLE_INTERVAL_SECONDS": "0.05",
                                          "HURST_MEAN_REVERSION_BELOW": "0.4", "HURST_TREND_ABOVE": "0.6"})
        self.env.start()
        self.engine = Engine()
        self.sent = []
        self.engine.send = self.sent.append

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def deliver(self, message):
        self.engine.message(None, envelope(message))

    def test_authentication_and_subscription(self):
        self.engine.tokens["accessToken"] = "test-token"
        self.deliver(oa.ProtoOAApplicationAuthRes())
        self.assertIsInstance(self.sent[-1], oa.ProtoOAAccountAuthReq)
        self.deliver(oa.ProtoOAAccountAuthRes(ctidTraderAccountId=123))
        self.assertIsInstance(self.sent[-1], oa.ProtoOASymbolsListReq)
        symbols = oa.ProtoOASymbolsListRes(ctidTraderAccountId=123)
        symbols.symbol.add(symbolId=42, symbolName="XAUUSD")
        self.deliver(symbols)
        self.assertEqual(list(self.sent[-1].symbolId), [42])
        self.assertIsInstance(self.sent[-1], oa.ProtoOASubscribeSpotsReq)

    def test_price_scale_legacy_sampling_and_stale_data_pause(self):
        self.engine.symbol_id = 42
        self.deliver(oa.ProtoOASpotEvent(ctidTraderAccountId=123, symbolId=42, bid=265012345))
        self.assertEqual(self.engine.price, 2650.12345)
        self.engine.sample()
        self.engine.sample()
        self.assertEqual(len(self.engine.points), 2)
        self.assertEqual(len(self.engine.prices), 256)
        self.assertEqual(self.engine.sample_interval, .05)
        self.assertTrue(self.engine.snapshot()["ready"])
        self.assertFalse(self.engine.snapshot()["analysis_ready"])
        self.engine.last_tick -= 121
        self.assertFalse(self.engine.snapshot()["ready"])
        self.engine.sample()
        self.assertEqual(len(self.engine.points), 2)
        self.deliver(oa.ProtoOASpotEvent(ctidTraderAccountId=123, symbolId=99, bid=10))
        self.assertEqual(self.engine.price, 2650.12345)

    def test_original_hurst_ou_noise_and_confidence(self):
        values = 2650 + np.cumsum(np.random.default_rng(7).normal(0, .2, 130))
        price_history = [float(values[0])] * 256
        clean_history = price_history.copy()
        self.engine.status = 'Live'
        for value in values:
            self.engine.price = float(value)
            self.engine.last_tick = time.time()
            self.engine.sample()
            price_history = (price_history + [float(value)])[-256:]
            clean_history = (clean_history + [advanced_denoise(price_history)])[-256:]
        window = np.array(price_history[-100:])
        tau = [np.sqrt(np.std(window[lag:] - window[:-lag])) for lag in range(2, 20)]
        legacy_hurst = np.clip(np.polyfit(np.log(range(2, 20)), np.log(tau), 1)[0] * 2, 0, 1)
        ou_window = np.array(price_history[-50:])
        legacy_ou = max(-np.polyfit(ou_window[:-1], np.diff(ou_window), 1)[0], 0)
        legacy_noise = np.mean(np.abs(np.array(price_history[-20:]) - np.array(clean_history[-20:])))
        point = self.engine.points[-1]
        self.assertAlmostEqual(point['hurst'], legacy_hurst)
        self.assertAlmostEqual(point['ou'], legacy_ou)
        self.assertAlmostEqual(point['noise'], legacy_noise)
        self.assertEqual(self.engine.snapshot()['price_history'], price_history[-100:])
        self.assertEqual(len(self.engine.snapshot()['noise_history']), 60)
        self.assertEqual(regime_for(.6), ('RANDOM WALK', '#fbbf24', 0.0))
        self.assertEqual(regime_for(.4), ('RANDOM WALK', '#fbbf24', 0.0))
        self.assertAlmostEqual(regime_for(.7)[2], 40)
        self.assertAlmostEqual(regime_for(.3)[2], 40)
        self.assertEqual(regime_for(.63, trend_above=.666)[0], 'RANDOM WALK')
        self.assertEqual(regime_for(.666, trend_above=.666)[0], 'RANDOM WALK')
        self.assertEqual(regime_for(.667, trend_above=.666)[0], 'TRENDING')

    def test_efficiency_measures_path_not_direction(self):
        self.assertEqual(efficiency_ratio([1, 2, 3, 4]), 1)
        self.assertEqual(efficiency_ratio([4, 3, 2, 1]), 1)
        self.assertEqual(efficiency_ratio([1, 2, 1, 2, 1]), 0)
        self.assertEqual(efficiency_ratio([2, 2, 2]), 0)
        self.assertIsNone(efficiency_ratio([1]))

    def test_ou_time_units_and_rejects_invalid_models(self):
        rng = np.random.default_rng(34)
        data = [20.]
        for noise in rng.normal(0, .2, 8000):
            data.append(20 + .95 * (data[-1] - 20) + noise)
        fit = fit_ou(data, .1)
        self.assertEqual(fit['status'], 'fitted')
        self.assertAlmostEqual(fit['phi'], .95, delta=.015)
        self.assertAlmostEqual(fit['mean'], 20, delta=.15)
        self.assertAlmostEqual(fit['half_life_seconds'], np.log(2) / fit['kappa_per_second'])
        self.assertAlmostEqual(fit_ou(data, .2)['half_life_seconds'], 2 * fit['half_life_seconds'])
        self.assertEqual(fit_ou([1.] * 50, .1)['status'], 'constant')
        self.assertEqual(fit_ou([1., 2.], .1)['status'], 'insufficient')
        divergent = fit_ou(1.1 ** np.arange(50), .1)
        self.assertEqual(divergent['status'], 'non_reverting')
        self.assertIsNone(divergent['half_life_seconds'])
        self.assertEqual(fit_ou(20 + (-.8) ** np.arange(50), .1)['status'], 'not_ou')

    def test_token_refresh_persistence_and_reauthentication(self):
        self.engine.tokens = {"accessToken": "old", "refreshToken": "refresh", "expires_at": 0}
        self.deliver(oa.ProtoOAApplicationAuthRes())
        self.assertIsInstance(self.sent[-1], oa.ProtoOARefreshTokenReq)
        self.engine.refresh()
        self.assertEqual(len(self.sent), 1)
        self.deliver(oa.ProtoOARefreshTokenRes(accessToken="new", tokenType="bearer",
                                             expiresIn=3600, refreshToken="rotated"))
        saved = json.loads(self.engine.token_path.read_text())
        self.assertEqual(saved["refreshToken"], "rotated")
        self.assertGreater(saved["expires_at"], time.time() + 3500)
        self.assertEqual(self.engine.token_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.sent[-1].accessToken, "new")

    def test_error_description_and_credentials_not_published(self):
        self.deliver(oa.ProtoOAErrorRes(errorCode="INVALID_REQUEST", description="SECRET"))
        state = json.dumps(self.engine.snapshot())
        self.assertIn("INVALID_REQUEST", state)
        self.assertNotIn("SECRET", state)
        self.assertNotIn("accessToken", state)
        self.engine.disconnected(None, None)
        self.assertFalse(self.engine.snapshot()["ready"])

    def test_http_liveness_readiness_and_not_found(self):
        dashboard = Dashboard(self.engine)
        for path, code in ((b"/healthz", 200), (b"/readyz", 503), (b"/api/state", 200), (b"/", 200), (b"/nope", 404)):
            request = DummyRequest([])
            request.path = path
            dashboard.render_GET(request)
            self.assertEqual(request.responseCode or 200, code)

    def test_constant_prices_are_finite(self):
        prices = [2650.] * 256
        self.assertEqual(get_hurst(prices), .5)
        self.assertEqual(estimate_ou_process(prices), 0)
        self.assertEqual(advanced_denoise(prices), 2650.)

    def test_timeout_reconnects_instead_of_waiting_forever(self):
        protocol = Mock()
        client = Mock(isConnected=True)
        client.whenConnected.return_value = succeed(protocol)
        self.engine.client = client
        self.assertIsNone(self.engine.request_failed(Failure(TimeoutError("secret must not be logged"))))
        protocol.transport.loseConnection.assert_called_once()
        self.assertNotIn("secret", json.dumps(self.engine.snapshot()))


if __name__ == "__main__":
    unittest.main()
