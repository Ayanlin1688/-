"""Workspace statistics strip and asynchronous directory metrics."""
from datetime import datetime
from pathlib import Path
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QFrame, QLabel, QSizePolicy
from core.i18n import tr
from .workspace_surface import WorkspaceCard, label, MUTED, BLUE, GREEN, RED, YELLOW


def directory_metrics(paths):
    result = dict(images=0, products=0, videos=0, bytes=0)
    image_root, output_root = paths.get('images'), paths.get('output')
    if image_root and Path(image_root).is_dir():
        root = Path(image_root)
        result['products'] = sum(path.is_dir() for path in root.iterdir())
        result['images'] = sum(path.is_file() and path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp', '.bmp'} for path in root.rglob('*'))
    if output_root and Path(output_root).is_dir():
        for path in Path(output_root).rglob('*'):
            if path.is_file() and path.suffix.lower() in {'.mp4', '.mov', '.avi', '.webm'}:
                try:
                    result['bytes'] += path.stat().st_size; result['videos'] += 1
                except OSError:
                    pass
    return result


class MiniBar(QWidget):
    """3px 微型进度条（任务进度/成功率的直观补充）。双主题适配。"""

    def __init__(self, color='#5B8DEF', parent=None):
        super().__init__(parent)
        self.setFixedHeight(3)
        self._ratio = 0.0
        self._color = QColor(color)

    def set_ratio(self, ratio):
        self._ratio = max(0.0, min(1.0, float(ratio)))
        self.update()

    def set_color(self, color):
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event):
        try:
            from ..materials import is_light
            track = QColor(0, 0, 0, 28) if is_light() else QColor(255, 255, 255, 26)
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(track)
            painter.drawRoundedRect(self.rect(), 1.5, 1.5)
            if self._ratio > 0:
                width = max(3, int(self.width() * self._ratio))
                painter.setBrush(self._color)
                painter.drawRoundedRect(0, 0, width, self.height(), 1.5, 1.5)
            painter.end()
        except Exception:
            pass


class StatBlock(QWidget):
    def __init__(self, title, value='—', parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self); layout.setContentsMargins(15, 8, 15, 8); layout.setSpacing(2)
        self.title = label(title, 12, '#8B93A3')
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.title)
        self.value = label(value, 22, '#f0f0f5', True, mono=True)
        self.value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.value)
        self.value.setTextFormat(Qt.RichText)
        self.sub = label('', 11, '#8B93A3')
        self.sub.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.sub)
        self.bar = MiniBar()
        self.bar.setVisible(False)
        layout.addWidget(self.bar)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)


