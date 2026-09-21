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

    # —— YL2 非对称签名（2026-09 升级；静态样例由厂商私钥签发，公钥已嵌入核心代码） ——
    YL2_VALID = 'YL2.eyJjdXN0b21lciI6Iuagt-S-i-WuouaItyIsImVkaXRpb24iOiJwcm8iLCJleHBpcmVzIjoiMjAzMC0xMi0zMSJ9.qI-zJ66vFfOq7jcTTCoF7MUVg-1fPJghFr62t8c5teuqppOzYSUN8yQj_OZ3BMNMwhnOYL1TT6VaEOx7aYL4JJiDxsKMYL2RU3Tc2SL1OXo5l9IM5q7N6swK_qvVIeWbU7CCJFKTFANqqpZc9gyKUc7S2oIsOExi2mQk44p5ePD862Y5EL3_AQZKzhrhPCyPcSZp86cidevrmKAxXtX_3T3TMWZu23ejoOHEZLRn9h06DayullKwqkj55lxGzczTjkMpyCKbuuMv9GZ8ywOtrbENEJa827yUGHLczapjOBKsMOanrJhhE87t4ryigDAnFaiXADBplvZ4Wfm_992gQ2vSfIQwLERjUjGbm5BLxds3vkZPVxX48LOiZeG2DIy1Vli43pkhqXqJxzFdbaxDV_ffhQUmSFH4IfbKaBghZpeCJCDribi4gCKtxGwO-WLTucHxbM0a0DyMNh0HBtJKPFHAVZ1YG52paggZfiXwT8wYEbDE_XZakEHJU8VUFcq9'
    YL2_EXPIRED = 'YL2.eyJjdXN0b21lciI6Iuagt-S-i-WuouaItyIsImVkaXRpb24iOiJwcm8iLCJleHBpcmVzIjoiMjAyNC0wMS0wMSJ9.MMKDSvJHXuLu6_s4q4zsWTZT4HVPeiR3EAsrdjAlmHDjymcB3uAqIbhvjT-pik_d7Uat8EjoZe5qr0k2zbmPAeOK5bY8_Q0BkBlQZZaNynKjZojeI0eK-HdSkJHTq20ATGc8ZOy40B8VWjj-w6grZR23iMSOtvIInohj73nOmI6pCYVP4HL73xN0379GghnG3D5qYAVJMkrK7P7EQImJWqZhtDYre9l30hKN3iXDG2wIdVfY_AdLID1Ljk6E8HqWxHeem2gRwzG8vGCVwXpMyjsAGQ2Th9KB4Kj2efzsHEGY0ydJOMlXrbAKCI6oNHArv2ndBdxKVyj4n3wMiUlU95_PJrkBdVQ2hz7LGCvaoiEkqo7dYXuxodXykg8YXMK5Nl7TlDZUchB3cMYqa4VAh4hYRa9CNAENfpdCDB5mCk3px377WvwNazioy3Wb8yn-cN14PgH0Ec29ASdpXJwGQVpdITXrwB-oTL382FT79NSWG50oL8jOs0IrGK4RvrdI'

    def test_yl2_asymmetric_roundtrip(self):
        parsed = parse_key(self.YL2_VALID)
        self.assertEqual(parsed['customer'], '样例客户')
        self.assertEqual(parsed['expires'], '2030-12-31')

    def test_yl2_tampered_rejected(self):
        tampered = self.YL2_VALID[:-6] + 'AAAAAA'
        with self.assertRaisesRegex(ValueError, '签名不匹配'):
            parse_key(tampered)

    def test_yl2_expired_rejected(self):
        with self.assertRaisesRegex(ValueError, '到期'):
            parse_key(self.YL2_EXPIRED)

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
            key_text = make_key_asymmetric('动态客户', 'pro', '2031-01-01', private_pem)
            self.assertEqual(parse_key(key_text)['customer'], '动态客户')
        finally:
            licensing._VERIFY_PUBLIC_PEM = original


if __name__ == '__main__':
    unittest.main()
