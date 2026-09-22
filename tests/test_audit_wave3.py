"""波次三回归护栏：YL1 发行门控（冻结版仅 YL2）与单实例守卫。"""
import os
import sys
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class LegacyKeyGatingTests(unittest.TestCase):
    """源码/测试环境保留 YL1 演示格式；冻结发行版只接受 YL2。"""

    def test_source_run_accepts_hmac_demo_keys(self):
        from core.licensing import make_key, parse_key
        key = make_key('演示客户')
        self.assertEqual(parse_key(key)['customer'], '演示客户')

    def test_frozen_build_rejects_hmac_demo_keys(self):
        from core.licensing import make_key, parse_key
        key = make_key('演示客户')
        with patch.object(sys, 'frozen', True, create=True):
            with self.assertRaises(ValueError) as ctx:
                parse_key(key)
            self.assertIn('旧版演示格式', str(ctx.exception))

    def test_frozen_build_still_accepts_yl2_keys(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from core import licensing
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        public_pem = private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        key = licensing.make_key_asymmetric('YL2客户', private_key_pem=pem)
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(licensing, '_VERIFY_PUBLIC_PEM', public_pem):
            data = licensing.parse_key(key)
        self.assertEqual(data['customer'], 'YL2客户')


class SingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_second_instance_detected_and_activation_requested(self):
        from PyQt5.QtTest import QTest
        from core.single_instance import SingleInstance
        key = f'YanlinMatrix-test-{os.getpid()}'
        first = SingleInstance(key)
        second = SingleInstance(key)
        fired = []
        first.activation_requested.connect(lambda: fired.append(1))
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            deadline = time.monotonic() + 3
            while not fired and time.monotonic() < deadline:
                QTest.qWait(20)
            self.assertTrue(fired, '未触发“请求前置窗口”信号')
        finally:
            second.close()
            first.close()

    def test_different_keys_can_coexist(self):
        from core.single_instance import SingleInstance
        first = SingleInstance(f'YanlinMatrix-testA-{os.getpid()}')
        second = SingleInstance(f'YanlinMatrix-testB-{os.getpid()}')
        try:
            self.assertTrue(first.acquire())
            self.assertTrue(second.acquire())
        finally:
            first.close()
            second.close()

    def test_key_for_stable_and_distinct(self):
        from core.single_instance import SingleInstance
        key_a = SingleInstance.key_for('C:/data/a')
        key_a_again = SingleInstance.key_for('C:/data/a')
        key_b = SingleInstance.key_for('C:/data/b')
        self.assertEqual(key_a, key_a_again)
        self.assertNotEqual(key_a, key_b)


if __name__ == '__main__':
    unittest.main()
