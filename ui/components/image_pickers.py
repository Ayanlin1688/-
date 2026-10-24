"""电商图上传区的三个图片选择器：已用图片 / 本地画廊 / 输入 URL。

参考软件依赖后端 /assets 与 /gallery，本仓库没有后端，落地为：
- 已用图片：从电商图数据库读取已完成任务生成图（succeeded 的 file_path），供复用；
- 画廊选图：本地目录 `~/Pictures/YanlinGallery`（首次使用由用户选择并持久化到
  config `ecommerce.gallery_dir`），缩略图网格多选；
- 输入 URL：多行文本，每行一个 http(s) 链接或本地绝对路径；URL 下载到配置目录旁
  `image_cache/`，网络失败按中文分类短句报错，不抛外文堆栈。

所有对话框只做选择与下载，路径合并/上限校验由页面层负责，便于 offscreen 测试。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import PrimaryPushButton, PushButton

from core.api_errors import classify_connection_error
from core.i18n import tr
from .. import materials
from ..responsive import WrapLayout
from ..theme import style_controls
from ..tokens import px
from .studio_dialog import StudioDialog

IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.webp', '.bmp')
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
DEFAULT_TIMEOUT = (6, 20)


# ---------------------------------------------------------------------------
# 纯函数（无 Qt 依赖，便于单测）
# ---------------------------------------------------------------------------
def is_image_file(path) -> bool:
    try:
        suffix = Path(str(path)).suffix.lower()
    except (OSError, ValueError):
        return False
    return suffix in IMAGE_SUFFIXES


def parse_input_lines(text) -> list[str]:
    """把多行文本拆成去空白的条目列表（忽略空行）。"""
    entries = []
    for line in str(text or '').splitlines():
        value = line.strip()
        if value:
            entries.append(value)
    return entries


def image_cache_dir(config_manager) -> Path:
    base = getattr(config_manager, 'path', None)
    if base is None:
        base = Path.home() / '.yanlin' / 'config.json'
    target = Path(base).parent / 'image_cache'
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(base).parent
    return target


def gallery_directory(config_manager) -> Path | None:
    """配置里的画廊目录；未配置返回 None（页面提示用户先选择）。"""
    config = getattr(config_manager, 'config', {}) or {}
    value = str((config.get('ecommerce') or {}).get('gallery_dir') or '').strip()
    return Path(value).expanduser() if value else None


def default_gallery_directory() -> Path:
    home = Path.home()
    pictures = home / 'Pictures'
    return (pictures if pictures.is_dir() else home) / 'YanlinGallery'


def scan_gallery_images(directory, *, limit=300) -> list[str]:
    """扫描画廊目录下的图片文件（一层，按文件名排序）。"""
    if not directory:
        return []
    root = Path(str(directory)).expanduser()
    try:
        if not root.is_dir():
            return []
        names = sorted(
            (entry for entry in root.iterdir()
             if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES),
            key=lambda item: item.name.lower())
    except OSError:
        return []
    return [str(path) for path in names[:max(0, int(limit))]]


def _extension_for(url, content_type='') -> str:
    suffix = Path(urlsplit(str(url)).path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return suffix
    mapping = {
        'image/png': '.png', 'image/jpeg': '.jpg', 'image/jpg': '.jpg',
        'image/webp': '.webp', 'image/bmp': '.bmp',
    }
    return mapping.get(str(content_type or '').split(';')[0].strip().lower(), '.png')


def _default_fetcher(url, timeout=DEFAULT_TIMEOUT, retry_without_verify=True):
    """默认下载器：直连优先（失败兜底关闭环境代理），SSL 失败时降级不校验证书一次。

    Returns (bytes, content_type)。异常统一抛给上层转中文短句。
    """
    import requests

    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(url, timeout=timeout,
                               headers={'User-Agent': 'YanlinMatrix/3.5'})
        response.raise_for_status()
        return response.content, response.headers.get('Content-Type', '')
    except requests.exceptions.SSLError:
        if not retry_without_verify:
            raise
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = session.get(url, timeout=timeout, verify=False,
                               headers={'User-Agent': 'YanlinMatrix/3.5'})
        response.raise_for_status()
        return response.content, response.headers.get('Content-Type', '')
    finally:
        session.close()


def resolve_input_entries(entries, cache_dir, *, fetcher=None, max_count=6):
    """把输入条目解析成本地图片路径；返回 (paths, errors)。

    - http(s) 链接：下载到 cache_dir（文件名取 URL 哈希），失败给中文短句；
    - 本地路径：必须存在且是图片后缀，否则中文提示；
    - 超过 max_count 的部分忽略并给出中文提示。
    """
    paths, errors = [], []
    fetcher = fetcher or _default_fetcher
    cache = Path(str(cache_dir))
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache = Path(cache.parent)
    seen = set()
    for entry in entries:
        if len(paths) >= max_count:
            errors.append(tr('最多选择 {count} 张图，其余已忽略').format(count=max_count))
            break
        parts = urlsplit(entry)
        if parts.scheme in ('http', 'https') and parts.netloc:
            try:
                content, content_type = fetcher(entry)
            except Exception as error:  # noqa: BLE001 - 统一转中文短句
                message = tr(classify_connection_error(error)['message'])
                errors.append(tr('图片链接下载失败：{message}').format(message=message))
                continue
            if not content:
                errors.append(tr('图片链接下载失败：返回内容为空'))
                continue
            if len(content) > MAX_DOWNLOAD_BYTES:
                errors.append(tr('图片过大（最大 {size}MB）').format(
                    size=MAX_DOWNLOAD_BYTES // (1024 * 1024)))
                continue
            digest = hashlib.sha1(entry.encode('utf-8')).hexdigest()[:20]
            suffix = _extension_for(entry, content_type)
            target = cache / (digest + suffix)
            try:
                target.write_bytes(content)
            except OSError:
                errors.append(tr('图片缓存写入失败，请检查磁盘空间'))
                continue
            resolved = str(target.resolve())
            if resolved not in seen:
                seen.add(resolved)
                paths.append(resolved)
            continue
        try:
            local = Path(entry).expanduser()
            valid = local.is_file() and local.suffix.lower() in IMAGE_SUFFIXES
            resolved = str(local.resolve()) if valid else ''
        except (OSError, ValueError):
            valid, resolved = False, ''
        if not valid:
            errors.append(tr('找不到本地图片：{name}').format(name=Path(entry).name or entry))
            continue
        if resolved not in seen:
            seen.add(resolved)
            paths.append(resolved)
    return paths, errors


# ---------------------------------------------------------------------------
# 选择器网格控件
# ---------------------------------------------------------------------------
class _PickTile(QWidget):
    """缩略图选择格：点击切换选中态（蓝框 + 右上角勾）。"""

    toggled = pyqtSignal(str, bool)

    def __init__(self, path, label='', *, size=104, parent=None):
        super().__init__(parent)
        self.path = str(path)
        self.label_text = str(label or '')
        self.selected = False
        self._size = int(size)
        self.setFixedSize(self._size, self._size + px(18))
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(self.path)
        image = QPixmap()
        try:
            if Path(self.path).is_file():
                image.loadFromData(Path(self.path).read_bytes())
        except (OSError, ValueError):
            image = QPixmap()
        self._pixmap = image
        self._caption = self._elided_caption()

    def _elided_caption(self):
        metrics = QFontMetrics(self.font())
        return metrics.elidedText(self.label_text or Path(self.path).name,
                                  Qt.ElideMiddle, self._size - px(6))

    def _image_rect(self):
        return self.rect().adjusted(0, 0, 0, -px(18))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        colors = materials.palette()
        image_rect = self._image_rect()
        painter.fillRect(image_rect, QColor(colors['bg2']))
        if self.selected:
            painter.fillRect(self.rect(), QColor(*materials.ECOMMERCE['brand_blue_soft']))
        if not self._pixmap.isNull():
            canvas = self._pixmap.scaled(image_rect.size(), Qt.KeepAspectRatioByExpanding,
                                         Qt.SmoothTransformation)
            left = max(0, (canvas.width() - image_rect.width()) // 2)
            top = max(0, (canvas.height() - image_rect.height()) // 2)
            painter.drawPixmap(image_rect.x(), image_rect.y(), canvas,
                               left, top, image_rect.width(), image_rect.height())
        else:
            painter.setPen(QColor(colors['text3']))
            painter.drawText(image_rect, Qt.AlignCenter, tr('无法预览'))
        border = (QColor(materials.ECOMMERCE['brand_blue']) if self.selected
                  else QColor(*colors['border']))
        pen = painter.pen()
        pen.setColor(border)
        pen.setWidth(2 if self.selected else 1)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(image_rect.adjusted(0, 0, -1, -1))
        painter.setPen(QColor(colors['text2']))
        caption_rect = self.rect().adjusted(px(2), image_rect.height() + px(2),
                                            -px(2), 0)
        painter.drawText(caption_rect, Qt.AlignHCenter | Qt.AlignVCenter, self._caption)
        if self.selected:
            badge = px(20)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(materials.ECOMMERCE['brand_blue']))
            painter.drawEllipse(image_rect.width() - badge - px(6), px(6), badge, badge)
            font = painter.font()
            font.setPixelSize(px(12))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor('#FFFFFF'))
            painter.drawText(image_rect.width() - badge - px(6), px(6), badge, badge,
                             Qt.AlignCenter, '✓')
        painter.end()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.selected = not self.selected
            self.update()
            self.toggled.emit(self.path, self.selected)
        super().mouseReleaseEvent(event)


class _PickerGrid(QWidget):
    """可滚动缩略图多选网格（WrapLayout）。"""

    MAX_SELECTED = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tiles = []
        self._selected = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(px(8))
        self.empty_label = QLabel(tr('暂无可选图片'))
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet('color:%s;' % materials.palette()['text3'])
        outer.addWidget(self.empty_label)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setStyleSheet('QScrollArea{background:transparent;border:0;}')
        self.scroll = scroll
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._layout = WrapLayout(spacing=px(8))
        content.setLayout(self._layout)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self.contents = content

    def set_images(self, images):
        for tile in self._tiles:
            tile.hide()
            tile.setParent(None)
            tile.deleteLater()
        self._tiles = []
        self._selected = []
        for path, label in images:
            tile = _PickTile(path, label, parent=self.contents)
            tile.toggled.connect(self._on_toggled)
            tile.show()
            self._tiles.append(tile)
            self._layout.addWidget(tile)
        self.empty_label.setVisible(not self._tiles)
        self.scroll.setVisible(bool(self._tiles))
        self.contents.updateGeometry()
        self.refresh_height()

    def refresh_height(self):
        try:
            width = self.scroll.viewport().width()
        except RuntimeError:
            width = 0
        if width <= 1:
            width = px(600)
        self.contents.setMinimumHeight(
            max(px(60), self._layout.heightForWidth(width)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_height()

    def tiles(self):
        return list(self._tiles)

    def selected_paths(self):
        return list(self._selected)

    def clear_selection(self):
        self._selected = []
        for tile in self._tiles:
            tile.selected = False
            tile.update()

    def _on_toggled(self, path, selected):
        if selected:
            if path in self._selected:
                return
            if len(self._selected) >= self.MAX_SELECTED:
                for tile in self._tiles:
                    if tile.path == path:
                        tile.selected = False
                        tile.update()
                        break
                return
            self._selected.append(path)
        else:
            try:
                self._selected.remove(path)
            except ValueError:
                pass


class _PickerDialogBase(StudioDialog):
    """选择器弹窗基类：标题 + 内容区 + 取消/确认按钮。"""

    def __init__(self, parent, title, confirm_text=None):
        super().__init__(parent, title)
        self.setModal(True)
        self.result_paths = []
        self.resize(px(720), px(560))
        self.body_layout.setSpacing(px(10))
        self.header_label = QLabel(self)
        self.header_label.setWordWrap(True)
        self.header_label.setStyleSheet('color:%s;' % materials.palette()['text2'])
        self.body_layout.addWidget(self.header_label)
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(px(8))
        self.body_layout.addWidget(self.content, 1)
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        self.error_label.setStyleSheet('color:%s;' % materials.palette()['error'])
        self.body_layout.addWidget(self.error_label)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.cancel_button = PushButton(tr('取消'), self)
        self.confirm_button = PrimaryPushButton(confirm_text or tr('确认添加'), self)
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button.clicked.connect(self._confirm)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.confirm_button)
        self.body_layout.addLayout(actions)
        style_controls(self)
        try:
            from core.i18n import register_retranslate
            register_retranslate(self)
        except Exception:
            pass

    def retranslate_ui(self):
        """语言切换：重翻静态控件与子类动态说明（幂等）。"""
        try:
            from ..i18n_helpers import retranslate_tree
            retranslate_tree(self)
        except Exception:
            pass
        self.cancel_button.setText(tr('取消'))
        self.confirm_button.setText(tr('确认添加'))
        try:
            self._retranslate_dynamic()
        except Exception:
            pass

    def _retranslate_dynamic(self):
        """子类覆写：刷新运行时生成的说明/占位文案。"""

    def _confirm(self):
        paths = self.collect()
        if not paths:
            self.show_error(tr('请选择至少一张图片'))
            return
        self.result_paths = list(paths)
        self.accept()

    def collect(self):
        return []

    def show_error(self, message):
        self.error_label.setText(str(message))
        self.error_label.setVisible(bool(message))


class UsedImagePickerDialog(_PickerDialogBase):
    """已用图片：从已完成电商图任务里挑生成结果复用。"""

    def __init__(self, parent, store, *, max_count=6, stage_text=None):
        super().__init__(parent, tr('已用图片'))
        self.store = store
        self.max_count = int(max_count)
        self.header_label.setText(tr('从已完成任务中挑选生成过的图片，最多 {count} 张').format(
            count=self.max_count))
        self.grid = _PickerGrid(self.content)
        self.grid.MAX_SELECTED = self.max_count
        self.content_layout.addWidget(self.grid, 1)
        images = self._used_images()
        self.grid.set_images(images)
        self.empty_label = self.grid.empty_label

    def _retranslate_dynamic(self):
        self.header_label.setText(tr('从已完成任务中挑选生成过的图片，最多 {count} 张').format(
            count=self.max_count))
        self.grid.set_images(self._used_images())
        self.empty_label = self.grid.empty_label

    def _used_images(self):
        images = []
        try:
            groups = self.store.list_groups(limit=50)
        except Exception:  # noqa: BLE001 - 数据库异常不应崩溃弹窗
            return images
        for group in groups:
            try:
                items = self.store.list_items(group.get('group_id', ''))
            except Exception:  # noqa: BLE001
                continue
            name = str(group.get('product_name') or '').strip()
            for item in items:
                if item.get('status') != 'succeeded':
                    continue
                path = str(item.get('file_path') or '')
                if not path:
                    continue
                try:
                    if not Path(path).is_file():
                        continue
                except (OSError, ValueError):
                    continue
                kind = tr('轮播') if item.get('kind') == 'carousel' else tr('详情')
                label = (f'{name} · {kind}{item.get("seq", "")}' if name
                         else f'{kind}{item.get("seq", "")}')
                images.append((path, label))
                if len(images) >= 120:
                    return images
        return images

    def collect(self):
        return self.grid.selected_paths()


class GalleryPickerDialog(_PickerDialogBase):
    """本地画廊：选择目录后多选。"""

    def __init__(self, parent, config_manager, *, max_count=6):
        super().__init__(parent, tr('画廊选图'))
        self.config_manager = config_manager
        self.max_count = int(max_count)
        self.header_label.setText(tr('从本地画廊目录选择图片，最多 {count} 张').format(
            count=self.max_count))
        toolbar = QHBoxLayout()
        self.directory_label = QLabel(self.content)
        self.directory_label.setWordWrap(True)
        self.directory_label.setStyleSheet('color:%s;' % materials.palette()['text2'])
        self.choose_dir_button = PushButton(tr('选择画廊目录'), self.content)
        self.choose_dir_button.clicked.connect(self.choose_directory)
        toolbar.addWidget(self.directory_label, 1)
        toolbar.addWidget(self.choose_dir_button)
        self.content_layout.addLayout(toolbar)
        self.grid = _PickerGrid(self.content)
        self.grid.MAX_SELECTED = self.max_count
        self.content_layout.addWidget(self.grid, 1)
        self.empty_label = self.grid.empty_label
        self.reload()

    def reload(self):
        directory = gallery_directory(self.config_manager)
        if directory is None or not directory.is_dir():
            self.directory_label.setText(tr('请先选择画廊目录'))
            self.grid.set_images([])
            return
        self.directory_label.setText(str(directory))
        self.grid.set_images([(path, Path(path).name)
                              for path in scan_gallery_images(directory)])

    def _retranslate_dynamic(self):
        self.header_label.setText(tr('从本地画廊目录选择图片，最多 {count} 张').format(
            count=self.max_count))
        directory = gallery_directory(self.config_manager)
        if directory is None or not directory.is_dir():
            self.directory_label.setText(tr('请先选择画廊目录'))
        else:
            self.directory_label.setText(str(directory))
        self.grid.empty_label.setText(tr('暂无可选图片'))

    def choose_directory(self):
        current = gallery_directory(self.config_manager)
        start = str(current) if current is not None and current.is_dir() else str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, tr('选择画廊目录'), start)
        if not selected:
            return
        try:
            target = Path(selected).expanduser()
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.show_error(tr('画廊目录不可用，请换一个目录'))
            return
        self.config_manager.update(('ecommerce', 'gallery_dir'), str(target))
        self.reload()

    def collect(self):
        return self.grid.selected_paths()


class UrlImageDialog(_PickerDialogBase):
    """输入 URL：多行链接 / 本地绝对路径，下载到本地缓存后返回。"""

    def __init__(self, parent, cache_dir, *, fetcher=None, max_count=6):
        super().__init__(parent, tr('输入图片 URL'))
        self.cache_dir = cache_dir
        self.fetcher = fetcher
        self.max_count = int(max_count)
        self.header_label.setText(tr(
            '每行一个图片链接（http/https）或本地绝对路径，最多 {count} 条').format(
                count=self.max_count))
        self.editor = QPlainTextEdit(self.content)
        self.editor.setPlaceholderText(
            tr('https://example.com/product.png\nD:\\图片\\产品白底图.jpg'))
        self.editor.setMinimumHeight(px(160))
        self.content_layout.addWidget(self.editor, 1)
        self.hint_label = QLabel(tr('链接会下载到本地缓存目录，网络失败会给出中文提示'), self.content)
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet('color:%s;' % materials.palette()['text3'])
        self.content_layout.addWidget(self.hint_label)
        self.error_label.setStyleSheet('color:%s;' % materials.palette()['warning'])

    def _retranslate_dynamic(self):
        self.header_label.setText(tr(
            '每行一个图片链接（http/https）或本地绝对路径，最多 {count} 条').format(
                count=self.max_count))
        self.editor.setPlaceholderText(
            tr('https://example.com/product.png\nD:\\图片\\产品白底图.jpg'))
        self.hint_label.setText(tr('链接会下载到本地缓存目录，网络失败会给出中文提示'))

    def collect(self):
        entries = parse_input_lines(self.editor.toPlainText())
        if not entries:
            self.show_error(tr('请输入至少一个图片链接或本地路径'))
            return []
        paths, errors = resolve_input_entries(
            entries, self.cache_dir, fetcher=self.fetcher, max_count=self.max_count)
        if errors:
            self.error_label.setText('；'.join(errors))
            self.error_label.setVisible(True)
        if not paths:
            if not errors:
                self.show_error(tr('没有可用的图片'))
            return []
        return paths
