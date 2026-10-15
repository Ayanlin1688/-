"""LIN assets, compact raster branding and English window identity regressions."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
from PyQt5.QtCore import QCoreApplication, QEvent, Qt
from PyQt5.QtGui import QFontMetrics, QIcon, QPixmap
from PyQt5.QtWidgets import QApplication

from core.version import APP_NAME
from ui import tokens
from ui.assets import asset_path
from ui.components.brand_widgets import BrandLogo, UserAvatar, logo_pixmap


ROOT = Path(__file__).resolve().parent.parent


class BrandAssetTests(unittest.TestCase):
    def test_ico_contains_every_required_frame_with_transparent_corners(self):
        with Image.open(asset_path('app.ico')) as icon:
            self.assertEqual(icon.ico.sizes(), {(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 128, 256)})
            for size in icon.ico.sizes():
                with self.subTest(size=size):
                    frame = icon.ico.getimage(size).convert('RGBA')
                    for point in ((0, 0), (size[0] - 1, 0), (0, size[1] - 1), (size[0] - 1, size[1] - 1)):
                        self.assertEqual(frame.getpixel(point)[3], 0)

    def test_256_frame_is_png_encoded(self):
        with Image.open(asset_path('app.ico')) as icon:
            entry = next(entry for entry in icon.ico.entry if entry.dim == (256, 256))
            icon.fp.seek(entry.offset)
            self.assertEqual(icon.fp.read(8), b'\x89PNG\r\n\x1a\n')

    def test_small_frames_are_green_without_pink_rabbit_or_blue_purple_brand(self):
        with Image.open(asset_path('app.ico')) as icon:
            for size in (16, 20, 24, 32, 40):
                pixels = [pixel for pixel in icon.ico.getimage((size, size)).convert('RGBA').getdata() if pixel[3] > 200]
                green = sum(green > red * 1.2 and green > blue * 1.2 for red, green, blue, alpha in pixels)
                pink_or_purple = sum(red > green * 1.2 or blue > green * 1.2 for red, green, blue, alpha in pixels)
                with self.subTest(size=size):
                    self.assertGreater(green / len(pixels), 0.45)
                    self.assertEqual(pink_or_purple, 0)

    def test_png_assets_load_at_expected_sizes_with_transparency(self):
        for name, size in (('app.png', 256), ('app-small.png', 256), ('app-512.png', 512)):
            with self.subTest(name=name), Image.open(asset_path(name)) as image:
                self.assertEqual(image.size, (size, size))
                self.assertEqual(image.mode, 'RGBA')
                self.assertEqual(image.getpixel((0, 0))[3], 0)
                self.assertEqual(image.getpixel((size // 2, size // 2))[3], 255)

    def test_tracked_brand_source_preserves_original_resolution(self):
        with Image.open(asset_path('source/icon-lin.png')) as image:
            self.assertEqual(image.size, (2880, 2880))

    def test_resource_path_supports_source_and_pyinstaller_internal_root(self):
        self.assertEqual(asset_path('app.ico'), ROOT / 'assets' / 'app.ico')
        with tempfile.TemporaryDirectory() as folder, patch.object(sys, '_MEIPASS', folder, create=True):
            self.assertEqual(asset_path('app.ico'), Path(folder) / 'assets' / 'app.ico')
            import main
            self.assertEqual(main._asset_path('app.ico'), asset_path('app.ico'))

    def test_installer_and_exe_product_names_keep_english_brand_and_compact_filename(self):
        specification = importlib.util.spec_from_file_location('brand_build_fixture', ROOT / 'packaging/build_release.py')
        build = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(build)
        with tempfile.TemporaryDirectory() as folder:
            version_info = build._write_version_info(Path(folder) / 'version.txt').read_text(encoding='utf-8')
        self.assertIn(f"StringStruct('ProductName', '{APP_NAME}')", version_info)
        self.assertIn("StringStruct('OriginalFilename', 'YanlinMatrix.exe')", version_info)
        installer = (ROOT / 'packaging/installer.iss').read_text(encoding='utf-8')
        self.assertIn('SetupIconFile=..\\assets\\app.ico', installer)
        self.assertIn('UninstallDisplayIcon={app}\\_internal\\assets\\app.ico', installer)
        self.assertIn(f'#define AppName "{APP_NAME}"', installer)


class BrandWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.scale = tokens.interface_scale()
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        tokens.set_interface_scale(self.scale)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_logo_pixmap_uses_full_and_compact_raster_assets(self):
        for size, compact, name in ((64, False, 'app.png'), (72, False, 'app-512.png'), (34, True, 'app-small.png')):
            with self.subTest(size=size, compact=compact):
                rendered = logo_pixmap(size, compact=compact)
                expected = QPixmap(str(asset_path(name))).scaled(size * 4, size * 4, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                expected.setDevicePixelRatio(4)
                self.assertFalse(rendered.isNull())
                self.assertEqual(rendered.toImage(), expected.toImage())
                self.assertEqual(rendered.width() / rendered.devicePixelRatioF(), size)

    def test_expanded_brand_paints_full_english_line_with_normal_scaled_font(self):
        brand = BrandLogo()
        self.widgets.append(brand)
        self.assertEqual(brand.title_text, APP_NAME)
        self.assertEqual(brand.accessibleName(), APP_NAME)
        self.assertEqual(brand.toolTip(), APP_NAME)
        for scale in (1.0, 1.1, 1.25, 1.3, 1.4, 1.5, 1.6):
            tokens.set_interface_scale(scale)
            brand.set_expanded(True)
            painter = Mock()
            with patch('ui.components.brand_widgets.QPainter', return_value=painter), \
                    patch('ui.components.brand_widgets.logo_pixmap', return_value=QPixmap(1, 1)) as raster:
                brand.paintEvent(None)
            raster.assert_called_once_with(tokens.px(34), compact=False)
            self.assertEqual(painter.drawText.call_count, 1)
            text_rect, flags, text = painter.drawText.call_args.args
            self.assertNotIn('\n', text)
            self.assertEqual(text, APP_NAME)
            self.assertEqual(flags, Qt.AlignLeft | Qt.AlignVCenter)
            self.assertEqual(text_rect.height(), tokens.px(34))
            font = painter.setFont.call_args.args[0]
            self.assertEqual(font.pixelSize(), tokens.px(14))
            self.assertFalse(font.bold())
            self.assertGreaterEqual(text_rect.width(), QFontMetrics(font).horizontalAdvance(APP_NAME))
            self.assertTrue(brand.rect().contains(text_rect.toRect()))

    def test_collapsed_brand_uses_simplified_tile_without_title(self):
        brand = BrandLogo()
        self.widgets.append(brand)
        painter = Mock()
        with patch('ui.components.brand_widgets.QPainter', return_value=painter), \
                patch('ui.components.brand_widgets.logo_pixmap', return_value=QPixmap(1, 1)) as raster:
            brand.paintEvent(None)
        raster.assert_called_once_with(tokens.px(34), compact=True)
        painter.drawText.assert_not_called()
        painter.drawRoundedRect.assert_not_called()

    def test_user_avatar_keeps_existing_user_monogram(self):
        avatar = UserAvatar()
        self.widgets.append(avatar)
        painter = Mock()
        with patch('ui.components.brand_widgets.QPainter', return_value=painter):
            avatar.paintEvent(None)
        self.assertEqual(painter.drawText.call_args.args[-1], 'Y')

    def test_splash_uses_green_icon_and_english_window_title(self):
        from ui.splash import show_splash
        splash = show_splash()
        self.assertIsNotNone(splash)
        self.widgets.append(splash)
        self.assertEqual(splash.windowTitle(), APP_NAME)
        self.assertFalse(splash.windowIcon().isNull())
        image = splash.pixmap().toImage()
        green_pixel = image.pixelColor(180, 55)
        self.assertGreater(green_pixel.green(), green_pixel.blue())
        self.assertGreater(green_pixel.green(), green_pixel.red())

    def test_startup_sets_english_display_name_and_new_application_icon(self):
        import main
        application = Mock()
        application.exec_.return_value = 0
        guard = Mock()
        guard.acquire.return_value = True
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(main, 'QApplication', return_value=application), \
                patch.object(main, 'ConfigManager', return_value=Mock(path=Path(folder) / 'config.json')), \
                patch.object(main, 'install_default'), patch.object(main, 'ensure_ui_font'), \
                patch.object(main, 'setTheme'), patch('ui.main_window.MainWindow'), \
                patch('ui.splash.show_splash', return_value=None), \
                patch('core.single_instance.SingleInstance', return_value=guard):
            self.assertEqual(main.main(), 0)
        application.setApplicationName.assert_called_once_with('YanlinMatrix')
        application.setApplicationDisplayName.assert_called_once_with(APP_NAME)
        icon = application.setWindowIcon.call_args.args[0]
        self.assertIsInstance(icon, QIcon)
        self.assertFalse(icon.isNull())
