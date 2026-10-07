"""后台任务收尾竞态回归：worker 销毁后到达的 finished 回调必须安全返回。"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5 import sip
from PyQt5.QtWidgets import QApplication

from core.background import BackgroundJobs, FunctionWorker


class BackgroundJobsRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_finished_after_worker_deletion_is_safe(self):
        jobs = BackgroundJobs()
        worker = FunctionWorker(lambda: None, jobs)
        jobs.workers.append(worker)
        sip.delete(worker)
        self.assertTrue(sip.isdeleted(worker))
        jobs._finished(worker)  # 修复前：RuntimeError 被 PyQt 升级为 qFatal 直接终止进程
        self.assertEqual(jobs.workers, [])
        sip.delete(jobs)

    def test_finished_after_jobs_deletion_is_safe(self):
        jobs = BackgroundJobs()
        worker = FunctionWorker(lambda: None, jobs)
        jobs.workers.append(worker)
        sip.delete(jobs)
        jobs._finished(worker)  # 对象已随窗口销毁：不得再触发任何 C++ 侧调用

    def test_force_stop_clears_workers_without_leaving_a_running_thread(self):
        jobs = BackgroundJobs()
        worker = FunctionWorker(lambda: None, jobs)
        jobs.workers.append(worker)
        jobs.force_stop()
        self.assertEqual(jobs.workers, [])


if __name__ == '__main__':
    unittest.main()
