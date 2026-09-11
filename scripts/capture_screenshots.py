"""Capture the Stage 3 UI with labelled synthetic local screenshot fixtures."""

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

from PyQt5.QtCore import QEventLoop, QTimer
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
    setThemeColor(QColor("#5e6ad2"), save=False)
    ensure_ui_font()
    # Synthetic data belongs only to this isolated screenshot config, never production history.
    temp = tempfile.TemporaryDirectory()
    config, tasks = make_fixture(temp.name)
    window = MainWindow(config, network_time=False)
    window.schedule_timer.stop()  # A fixture's enabled schedule must never submit work.
    window.setWindowTitle('StoryboardVideoStudio · 本地截图示例（非真实生成结果）')
    window.resize(1600, 1100)
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
    window.workspace_page.append_log("本地界面示例：失败/跳过也计入产品已结束数量", "warning")

    # First view demonstrates failed/skipped states and the true empty recent panel.
    pending = copy.deepcopy(tasks)
    for task in pending:
        if task['status'] == 'completed':
            task.update(status='waiting', result_path='', task_id='')
    pending[0].update(status='failed', task_id='', error='参数校验失败：H3 不支持 720p（截图示例）')
    window.workspace_page._tasks_updated(pending)
    window.workspace_page.recent_panel.update_history([])
    window.workspace_page._current_changed(0, pending[0])
    window.workspace_page.current_task.update_progress(0, 3, -1)
    window.workspace_page.queue_panel.select_task(0)
    wait_for_animation()
    capture(window, "screenshot_1_workspace.png")

    dialog = MatchDialog(window, window.workspace_page.append_log, config,
                         window.workspace_page.data_source.matches, window.workspace_page.data_source.automatic_matches)
    capture(dialog, "screenshot_2_match_dialog.png")
    dialog.close()

    window.workspace_page._tasks_updated(tasks)
    window.workspace_page.recent_panel.update_history(config.config['history'])
    window.workspace_page._current_changed(4, tasks[4])
    window.workspace_page.current_task.update_progress(45, 84, 103)
    window.workspace_page.queue_panel.select_task(4)
    window.workspace_page.params_card.toggle_advanced()
    wait_for_animation()
    capture(window, "screenshot_3_params_collapsed.png")
    window.switchTo(window.history_page)
    wait_for_animation(350)
    capture(window, "screenshot_4_history.png")
    window.switchTo(window.settings_page)
    wait_for_animation(350)
    scroll = window.settings_page.findChild(QScrollArea)
    if scroll is not None:
        scroll.ensureWidgetVisible(window.settings_page.sync_button, 0, 60)
    wait_for_animation(350)
    capture(window, "screenshot_5_settings.png")

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
