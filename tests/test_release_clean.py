"""发版洁净度与全新客户的十四天试用边界回归。"""
import copy
import hashlib
import io
import importlib.util
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core.config_manager import DEFAULT_CONFIG
from core.licensing import gate_block, license_status, make_key_asymmetric, parse_key
from scripts.check_release_clean import check_release, forbidden_reason, main


class ReleaseTrialTests(unittest.TestCase):
    def test_defaults_are_unactivated_fourteen_day_enforced_trial(self):
        license_config = DEFAULT_CONFIG['license']
        self.assertEqual(license_config['key'], '')
        self.assertEqual(license_config['trial_started'], '')
        self.assertEqual(license_config['trial_days'], 14)
        self.assertIs(license_config['enforce'], True)
        self.assertEqual(DEFAULT_CONFIG['api']['api_key'], '')
        self.assertEqual(DEFAULT_CONFIG['api']['upload_api_key'], '')
        self.assertEqual(DEFAULT_CONFIG['history'], [])

    def test_new_customer_expires_on_day_fifteen_and_yl2_unlocks(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        start = date(2026, 10, 5)
        with patch('core.licensing.sys.frozen', True, create=True):
            self.assertEqual(license_status(config, today=start)['state'], 'trial')
            self.assertEqual(config['license']['trial_started'], start.isoformat())
            self.assertIsNone(gate_block(config, today=start))
            last_day = start + timedelta(days=13)
            self.assertEqual(license_status(config, today=last_day)['state'], 'trial')
            self.assertIsNone(gate_block(config, today=last_day))
            expired_day = start + timedelta(days=14)
            self.assertEqual(license_status(config, today=expired_day)['state'], 'expired')
            self.assertIn('试用已结束', gate_block(config, today=expired_day))
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            private_pem = private_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption())
            public_pem = private_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode('ascii')
            with patch('core.licensing._VERIFY_PUBLIC_PEM', public_pem):
                config['license']['key'] = make_key_asymmetric('回归测试客户', private_key_pem=private_pem)
                self.assertTrue(config['license']['key'].startswith('YL2.'))
                self.assertEqual(parse_key(config['license']['key'])['customer'], '回归测试客户')
                self.assertEqual(license_status(config, today=expired_day)['state'], 'active')
                self.assertIsNone(gate_block(config, today=expired_day))


class ReleaseCleanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / 'YanlinMatrix'
        self.bundle.mkdir()
        (self.bundle / 'YanlinMatrix.exe').write_bytes(b'regression fixture')
        self.zip_path = self.root / 'portable.zip'

    def make_zip(self):
        with zipfile.ZipFile(self.zip_path, 'w') as archive:
            for path in self.bundle.rglob('*'):
                if path.is_file():
                    archive.write(path, path.relative_to(self.bundle.parent).as_posix())

    def test_clean_package_allows_only_exact_certifi_certificate(self):
        certificate = self.bundle / '_internal' / 'certifi' / 'cacert.pem'
        certificate.parent.mkdir(parents=True)
        certificate.write_bytes(b'certificate fixture')
        self.make_zip()
        result = check_release(self.bundle, self.zip_path)
        self.assertEqual(result['hits'], [])
        self.assertEqual(result['bundle_count'], 2)
        self.assertEqual(result['zip_count'], 2)
        self.assertEqual(result['whitelist_count'], 2)
        self.assertEqual(result['sha256'], hashlib.sha256(self.zip_path.read_bytes()).hexdigest())
        self.assertEqual(result['zip_path'], str(self.zip_path.resolve()))
        for name in ('cacert.pem', '_internal/other/cacert.pem', '_internal/certifi/private.pem'):
            with self.subTest(name=name):
                self.assertIsNotNone(forbidden_reason(name))

    def test_forbidden_files_are_detected_in_directory_and_zip(self):
        paths = (
            'config.json', 'CONFIG.JSON.bak', 'models_cache.json.tmp',
            'history.sqlite3', 'history.sqlite3-wal', 'secret.key', 'public.pem',
            '.cluster/local.txt', 'license-ledger.csv', 'video.MP4', 'video.mov',
            'video.avi', 'logs/local.txt', 'run.log', 'screenshots/frame.png',
            'screenshot_1.png', 'codex-clipboard-local.png', 'screenshots.zip',
            'yanlin-diagnostics.zip', 'upload-auth-diagnostics.json', '诊断包.zip',
        )
        for name in paths:
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'local fixture')
        self.make_zip()
        hits = check_release(self.bundle, self.zip_path)['hits']
        for name in paths:
            with self.subTest(name=name):
                self.assertTrue(any(hit.startswith(f'目录/{name}：') for hit in hits))
                self.assertTrue(any(hit.startswith(f'ZIP/YanlinMatrix/{name}：') for hit in hits))

    def test_zip_only_contamination_and_empty_private_directories_are_detected(self):
        (self.bundle / '.cluster').mkdir()
        self.make_zip()
        with zipfile.ZipFile(self.zip_path, 'a') as archive:
            archive.writestr('YanlinMatrix/config.json', '{}')
        hits = check_release(self.bundle, self.zip_path)['hits']
        self.assertTrue(any(hit.startswith('目录/.cluster：') for hit in hits))
        self.assertTrue(any(hit.startswith('ZIP/YanlinMatrix/config.json：') for hit in hits))

    def test_zip_traversal_and_duplicate_case_insensitive_names_are_rejected(self):
        self.make_zip()
        with zipfile.ZipFile(self.zip_path, 'a') as archive:
            archive.writestr('YanlinMatrix/../outside.txt', 'fixture')
            archive.writestr('YanlinMatrix/YANLINMATRIX.EXE', 'fixture')
        hits = check_release(self.bundle, self.zip_path)['hits']
        self.assertTrue(any('不安全' in hit for hit in hits))
        self.assertTrue(any('重复' in hit for hit in hits))

    def test_cli_exits_nonzero_for_contamination_missing_or_invalid_zip(self):
        (self.bundle / 'config.json').write_text('{}', encoding='utf-8')
        self.make_zip()
        args = ['--bundle', str(self.bundle), '--zip', str(self.zip_path)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 1)
            self.zip_path.write_bytes(b'invalid zip')
            self.assertEqual(main(args), 1)
            self.zip_path.unlink()
            self.assertEqual(main(args), 1)

    def test_build_stops_before_manifest_when_clean_check_fails(self):
        module_path = Path(__file__).resolve().parent.parent / 'packaging' / 'build_release.py'
        spec = importlib.util.spec_from_file_location('yanlin_build_release_test', module_path)
        build_release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build_release)
        self.make_zip()
        with patch.object(build_release, 'DIST', self.root), \
                patch.object(build_release, 'maybe_sign'), \
                patch.object(build_release, 'make_zip', return_value=self.zip_path), \
                patch.object(build_release, 'make_manifest') as manifest, \
                patch.object(build_release.subprocess, 'check_call') as command, \
                patch('sys.argv', ['build_release.py', '--no-build']):
            import subprocess
            command.side_effect = [0, subprocess.CalledProcessError(1, 'check_release_clean')]
            with self.assertRaises(subprocess.CalledProcessError):
                build_release.main()
            manifest.assert_not_called()


if __name__ == '__main__':
    unittest.main()
