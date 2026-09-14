"""Workspace statistics strip and asynchronous directory metrics."""
from datetime import datetime
from pathlib import Path
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QFrame
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
        layout = QVBoxLayout(self); layout.setContentsMargins(14, 9, 14, 9); layout.setSpacing(3)
        self.title = label(title, 11, '#8b8b9e'); layout.addWidget(self.title)
        self.value = label(value, 18, accent, True, mono=title in {'运行时长', '用时/剩余'}); layout.addWidget(self.value)
        self.value.setTextFormat(Qt.RichText)


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
        self.metrics_card = WorkspaceCard(); self.metrics_card.setFixedHeight(78)
        row = QHBoxLayout(self.metrics_card); row.setContentsMargins(2, 2, 2, 2); row.setSpacing(0)
        specs = [('批量生成中', '就绪', BLUE), ('产品进度', '0/0', '#f0f0f5'), ('任务进度', '0/0', '#f0f0f5'),
                 ('今日完成', '0', GREEN), ('待完成', '0', '#f0f0f5'), ('成功率', '—', '#f0f0f5'), ('失败', '0', RED), ('并发', '1', '#f0f0f5')]
        self.blocks = []
        for index, (title, value, accent) in enumerate(specs):
            if index:
                separator = QFrame(); separator.setFrameShape(QFrame.VLine); separator.setFixedWidth(1); separator.setStyleSheet('color:rgba(255,255,255,0.08); background:rgba(255,255,255,0.08);')
                row.addWidget(separator)
            block = StatBlock(title, value, accent); row.addWidget(block, 1); self.blocks.append(block)
        root.addWidget(self.metrics_card)
        self.cards = [_CompatCard(self) for _ in range(4)]

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
        values = [('批量生成中', '批量生成中' if running else '就绪', BLUE), ('产品进度', f'{product_done}/{len(groups)}', '#f0f0f5'),
                  ('任务进度', f'{completed}/{total}', '#f0f0f5'), ('今日完成', str(sum(t.get('status') == 'completed' for t in history if str(t.get('finished_at','')).startswith(datetime.now().date().isoformat()))), GREEN),
                  ('待完成', str(pending), '#f0f0f5'), ('成功率', f'{completed/done*100:.0f}%' if done else '—', '#f0f0f5'), ('失败', str(failed), RED),
                  ('并发', str(max(1, sum(t.get('status') in {'queued','uploading','submitting','processing','downloading'} for t in tasks))), '#f0f0f5')]
        for block, (title, value, accent) in zip(self.blocks, values):
            block.title.setText(title); block.value.setText(value); block.value.setTextColor(accent, accent)
        self.cards[0].number.setText(str(total))
        matched = sum(bool(t.get('images')) for t in tasks)
        self.cards[0].detail.setText(f'✓ {matched} 已匹配 · ⚠ {total-matched} 未匹配')
