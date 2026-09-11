"""Render recorded real DEBUG evidence through the application's Fluent log widget."""
import json
import argparse
import os
from pathlib import Path
import re
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from qfluentwidgets import TitleLabel, CaptionLabel, Theme, setTheme, setThemeColor
from ui.widgets.log_drawer import LogDrawer
from ui.widgets.current_task_card import CurrentTaskCard
from ui.components.match_dialog import MatchDialog
from ui.components.custom_widgets import ensure_ui_font
from ui.theme import apply_palette, style_controls


def main():
    parser = argparse.ArgumentParser(description='从真实运行证据生成Fluent截图，不联网、不提交。')
    parser.add_argument('--auto-match', action='store_true')
    args = parser.parse_args()
    folder = ROOT / 'artifacts' / ('reference-automatch' if args.auto_match else 'reference-debug')
    events = json.loads((folder / 'events.json').read_text(encoding='utf-8'))
    report = json.loads((folder / 'run.json').read_text(encoding='utf-8'))
    task = report['tasks'][0]
    if not task.get('task_id') or task.get('submitted_image_count') != 3:
        raise SystemExit('尚无真实三图提交成功记录，不生成成功截图。')
    app = QApplication([]); ensure_ui_font(); apply_palette(app)
    setTheme(Theme.DARK, save=False); setThemeColor('#5e6ad2', save=False)
    surface = QWidget(); surface.setObjectName('referenceEvidence')
    surface.setStyleSheet('#referenceEvidence { background: #0a0a0b; }')
    outer = QVBoxLayout(surface); outer.setContentsMargins(24, 24, 24, 24); outer.setSpacing(16)
    title = TitleLabel('真实三图调试 · 参考图与上传校验'); outer.addWidget(title)
    outer.addWidget(CaptionLabel(f"运行日志回放 · {report['updated_at']} · task_id={task['task_id']}"))
    card = CurrentTaskCard(lambda *_: None); card.set_debug_mode(True); card.update_task(0, task)
    progress = next((float(match.group(1)) for event in reversed(events)
                     if (match := re.search(r'进度=([0-9.]+)%', event['message']))), 0)
    card.update_progress(progress, 0, -1)
    card.timing.setText(f'服务端最近报告进度：{progress:g}% · 非实时回放')
    outer.addWidget(card)
    drawer = LogDrawer(); drawer.set_debug_mode(True)
    drawer.browser.setMaximumHeight(16777215)
    outer.addWidget(drawer, 1)
    style_controls(surface)
    surface.resize(1500, 1320)
    surface.show(); app.processEvents()
    QTest.qWait(350)  # Finish the real Fluent progress animation before capture.

    for name, label, predicate in (
        ('debug_1_references.png', '真实三图调试 · 数量、顺序、URL 与原图一致性',
         lambda message: not message.startswith(('提示词替换前:', '提示词替换后:', '实际提交提示词:', '请求体:'))),
        ('debug_2_request.png', '真实三图调试 · 完整请求体（已脱敏）',
         lambda message: message.startswith(('请求:', '请求体:', '实际提交images:'))),
    ):
        title.setText(label.replace('真实三图调试', '自动匹配三图验证') if args.auto_match else label)
        drawer.entries = [(event['time'].split('T')[-1], event['message'], event['level']) for event in events
                          if (event['level'] == 'debug' and predicate(event['message'])) or
                          (name == 'debug_1_references.png' and '上传图片3张' in event['message'])]
        drawer.filter_box.setCurrentIndex(0); drawer._render()
        app.processEvents()
        drawer.browser.verticalScrollBar().setValue(0)
        app.processEvents()
        if not surface.grab().save(str(folder / name)):
            raise RuntimeError('截图保存失败')
        print(f'Saved {folder / name}; overflow={drawer.browser.verticalScrollBar().maximum()}')
    surface.close()
    if args.auto_match:
        dialog = MatchDialog(matches=[task], automatic_matches=[task])
        dialog.setWindowTitle('自动匹配结果 · 真实三图验证记录')
        dialog.resize(1100, 700); dialog.show(); app.processEvents()
        QTest.qWait(100)
        if not dialog.grab().save(str(folder / 'debug_3_match_dialog.png')):
            raise RuntimeError('匹配详情截图保存失败')
        print(f'Saved {folder / "debug_3_match_dialog.png"}; count={dialog.picture_list.count()}')
        dialog.close()


if __name__ == '__main__':
    main()
