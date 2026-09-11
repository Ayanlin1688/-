from unittest import TestCase
from unittest.mock import MagicMock, patch
from core.network_clock import NetworkClock


class NetworkClockTests(TestCase):
    def session(self, headers):
        session = MagicMock()
        response = session.head.return_value.__enter__.return_value
        response.headers = headers
        response.url = 'https://clock.example.invalid/'
        return session

    def test_https_date_calibrates_midpoint_without_implicit_auth(self):
        session = self.session({'Date': 'Thu, 01 Jan 1970 00:01:40 GMT'})
        with patch('core.network_clock.requests.Session') as factory, \
                patch('core.network_clock.time.time', side_effect=[90, 92]):
            factory.return_value.__enter__.return_value = session
            sample = NetworkClock().sample()
        self.assertEqual(sample['offset'], 9.5)
        self.assertIs(session.trust_env, False)
        clock = NetworkClock(); clock.apply(sample)
        with patch('core.network_clock.time.time', return_value=1800000000):
            self.assertEqual(clock.now().timestamp(), 1800000009.5)
        self.assertIn('网络已校时', clock.status)

    def test_stale_or_slow_network_is_rejected_and_system_fallback_is_explicit(self):
        for headers, readings in [({'Date': 'Thu, 01 Jan 1970 00:01:40 GMT', 'Age': '60'}, [90, 91, 92, 93]),
                                  ({'Date': 'Thu, 01 Jan 1970 00:01:40 GMT'}, [90, 97, 100, 107])]:
            session = self.session(headers)
            with patch('core.network_clock.requests.Session') as factory, \
                    patch('core.network_clock.time.time', side_effect=readings):
                factory.return_value.__enter__.return_value = session
                with self.assertRaisesRegex(RuntimeError, '使用系统时间'):
                    NetworkClock().sample()
        clock = NetworkClock(); clock.offset = 10; clock.use_system_time()
        self.assertEqual(clock.offset, 0)
        self.assertIn('使用系统时间', clock.status)
