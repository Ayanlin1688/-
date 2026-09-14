"""UI-only verification with isolated config. Never loads user paths or credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', help='Use the native Windows Qt window')
    args = parser.parse_args()
    os.environ['QT_QPA_PLATFORM'] = 'windows' if args.native and sys.platform == 'win32' else 'offscreen'
    from PyQt5.QtCore import QEvent, QTimer
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication
    from qfluentwidgets.components.widgets.acrylic_label import isAcrylicAvailable
    from core.config_manager import ConfigManager
    from ui.main_window import MainWindow
    from test_visual_motion import VisualMotionTests

    app = QApplication.instance() or QApplication([])
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(VisualMotionTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1

    intervals = []
    previous = time.perf_counter()
    def heartbeat():
        nonlocal previous
        now = time.perf_counter()
        intervals.append((now-previous)*1000)
        previous = now
    with tempfile.TemporaryDirectory() as folder:
        window = MainWindow(ConfigManager(Path(folder)/'config.json'), network_time=False)
        window.schedule_timer.stop()
        window.resize(1400, 1000); window.show(); QTest.qWait(300)
        workspace = window.workspace_page
        workspace.params_card.toggle_advanced(); QTest.qWait(300)
        workspace.queue_panel.update_tasks([
            dict(prompt_name=f'视觉测试{i+1}', status='processing', model='video-v3', images=[], progress=55)
            for i in range(5)])
        workspace.queue_panel.list.scrollToTop()
        QTest.qWait(200)
        timer = QTimer(); timer.setInterval(16); timer.timeout.connect(heartbeat)
        previous = time.perf_counter(); timer.start()
        button = workspace.start_button
        for _ in range(5):
            QApplication.sendEvent(button, QEvent(QEvent.Enter)); QTest.qWait(250)
            QApplication.sendEvent(button, QEvent(QEvent.Leave)); QTest.qWait(250)
        timer.stop()
        window.showMinimized(); QTest.qWait(150)
        minimized_idle = not window._visual_clock.timer.isActive()
        window.showNormal(); QTest.qWait(250)
        restored = window._visual_clock.timer.isActive()
        window.close(); QTest.qWait(60)
        window.deleteLater(); QTest.qWait(60)

    samples = sorted(intervals)
    report = {
        'platform': app.platformName(),
        'windows_build': sys.getwindowsversion().build if sys.platform == 'win32' else None,
        'tests_passed': result.testsRun,
        'card_material': 'cached translucent gradient and noise (no live desktop blur)',
        'optional_acrylic_available': bool(isAcrylicAvailable),
        'minimized_visual_timer_stopped': minimized_idle,
        'restored_visual_timer_running': restored,
        'heartbeat_samples': len(samples),
        'heartbeat_median_ms': round(statistics.median(samples), 2),
        'heartbeat_p95_ms': round(samples[min(len(samples)-1, int(len(samples)*.95))], 2),
        'heartbeat_max_ms': round(max(samples), 2),
        'measurement': '16ms GUI timer while 5 status dots, progress shimmer and button hover animate; not FPS',
    }
    out = ROOT / 'artifacts' / 'workspace-dashboard'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if minimized_idle and restored else 1


if __name__ == '__main__':
    raise SystemExit(main())
