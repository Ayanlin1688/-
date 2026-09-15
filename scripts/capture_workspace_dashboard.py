"""Capture exactly three workspace acceptance images using isolated local fixtures."""
import copy
from datetime import datetime
import math
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import Qt, QRectF, QPointF
from PyQt5.QtGui import QImage, QPainter, QColor, QPen, QLinearGradient, QPainterPath
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest
from ui.main_window import MainWindow
from scripts.screenshot_fixture import make_fixture


def illustrate_reference(path, variation):
    """Draw clearly synthetic bedding reference art; no network/assets/credentials."""
    image = QImage(960, 640, QImage.Format_RGB32)
    painter = QPainter(image); painter.setRenderHint(QPainter.Antialiasing)
    background = QLinearGradient(0, 0, 960, 640)
    background.setColorAt(0, QColor('#ded5cc')); background.setColorAt(1, QColor('#a7968e'))
    painter.fillRect(image.rect(), background); painter.setPen(Qt.NoPen)
    painter.setBrush(QColor('#cdc2b7')); painter.drawRoundedRect(QRectF(180, 90, 610, 310), 32, 32)
    painter.setBrush(QColor('#685850')); painter.drawRoundedRect(QRectF(150, 290, 680, 260), 30, 30)
    painter.setBrush(QColor('#eee8df')); painter.drawRoundedRect(QRectF(154, 256, 670, 248), 28, 28)
    for x in (228, 484):
        painter.drawRoundedRect(QRectF(x, 177, 232, 112), 26, 26)
    blanket = QLinearGradient(170, 300, 800, 545)
    blanket.setColorAt(0, QColor(('#a35665', '#934052', '#b15e70')[variation]))
    blanket.setColorAt(1, QColor('#5d293a'))
    painter.setBrush(blanket); painter.drawRoundedRect(QRectF(160, 320, 665, 226), 17, 17)
    painter.setPen(QPen(QColor(255, 207, 219, 70), 2))
    for y in range(342, 538, 15):
        curve = QPainterPath(QPointF(172, y))
        for x in range(176, 814, 4):
            curve.lineTo(x, y + 3*math.sin(x/16 + variation))
        painter.drawPath(curve)
    painter.setPen(Qt.NoPen); painter.setBrush(QColor('#ddd0b8'))
    painter.drawRoundedRect(QRectF(35, 307, 88, 125), 6, 6)
    painter.setBrush(QColor('#57533f')); painter.drawEllipse(QRectF(49, 185, 61, 137))
    painter.setPen(QColor('#52463e')); font = painter.font(); font.setPixelSize(18); painter.setFont(font)
    painter.drawText(34, 603, f'STUDIO REFERENCE  /  0{variation+1}  /  界面演示素材')
    painter.end(); image.save(str(path))


def wait_until(predicate, timeout=10000):
    start = time.monotonic()
    while not predicate():
        if (time.monotonic()-start)*1000 > timeout:
            raise RuntimeError('截图等待超时')
        QTest.qWait(25)


