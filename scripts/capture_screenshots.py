"""Capture the Fluent UI with labelled synthetic local screenshot fixtures."""

from __future__ import annotations

import os
import sys
import tempfile
import zipfile
import copy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "screenshots"
sys.path.insert(0, str(ROOT))

from PyQt5.QtCore import QEventLoop, QPoint, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication, QScrollArea
from qfluentwidgets import Theme, setTheme, setThemeColor

from ui.components.match_dialog import MatchDialog
from ui.components.custom_widgets import ensure_ui_font
from ui.main_window import MainWindow
from core.config_manager import ConfigManager
from scripts.screenshot_fixture import make_fixture


def capture(widget, name: str) -> None:
    widget.show()
    QApplication.processEvents()
    pixmap = widget.grab()
    pixmap.save(str(OUT / name), "PNG")


def wait_for_animation(milliseconds: int = 240) -> None:
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec_()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    setTheme(Theme.DARK, save=False)
    setThemeColor(QColor("#409eff"), save=False)
    ensure_ui_font()
    # Synthetic data belongs only to this isolated screenshot config, never production history.
    temp = tempfile.TemporaryDirectory()
    config, tasks = make_fixture(temp.name)
    window = MainWindow(config, network_time=False)
    window.schedule_timer.stop()  # A fixture's enabled schedule must never submit work.
    window.settings_page.pool_timer.stop()  # Keep fixture countdown stable during capture.
    window.setWindowTitle('StoryboardVideoStudio · 本地截图示例（非真实生成结果）')
    window.resize(1600, 1200)
    window.show()
    QApplication.processEvents()
    wait_for_animation()
    while window.workspace_page.jobs.busy:
        wait_for_animation(50)
    window.workspace_page._tasks_updated(tasks)
    window.workspace_page._current_changed(4, tasks[4])
    window.workspace_page.current_task.update_progress(45, 84, 103)
    window.workspace_page.queue_panel.select_task(4)
    wait_for_animation()
    window.workspace_page.append_log("本地界面示例：已识别 2 个产品，共 8 个提示词，每条绑定 3 张参考图", "success")
    window.workspace_page.append_log("本地界面示例：玫瑰毯子队列结束，切换到车载风扇", "info")
    window.workspace_page.append_log("本地界面示例：模型 video-v2 失败，切换到 video-v3 重试，video-v2 进入冷却30秒", "warning")
    window.workspace_page.append_log("本地界面示例：第2次重试，剩余3次；最大并发2", "warning")
    window.workspace_page.append_log("本地界面示例：提示词已分别识别为 MiniMax-H3 / video-v2 / video-v3", "info")
    window.workspace_page.append_log("本地界面示例：提示词格式已从H3转换为V2格式", "info")
    window.workspace_page.append_log("本地界面示例：检测到相同任务，跳过避免重复扣费", "warning")
    window.workspace_page.append_log("本地界面示例：HTTP 429，提交待确认，禁止自动重新创建", "warning")

    # First view demonstrates two occupied slots and the empty recent panel.
    pending = copy.deepcopy(tasks)
    for task in pending:
        if task['status'] == 'completed':
            task.update(status='waiting', result_path='', task_id='')
    pending[0].update(status='processing', model='MiniMax-H3', task_id='local_fixture_001', error='本地截图示例')
    pending[1].update(status='retry_wait', model='video-v2', error='提交失败，正在重试（截图示例）')
    for task in pending[4:]:
        task.update(status='waiting', task_id='')
    window.workspace_page._tasks_updated(pending)
    window.workspace_page.recent_panel.update_history([])
    window.workspace_page._current_changed(0, pending[0])
    window.workspace_page.current_task.update_progress(45, 84, 103)
    window.workspace_page.queue_panel.select_task(0)
    wait_for_animation()
    capture(window, "screenshot_1_workspace.png")

    dialog = MatchDialog(window, window.workspace_page.append_log, config,
                         window.workspace_page.data_source.matches, window.workspace_page.data_source.automatic_matches)
    dialog.resize(1200, 1100)
    dialog.model_combo.setCurrentText('video-v2')
    dialog.preview_tabs.setCurrentItem('converted')
    wait_for_animation()
    capture(dialog, "screenshot_2_match_dialog.png")
    dialog.close()

    window.workspace_page._tasks_updated(tasks)
    window.workspace_page.recent_panel.update_history(config.config['history'])
    window.workspace_page._current_changed(4, tasks[4])
    window.workspace_page.current_task.update_progress(45, 84, 103)
    window.workspace_page.queue_panel.select_task(4)
    window.workspace_page.params_card.toggle_advanced()
    window.workspace_page._current_changed(2, tasks[2])
    window.workspace_page.queue_panel.select_task(2)
    window.workspace_page.current_task.update_progress(0, 0, -1)
    wait_for_animation()
    capture(window, "screenshot_3_params_collapsed.png")
    window.switchTo(window.history_page)
    wait_for_animation(350)
    capture(window, "screenshot_4_history.png")
    window.switchTo(window.settings_page)
    window.resize(1600, 1160)
    wait_for_animation(350)
    scroll = window.settings_page.findChild(QScrollArea)
    if scroll is not None:
        scroll.verticalScrollBar().setValue(window.settings_page.auto_detect.mapTo(scroll.widget(), QPoint()).y() - 50)
    wait_for_animation(350)
    capture(window, "screenshot_5_settings.png")

    window.resize(1600, 1200)
    window.switchTo(window.workspace_page)
    wait_for_animation(350)
    QApplication.processEvents()
    window.workspace_page.log_drawer.toggle()
    window.workspace_page.queue_panel.toggle_group('玫瑰毯子')
    wait_for_animation()
    capture(window, "screenshot_6_log_collapsed.png")
    window.close()
    app.processEvents()
    temp.cleanup()
    with zipfile.ZipFile(ROOT / 'screenshots.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(OUT.glob('screenshot_*.png')):
            archive.write(path, path.name)


if __name__ == "__main__":
    main()
