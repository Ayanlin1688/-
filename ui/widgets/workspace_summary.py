"""Workspace statistics strip and asynchronous directory metrics."""
from datetime import datetime
from pathlib import Path
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QFrame, QSizePolicy
from core.i18n import tr
from .workspace_surface import WorkspaceCard, label, MUTED, BLUE, GREEN, RED


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


class StatBlock(QWidget):
    def __init__(self, title, value='0', accent=MUTED, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self); layout.setContentsMargins(16, 9, 16, 9); layout.setSpacing(3)
        self.title = label(title, 12, '#8B93A3')
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.title)
        # 卡片数值降到 15px（页面标题保持第一层级）；Ignored 宽度策略让 8 张卡严格均分。
        self.value = label(value, 15, accent, True, mono=True)
        self.value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.value)
        self.value.setTextFormat(Qt.RichText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)


class _CompatCard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.setFixedSize(0, 0)
        self.number = label('0'); self.detail = label('')
        self.choose_button = QWidget(self); self.choose_button.setFixedSize(0, 0)


class WorkspaceSummary(QWidget):
    directory_requested = pyqtSignal(str)
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        self.metrics_card = WorkspaceCard(); self.metrics_card.setFixedHeight(64)
        row = QHBoxLayout(self.metrics_card); row.setContentsMargins(2, 2, 2, 2); row.setSpacing(0)
        specs = [(tr('批量生成中'), tr('就绪'), BLUE), (tr('产品进度'), '0/0', '#f0f0f5'), (tr('任务进度'), '0/0', '#f0f0f5'),
                 (tr('今日完成'), '0', GREEN), (tr('待完成'), '0', '#f0f0f5'), (tr('成功率'), '—', '#f0f0f5'), (tr('失败'), '0', RED), (tr('并发'), '1', '#f0f0f5')]
        self.blocks = []
        self._separators = []
        for index, (title, value, accent) in enumerate(specs):
            if index:
                separator = QFrame(); separator.setFrameShape(QFrame.VLine); separator.setFixedWidth(1)
                self._separators.append(separator)
                row.addWidget(separator)
            block = StatBlock(title, value, accent); row.addWidget(block, 1); self.blocks.append(block)
        root.addWidget(self.metrics_card)
        self.cards = [_CompatCard(self) for _ in range(4)]
        from ..materials import register_theme_callback
        register_theme_callback(self._apply_mode_style)
        self._apply_mode_style()

    def _apply_mode_style(self):
        try:
            from ..materials import is_light
            color = 'rgba(15,26,52,0.10)' if is_light() else 'rgba(255,255,255,0.06)'
            for separator in self._separators:
                separator.setStyleSheet(f'color:{color}; background:{color};')
        except RuntimeError:
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
        seconds = int(elapsed)
        clock = f'{seconds//3600:02d}:{seconds%3600//60:02d}:{seconds%60:02d}'
        from ..materials import map_text_color, is_light
        light = is_light()
        run_color = '#3E6FD1' if light else '#5B8DEF'
        ready_color = '#2E8B57' if light else '#7CC79A'
        first_value = (f'<span style="color:{run_color}">{clock}</span>' if running
                       else f'<span style="color:{ready_color}">{tr("就绪")}</span>')
        active = sum(t.get('status') in {'queued','uploading','submitting','processing','downloading'} for t in tasks)
        today = sum(t.get('status') == 'completed' for t in history if str(t.get('finished_at','')).startswith(datetime.now().date().isoformat()))

        def zeroish(text):
            return set(str(text)) <= set('0/— ')

        dim = '#8B93A3'
        values = [
            (tr('批量生成中'), first_value, None),
            (tr('产品进度'), f'{product_done}/{len(groups)}', dim if zeroish(f'{product_done}/{len(groups)}') else '#f0f0f5'),
            (tr('任务进度'), f'{completed}/{total}', dim if zeroish(f'{completed}/{total}') else '#f0f0f5'),
            (tr('今日完成'), str(today), GREEN if today else dim),
            (tr('待完成'), str(pending), '#f0f0f5' if pending else dim),
            (tr('成功率'), f'{completed/done*100:.0f}%' if done else '—', '#f0f0f5' if done else dim),
            (tr('失败'), str(failed), RED if failed else dim),
            (tr('并发'), str(active), '#f0f0f5' if active else dim),
        ]
        for block, (title, value, accent) in zip(self.blocks, values):
            block.title.setText(title); block.value.setText(value)
            if accent is not None:
                mapped = map_text_color(accent)
                block.value.setTextColor(mapped, mapped)
        self.cards[0].number.setText(str(total))
        matched = sum(bool(t.get('images')) for t in tasks)
        self.cards[0].detail.setText(f'✓ {matched} {tr("已匹配")} · ⚠ {total-matched} {tr("未匹配")}')
