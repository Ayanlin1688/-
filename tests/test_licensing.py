"""许可最小版：激活码往返、防篡改、到期、试用状态与闸门。"""
import copy
import unittest
from datetime import date, timedelta

from core.config_manager import DEFAULT_CONFIG
from core.licensing import gate_block, license_status, make_key, parse_key


class LicensingTests(unittest.TestCase):
    def test_key_roundtrip(self):
        key = make_key('测试客户', 'pro', '')
        parsed = parse_key(key)
        self.assertEqual(parsed['customer'], '测试客户')
        self.assertEqual(parsed['edition'], 'pro')
        self.assertEqual(parsed['expires'], '')
        # 带空格/换行也能识别
        self.assertEqual(parse_key('  ' + key + '\n')['customer'], '测试客户')

    def test_tampered_and_malformed_keys_rejected(self):
        key = make_key('客户A')
        payload, signature = key.split('.')[1], key.split('.')[2]
        tampered = 'YL1.' + payload[:-2] + 'AA.' + signature
        with self.assertRaisesRegex(ValueError, '签名不匹配'):
            parse_key(tampered)
        with self.assertRaisesRegex(ValueError, '格式不正确'):
            parse_key('not-a-key')
        with self.assertRaisesRegex(ValueError, '格式不正确'):
            parse_key('YL1.abc')
        with self.assertRaisesRegex(ValueError, '格式不正确'):
            parse_key('')

    def test_expired_key_rejected(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, '到期'):
            parse_key(make_key('客户B', expires=yesterday))
        future = (date.today() + timedelta(days=30)).isoformat()
        self.assertEqual(parse_key(make_key('客户B', expires=future))['expires'], future)

    def test_trial_status_starts_and_counts_down(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        start = date(2026, 1, 1)
        status = license_status(config, today=start)
        self.assertEqual(status['state'], 'trial')
        self.assertEqual(config['license']['trial_started'], '2026-01-01')
        self.assertEqual(status['days_left'], 14)
        later = license_status(config, today=date(2026, 1, 10))
        self.assertEqual(later['days_left'], 5)
        expired = license_status(config, today=date(2026, 1, 20))
        self.assertEqual(expired['state'], 'expired')

    def test_active_key_wins_over_expired_trial(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['license'].update(key=make_key('客户C'), trial_started='2020-01-01')
        status = license_status(config, today=date(2026, 6, 1))
        self.assertEqual(status['state'], 'active')
        self.assertIn('客户C', status['detail'])

    def test_invalid_key_reported(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['license']['key'] = 'YL1.broken.key'
        status = license_status(config)
        self.assertEqual(status['state'], 'invalid')

    def test_gate_only_blocks_when_enforced_and_expired(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['license']['trial_started'] = '2020-01-01'
        today = date(2026, 6, 1)
        self.assertIsNone(gate_block(config, today=today))  # 演示模式不拦
        config['license']['enforce'] = True
        self.assertIn('试用已结束', gate_block(config, today=today))
        config['license']['trial_started'] = today.isoformat()
        self.assertIsNone(gate_block(config, today=today))  # 试用期内放行
        config['license']['key'] = make_key('客户D')
        config['license']['trial_started'] = '2020-01-01'
        self.assertIsNone(gate_block(config, today=today))  # 已激活放行

    # —— YL2 非对称签名（2026-10 密钥轮换；测试样例运行时生成） ——
    def test_yl2_dynamic_keypair(self):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            self.skipTest('cryptography 不可用')
        import core.licensing as licensing
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = key.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
        public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        original = licensing._VERIFY_PUBLIC_PEM
        licensing._VERIFY_PUBLIC_PEM = public_pem
        try:
            from core.licensing import make_key_asymmetric
            valid = make_key_asymmetric('动态客户', 'pro', '2031-01-01', private_pem)
            parsed = parse_key(valid)
            self.assertEqual(parsed['customer'], '动态客户')
            self.assertEqual(parsed['edition'], 'pro')
            self.assertEqual(parsed['expires'], '2031-01-01')

            expired = make_key_asymmetric(
                '动态客户', 'pro', (date.today() - timedelta(days=1)).isoformat(), private_pem)
            with self.assertRaisesRegex(ValueError, '到期'):
                parse_key(expired)

            signature = valid.rsplit('.', 1)[1]
            replacement = 'A' if signature[0] != 'A' else 'B'
            tampered = valid.rsplit('.', 1)[0] + '.' + replacement + signature[1:]
            with self.assertRaisesRegex(ValueError, '签名不匹配'):
                parse_key(tampered)
        finally:
            licensing._VERIFY_PUBLIC_PEM = original


if __name__ == '__main__':
    unittest.main()