class CompoundStat(QWidget):
    """复合指标卡：产品进度 / 失败 / 并发 三行紧凑排布。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self); root.setContentsMargins(15, 8, 15, 8); root.setSpacing(2)
        self.rows = {}
        for key, title in (('product', tr('产品进度')), ('failed', tr('失败')), ('active', tr('并发'))):
            row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(8)
            name = label(title, 12, '#8B93A3')
            value = label('—', 12, '#f0f0f5', True, mono=True)
            row.addWidget(name); row.addStretch(1); row.addWidget(value)
            root.addLayout(row)
            self.rows[key] = value


class _CompatCard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.setFixedSize(0, 0)
        self.number = label('0'); self.detail = label('')
        self.choose_button = QWidget(self); self.choose_button.setFixedSize(0, 0)


class WorkspaceSummary(QWidget):
    directory_requested = pyqtSignal(str)
    # 4 张主卡 + 1 张复合卡：主行只留最需要一眼看到的信息（浅色主题微染仅对前 4 张生效）。
    _TINTS = {0: (74, 141, 255), 1: (52, 199, 89), 2: (255, 184, 77), 3: (139, 92, 246)}
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        self.metrics_row = QHBoxLayout(); self.metrics_row.setContentsMargins(0, 0, 0, 0); self.metrics_row.setSpacing(12)
        specs = [(tr('任务进度'), '0/0'), (tr('成功率'), '—'), (tr('今日完成'), '0'), (tr('待处理'), '0')]
        self.blocks = []
        self.stat_cards = []
        for index, (title, value) in enumerate(specs):
            card = WorkspaceCard(); card.setFixedHeight(84)
            lay = QVBoxLayout(card); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(0)
            block = StatBlock(title, value)
            lay.addWidget(block)
            self.metrics_row.addWidget(card, 4)
            self.blocks.append(block); self.stat_cards.append(card)
        compound_card = WorkspaceCard(); compound_card.setFixedHeight(84)
        compound_layout = QVBoxLayout(compound_card); compound_layout.setContentsMargins(0, 0, 0, 0); compound_layout.setSpacing(0)
        self.compound = CompoundStat()
        compound_layout.addWidget(self.compound)
        self.metrics_row.addWidget(compound_card, 5)
        self.compound_card = compound_card
        root.addLayout(self.metrics_row)
        self.cards = [_CompatCard(self) for _ in range(4)]
        self.blocks[0].bar.setVisible(True)
        self.blocks[1].bar.setVisible(True)
        from ..materials import register_theme_callback
        register_theme_callback(self._apply_mode_style)
        self._apply_mode_style()

    def _apply_mode_style(self):
        try:
            from ..materials import is_light
            light = is_light()
            for index, card in enumerate(self.stat_cards):
                rgb = self._TINTS.get(index)
                card.set_tint(QColor(rgb[0], rgb[1], rgb[2], 96) if (light and rgb) else None)
        except RuntimeError:
            pass
        except Exception:
            pass

    def update_paths(self, paths):
        self._paths = dict(paths)

    def update_metrics(self, metrics):
        self._metrics = dict(metrics)

    def update_tasks(self, tasks, history, elapsed, running):
        completed = sum(t.get('status') == 'completed' for t in tasks)
        failed = sum(t.get('status') == 'failed' for t in tasks)
        pending = sum(t.get('status', 'waiting') not in {'completed', 'failed', 'cancelled', 'skipped', 'duplicate', 'submission_unknown'} for t in tasks)
        total = len(tasks); done = completed + failed
        groups = {t.get('product') or '未分组' for t in tasks}
        product_done = sum(all(t.get('status') in {'completed','failed','cancelled','skipped','duplicate','submission_unknown'} for t in tasks if (t.get('product') or '未分组') == group) for group in groups) if groups else 0
        active = sum(t.get('status') in {'queued','uploading','submitting','processing','downloading'} for t in tasks)
        waiting = sum(t.get('status', 'waiting') == 'waiting' for t in tasks)
        today = sum(t.get('status') == 'completed' for t in history if str(t.get('finished_at','')).startswith(datetime.now().date().isoformat()))
        from ..materials import map_text_color
        dim = '#8B93A3'

        def assign(block, text, strong=True):
            block.value.setText(text)
            color = '#f0f0f5' if strong else dim
            mapped = map_text_color(color)
            block.value.setTextColor(mapped, mapped)

        assign(self.blocks[0], f'{completed}/{total}', total > 0)
        self.blocks[0].sub.setText(f"{tr('生成中')} {active}")
        self.blocks[0].bar.set_ratio(completed / total if total else 0)
        assign(self.blocks[1], f'{completed/done*100:.0f}%' if done else '—', done > 0)
        self.blocks[1].sub.setText(f"{tr('已完成')} {completed} · {tr('失败')} {failed}" if total else '')
        self.blocks[1].bar.set_ratio(completed / done if done else 0)
        assign(self.blocks[2], str(today), today > 0)
        assign(self.blocks[3], str(pending), pending > 0)
        self.blocks[3].sub.setText(f"{tr('等待中')} {waiting}" if waiting else '')
        self.compound.rows['product'].setText(f'{product_done}/{len(groups)}' if groups else '—')
        self.compound.rows['failed'].setText(str(failed))
        mapped = map_text_color(RED if failed else dim)
        self.compound.rows['failed'].setTextColor(mapped, mapped)
        self.compound.rows['active'].setText(str(active))
        self.cards[0].number.setText(str(total))
        matched = sum(bool(t.get('images')) for t in tasks)
        self.cards[0].detail.setText(f'✓ {matched} {tr("已匹配")} · ⚠ {total-matched} {tr("未匹配")}')
