"""High-DPI startup, logical screen metrics and per-monitor signal regressions."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QObject, QRect, QSize, pyqtSignal
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from ui.scaling import screen_factors

ROOT = Path(__file__).resolve().parent.parent


class FakeScreen(QObject):
    logicalDotsPerInchChanged = pyqtSignal(float)

    def __init__(self, dpi=96, width=1920, height=1080, available_height=1040):
        super().__init__()
        self.dpi = dpi
        self.screen_size = QSize(width, height)
        self.available = QRect(0, 0, width, available_height)

    def name(self):
        return 'screen-fixture'

    def logicalDotsPerInch(self):
        return self.dpi

    def size(self):
        return self.screen_size

    def availableGeometry(self):
        return self.available


class ScreenFactorsTests(unittest.TestCase):
    def test_96dpi_1080p(self):
        self.assertEqual(screen_factors(FakeScreen()), {
            'dpi': 1.0, 'logical_width': 1920, 'logical_height': 1080,
            'available_width': 1920, 'available_height': 1040,
        })

    def test_192dpi_4k_already_has_logical_1080p_dimensions(self):
        factors = screen_factors(FakeScreen(dpi=192))
        self.assertEqual(factors['dpi'], 2.0)
        self.assertEqual((factors['logical_width'], factors['logical_height']), (1920, 1080))
        self.assertEqual(factors['available_height'], 1040)

    def test_96dpi_unscaled_4k(self):
        self.assertEqual(screen_factors(FakeScreen(96, 3840, 2160, 2080)), {
            'dpi': 1.0, 'logical_width': 3840, 'logical_height': 2160,
            'available_width': 3840, 'available_height': 2080,
        })

    def test_fractional_dpi_and_offset_work_area_remain_logical(self):
        screen = FakeScreen(144, 2560, 1440, 1380)
        screen.available = QRect(-2560, 40, 2520, 1380)
        factors = screen_factors(screen)
        self.assertEqual(factors['dpi'], 1.5)
        self.assertEqual(factors['logical_width'], 2560)
        self.assertEqual(factors['available_width'], 2520)
        self.assertEqual(factors['available_height'], 1380)


class HighDpiStartupTests(unittest.TestCase):
    def test_entrypoint_sets_attributes_and_policy_before_constructing_application(self):
        probe = textwrap.dedent('''
            import sys
            from pathlib import Path
            from unittest.mock import Mock, patch
            from PyQt5.QtCore import Qt
            from PyQt5.QtWidgets import QApplication
            import main

            class StartupChecked(Exception):
                pass

            class ProbeApplication(QApplication):
                def __init__(self, args):
                    assert QApplication.instance() is None
                    assert QApplication.testAttribute(Qt.AA_EnableHighDpiScaling)
                    assert QApplication.testAttribute(Qt.AA_UseHighDpiPixmaps)
                    expected = Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
                    assert QApplication.highDpiScaleFactorRoundingPolicy() == expected
                    super().__init__(args)
                    assert abs(self.primaryScreen().devicePixelRatio() - float(sys.argv[1])) < 0.01
                    print('high-dpi-startup-ok')
                    raise StartupChecked

            manager = Mock(path=Path(sys.argv[2]) / 'config.json')
            with patch.object(main, 'QApplication', ProbeApplication), \\
                    patch.object(main, 'ConfigManager', return_value=manager), \\
                    patch.object(main, 'install_default'):
                try:
                    main.main()
                except StartupChecked:
                    pass
                else:
                    raise AssertionError('entrypoint did not construct QApplication')
        ''')
        with tempfile.TemporaryDirectory() as folder:
            for scale in ('1', '1.25', '1.5'):
                with self.subTest(scale=scale):
                    environment = dict(os.environ)
                    for key in ('QT_SCREEN_SCALE_FACTORS', 'QT_FONT_DPI', 'QT_SCALE_FACTOR_ROUNDING_POLICY'):
                        environment.pop(key, None)
                    environment.update(QT_QPA_PLATFORM='offscreen', QT_SCALE_FACTOR=scale, QT_FONT_DPI='96')
                    result = subprocess.run(
                        [sys.executable, '-X', 'utf8', '-c', probe, scale, folder],
                        cwd=ROOT, env=environment, capture_output=True, text=True, encoding='utf-8', timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('high-dpi-startup-ok', result.stdout)

    def test_manifest_declares_per_monitor_v2_and_legacy_fallback(self):
        import xml.etree.ElementTree as ET
        manifest = ET.parse(ROOT / 'packaging' / 'app.manifest')
        namespaces = {'asm': 'urn:schemas-microsoft-com:asm.v3'}
        settings = manifest.find('asm:application/asm:windowsSettings', namespaces)
        self.assertIsNotNone(settings)
        self.assertEqual(
            settings.find('{http://schemas.microsoft.com/SMI/2016/WindowsSettings}dpiAwareness').text,
            'PerMonitorV2,PerMonitor,System')
        self.assertEqual(
            settings.find('{http://schemas.microsoft.com/SMI/2005/WindowsSettings}dpiAware').text,
            'True/PM')

    def test_custom_manifest_preserves_windows_version_detection_for_backdrop_selection(self):
        import xml.etree.ElementTree as ET
        manifest = ET.parse(ROOT / 'packaging' / 'app.manifest')
        namespaces = {'compat': 'urn:schemas-microsoft-com:compatibility.v1'}
        supported = manifest.findall('compat:compatibility/compat:application/compat:supportedOS', namespaces)
        self.assertIn('{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}',
                      {entry.get('Id') for entry in supported})

    def test_pyinstaller_receives_custom_manifest_without_running_build(self):
        specification = importlib.util.spec_from_file_location('dpi_build_fixture', ROOT / 'packaging' / 'build_release.py')
        build = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(build)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            bundle = target / 'dist' / build.EXE_NAME

            def fake_build(args):
                bundle.mkdir(parents=True)
                (bundle / f'{build.EXE_NAME}.exe').write_bytes(b'fixture')

            with patch.object(build, 'BUILD', target / 'build'), \
                    patch.object(build, 'DIST', target / 'dist'), \
                    patch('PyInstaller.__main__.run', side_effect=fake_build) as run:
                self.assertEqual(build.build_bundle(), bundle)
                args = run.call_args.args[0]
                self.assertEqual(args[args.index('--manifest') + 1], str(ROOT / 'packaging' / 'app.manifest'))
                self.assertEqual(args[args.index('--icon') + 1], str(ROOT / 'assets' / 'app.ico'))
                self.assertEqual(args[args.index('--add-data') + 1], f'{ROOT / "assets"}{os.pathsep}assets')


class ScreenSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        if sys.platform == 'win32':
            native_position = patch('qframelesswindow.windows.win32gui.SetWindowPos')
            native_position.start()
            self.addCleanup(native_position.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.window = MainWindow(ConfigManager(Path(self.temporary.name) / 'config.json'), network_time=False)
        self.window.show()
        QTest.qWait(100)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(40)
        self.temporary.cleanup()

    def test_native_window_screen_changed_signal_is_connected_once(self):
        handle = self.window.windowHandle()
        self.assertIs(self.window._dpi_window_handle, handle)
        self.assertIs(self.window._dpi_screen, handle.screen())
        self.window.hide()
        self.window.show()
        self.window._connect_screen_signals()
        with patch.object(self.window.workspace_page, 'append_log') as log:
            handle.screenChanged.emit(handle.screen())
            log.assert_called_once()
            self.assertIn('screen-changed', log.call_args.args[0])
            self.assertEqual(log.call_args.args[1], 'info')

    def test_switching_screens_disconnects_old_dpi_and_does_not_duplicate_new_signal(self):
        previous = FakeScreen()
        current = FakeScreen(192)
        self.window._on_screen_changed(previous)
        self.window._on_screen_changed(current)
        self.window._on_screen_changed(current)
        geometry = self.window.geometry()
        with patch.object(self.window.workspace_page, 'append_log') as log:
            previous.logicalDotsPerInchChanged.emit(144)
            log.assert_not_called()
            current.logicalDotsPerInchChanged.emit(192)
            log.assert_called_once()
            self.assertIn('logical-dpi-changed', log.call_args.args[0])
            self.assertIn('dpi=2.000', log.call_args.args[0])
        self.assertEqual(self.window.geometry(), geometry)

    def test_close_disconnects_native_window_and_current_screen_signals(self):
        handle = self.window.windowHandle()
        screen = FakeScreen()
        self.window._on_screen_changed(screen)
        self.window.close()
        self.assertIsNone(self.window._dpi_screen)
        self.assertIsNone(self.window._dpi_window_handle)
        with patch.object(self.window.workspace_page, 'append_log') as log:
            screen.logicalDotsPerInchChanged.emit(144)
            handle.screenChanged.emit(handle.screen())
            log.assert_not_called()

    def test_no_screen_temporarily_disconnects_dpi_observation(self):
        screen = FakeScreen()
        self.window._on_screen_changed(screen)
        self.window._on_screen_changed(None)
        self.assertIsNone(self.window._dpi_screen)
        with patch.object(self.window.workspace_page, 'append_log') as log:
            screen.logicalDotsPerInchChanged.emit(144)
            log.assert_not_called()
        self.window._on_screen_changed(self.window.windowHandle().screen())
        self.assertIsNotNone(self.window._dpi_screen)


if __name__ == '__main__':
    unittest.main()