def main():
    output = ROOT / 'screenshots'; output.mkdir(exist_ok=True)
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix='workspace-ui-') as folder:
        config, originals = make_fixture(folder)
        config.config['schedule']['enabled'] = False
        config.save_config()
        for task in originals:
            for index, path in enumerate(task['images']):
                illustrate_reference(path, index)
        window = MainWindow(config, network_time=False)
        window.schedule_timer.stop(); window.settings_page.pool_timer.stop()
        window.setWindowTitle('Yanlin Smart-Creation Matrix · 本地界面验收示例（非真实生成结果）')
        window.resize(1600, 1020); window.show(); QTest.qWait(300)
        page = window.workspace_page
        wait_until(lambda: not page.jobs.busy)
        tasks = copy.deepcopy(originals[:4])
        for task, status, progress in zip(tasks, ('processing', 'completed', 'failed', 'waiting'), (46, 100, 12, 0)):
            task.update(status=status, progress=progress, error='', effective_parameters=dict(aspect_ratio='9:16', resolution='1080p', duration=8))
            if status != 'processing' and status != 'completed':
                task['task_id'] = ''
            task['finished_at'] = datetime.now().isoformat() if status == 'completed' else ''
            if status == 'failed':
                task['error'] = '本地界面示例：服务端返回失败'
        tasks[0]['model'] = 'MiniMax-H3'; tasks[1]['model'] = 'video-v2'; tasks[2]['model'] = 'video-v3'
        tasks[1]['effective_parameters']['resolution'] = '720p'; tasks[2]['effective_parameters']['resolution'] = '720p'
        tasks[1]['effective_parameters']['duration'] = 10
        config.config['history'] = [t for t in tasks if t['status'] in {'completed', 'failed'}]
        page._tasks_updated(tasks); page._current_changed(0, tasks[0]); page._elapsed = 142
        # 演示统一的“运行中”快照：顶栏/统计条/按钮状态自洽。
        page.task_manager.is_running = True
        page._running_changed(True)
        page._started_at = time.monotonic() - 142
        page.product_progress_label.setText('当前：玫瑰毯子 (1/4)，总进度：产品 0/1')
        page.summary.update_tasks(tasks, config.config['history'], 142, True)
        page._update_summary()  # 顶栏与统计条使用同一份快照，避免演示数据自相矛盾
        page.task_manager.is_running = False  # 仅用于演示渲染；复位后退出流程保持干净
        page.stats_timer.stop(); page.metrics_timer.stop()
        page.log_drawer.clear()
        for text, level in [
            ('界面验收示例 · 使用隔离配置与本地插画，未提交付费请求', 'info'),
            ('匹配完成：共26个提示词，26个已匹配，0个未匹配', 'info'),
            ('任务1/4：提示词=01_柔软纹理.txt，产品=玫瑰毯子，模型=MiniMax-H3', 'info'),
            ('任务1/4：绑定参考图3张：1(1).png, 1(2).png, 1(3).png', 'info'),
            ('任务1/4：参数=比例9:16，分辨率1080p，时长8秒', 'info'),
            ('01_柔软纹理：MiniMax-H3 · 9:16 · 1080p · 正在生成 46%', 'info'),
            ('02_铺床展示：video-v2 失败，切换到 video-v3 重试，video-v2 进入冷却30秒', 'warning'),
            ('03_沙发盖毯：服务端返回失败，保留原任务记录（演示状态）', 'error'),
            ('02_铺床展示：已下载 002_铺床展示.mp4（演示状态）', 'success')]:
            page.append_log(text, level)
        page._row_progress(0, 46, 84, 103)
        page.splitter.setSizes([max(540, page.height()-330), 140]); QTest.qWait(600)
        print(f'Capture canvas: {window.size().width()}x{window.size().height()}')
        visible = 0
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            visible = sum(page.queue_panel.list.visualItemRect(page.queue_panel.task_item(i)).bottom() <= page.queue_panel.list.viewport().height() for i in range(len(tasks)))
            if visible >= 3:
                break
            QTest.qWait(150)
        print(f'Complete visible task rows: {visible}')
        assert window.grab().save(str(output / 'screenshot_workspace_full.png'))
        if visible < 3:
            print('警告：可见任务行不足3行，已按当前布局截图')
        # Crop the real rendered table, including header and complete expanded rows.
        table = page.queue_panel
        page._row_progress(0, 46, 84, 103)
        detail = table.grab().copy(0, 0, table.width(), min(table.height(), 46+34+52*3+12))
        assert detail.save(str(output / 'screenshot_table_detail.png'))
        QTest.mouseClick(table.rows[0].images_button, Qt.LeftButton); QTest.qWait(300)
        if hasattr(page, 'image_preview') and page.image_preview.isVisible():
            page.image_preview.grab().save(str(output / 'screenshot_image_preview.png')); page.image_preview.close()
        # The acceptance capture requested for this compact layout is the
        # bottom log close-up; keep the legacy preview artifact too.
        log_crop = page.log_drawer.grab()
        assert log_crop.save(str(output / 'screenshot_log.png'))
        assert log_crop.save(str(output / 'screenshot_log_filter.png'))
        window.close()
        wait_until(lambda: not window._background_busy()); app.processEvents()
        window.deleteLater(); QTest.qWait(50)
    print('Saved 3 workspace acceptance screenshots; local fixtures only.')


if __name__ == '__main__':
    main()
