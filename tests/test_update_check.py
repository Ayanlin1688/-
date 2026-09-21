"""更新检查：版本比较、清单解析与容错（全部离线注入，不发起真实请求）。"""
import unittest

from core.update_check import check_for_update, is_newer
from core.version import APP_VERSION


class UpdateCheckTests(unittest.TestCase):
    def test_version_compare(self):
        self.assertTrue(is_newer('99.0.0'))
        self.assertTrue(is_newer('3.10.0'))
        self.assertFalse(is_newer(APP_VERSION))
        self.assertFalse(is_newer('0.0.1'))
        self.assertFalse(is_newer(''))

    def test_newer_manifest_returned(self):
        def fetch(url):
            return {'version': '9.9.9', 'notes': '大版本', 'assets': [{'download_url': 'https://example.com/setup.exe'}]}
        result = check_for_update('https://example.com/manifest.json', fetch=fetch)
        self.assertEqual(result['version'], '9.9.9')
        self.assertIn('setup.exe', result['url'])
        self.assertEqual(result['notes'], '大版本')

    def test_same_version_and_errors_return_none(self):
        self.assertIsNone(check_for_update('https://x', fetch=lambda url: {'version': APP_VERSION}))
        self.assertIsNone(check_for_update('https://x', fetch=lambda url: {'version': '0.0.1'}))

        def boom(url):
            raise OSError('offline')

        self.assertIsNone(check_for_update('https://x', fetch=boom))
        self.assertIsNone(check_for_update('https://x', fetch=lambda url: 'not-a-dict'))
        self.assertIsNone(check_for_update('https://x', fetch=lambda url: {'version': ''}))
        self.assertIsNone(check_for_update(''))

    def test_asset_file_fallback_url(self):
        def fetch(url):
            return {'version': '99.0.0', 'assets': [{'file': 'YanlinMatrix-v99.zip'}]}
        result = check_for_update('https://x', fetch=fetch)
        self.assertEqual(result['url'], 'YanlinMatrix-v99.zip')

    def test_asset_sha256_passthrough(self):
        def fetch(url):
            return {'version': '99.0.0', 'assets': [{'file': 'pkg.zip', 'sha256': 'abc123def456'}]}
        result = check_for_update('https://x', fetch=fetch)
        self.assertEqual(result['sha256'], 'abc123def456')
        empty = check_for_update('https://x', fetch=lambda url: {'version': '99.0.0'})
        self.assertEqual(empty['sha256'], '')


if __name__ == '__main__':
    unittest.main()
