"""Deterministic completion latency tests without provider requests or wall-clock sleeps."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.api_client import ApiClient
from core.config_manager import DEFAULT_CONFIG
from core.http_client import RequestError
from core.task_execution import RemoteGenerationFailed, TaskExecution
from core.task_polling import PollSchedule


class PollScheduleTests(unittest.TestCase):
    def test_low_progress_backoff_is_gentle_and_capped_at_fifteen(self):
        schedule = PollSchedule()
        intervals = [schedule.observe('queued', 20, moment) for moment in range(30)]
        self.assertEqual(intervals[0], 5)
        self.assertAlmostEqual(intervals[1], 6)
        self.assertEqual(max(intervals), 15)

    def test_progress_change_resets_each_stage_immediately(self):
        schedule = PollSchedule()
        for moment in range(20):
            schedule.observe('queued', 20, moment)
        self.assertEqual(schedule.observe('queued', 21, 21), 5)
        self.assertEqual(schedule.observe('processing', 70, 22), 3)
        self.assertEqual(schedule.observe('processing', 95, 23), 2)

    def test_high_progress_stall_becomes_faster_after_ninety_seconds(self):
        schedule = PollSchedule()
        self.assertTrue(all(schedule.observe('processing', 80, moment) == (3 if moment < 90 else 2)
                            for moment in range(300)))

    def test_ninety_five_and_one_hundred_processing_stay_at_two_seconds(self):
        for progress in (95, 99, 100):
            with self.subTest(progress=progress):
                schedule = PollSchedule()
                self.assertTrue(all(schedule.observe('processing', progress, moment) == 2 for moment in range(300)))

    def test_progress_regression_cannot_leave_the_fast_stage(self):
        schedule = PollSchedule()
        self.assertEqual(schedule.observe('processing', 80, 0), 3)
        self.assertEqual(schedule.observe('processing', 10, 10), 3)
        self.assertEqual(schedule.observe('processing', 96, 20), 2)
        self.assertEqual(schedule.observe('processing', 0, 30), 2)

    def test_processing_without_progress_becomes_fast_after_a_minute(self):
        schedule = PollSchedule()
        self.assertEqual(schedule.observe('processing', 0, 0), 5)
        self.assertEqual(schedule.observe('processing', 0, 60), 3)
        self.assertEqual(schedule.observe('processing', 0, 600), 2)

    def test_low_progress_uses_final_interval_after_ninety_seconds(self):
        schedule = PollSchedule()
        self.assertEqual(schedule.observe('processing', 45, 10), 5)
        self.assertEqual(schedule.observe('processing', 45, 69.999), 6)
        self.assertEqual(schedule.observe('processing', 45, 70), 3)
        self.assertEqual(schedule.observe('processing', 45, 99.999), 3)
        self.assertTrue(all(schedule.observe('processing', 45, moment) == 2 for moment in range(100, 300)))

    def test_progress_change_preserves_elapsed_processing_final_stage(self):
        schedule = PollSchedule()
        schedule.observe('processing', 45, 0)
        self.assertEqual(schedule.observe('processing', 45, 90), 2)
        self.assertEqual(schedule.observe('processing', 46, 91), 2)
        self.assertEqual(schedule.observe('processing', 20, 92), 2)

    def test_interrupted_processing_restarts_final_stage_timer(self):
        schedule = PollSchedule()
        schedule.observe('processing', 45, 0)
        self.assertEqual(schedule.observe('processing', 45, 90), 2)
        schedule.observe('queued', 45, 100)
        self.assertGreater(schedule.observe('processing', 45, 101), 2)
        self.assertEqual(schedule.observe('processing', 45, 161), 3)
        self.assertEqual(schedule.observe('processing', 45, 191), 2)

    def test_only_continuous_processing_uses_the_processing_timer(self):
        schedule = PollSchedule()
        schedule.observe('processing', 0, 0)
        self.assertEqual(schedule.observe('queued', 0, 70), 6)
        self.assertGreater(schedule.observe('processing', 0, 71), 3)

    def test_completed_without_progress_uses_two_seconds(self):
        self.assertEqual(PollSchedule().observe('completed', 0, 0), 2)

    def test_configured_intervals_cannot_exceed_stage_limits(self):
        self.assertEqual(PollSchedule(60).observe('queued', 1, 0), 5)
        self.assertEqual(PollSchedule(.01).observe('processing', 99, 0), .05)
        self.assertEqual(PollSchedule(1).observe('processing', 99, 0), 1)


class ExecutionPollingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = SimpleNamespace(now=0.0, delays=[])
        self.events = []
        self.logs = []
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['paths']['output'] = str(self.root)
        config['workspace']['poll_timeout'] = 180
        config['download_settings']['aigc_metadata'] = False
        self.task = dict(task_id='already-paid', model='video-v3', images=[], prompt_name='fixture',
                         prompt_path='fixture.txt', status='processing', result_url='', attempts=[])
        owner = SimpleNamespace(config=config, control=SimpleNamespace(check=lambda: None, delay=self.delay),
                                pool=None, log=lambda message, level='info': self.logs.append(message),
                                tasks=[self.task], update_task=Mock(), progress_for=Mock(), scope='local-fixture',
                                submission_error=Mock(return_value=0), ledger=None)
        self.addCleanup(patch.stopall)
        patch('core.task_execution.time', SimpleNamespace(monotonic=lambda: self.clock.now)).start()
        self.execution = TaskExecution(owner, 0, self.task, 'video-v3')
        self.execution.recovered_id = 'already-paid'
        self.execution._resubmitted = False
        self.client = Mock()
        self.downloader = Mock(download_video=Mock(side_effect=self.download))

    def delay(self, seconds):
        self.clock.delays.append(seconds)
        self.clock.now += seconds

    def download(self, url, folder, filename, **kwargs):
        self.events.append(('download', self.clock.now))
        output = Path(folder) / filename
        output.write_bytes(b'local-video')
        self.clock.now += .25
        return str(output)

    def response(self, status='processing', progress=80, url=None):
        return dict(status=status, progress=progress, result_url=url, raw={})

    def run_responses(self, responses):
        iterator = iter(responses)
        def query(*args):
            result = next(iterator)
            self.events.append((result['status'], self.clock.now))
            return result
        self.client.query_task.side_effect = query
        self.execution._attempt('fixture', self.client, Mock(), self.downloader, Mock())

    def test_completion_immediately_downloads_and_records_the_timeline(self):
        self.run_responses([self.response(progress=80), self.response(progress=80),
                            self.response(progress=95), self.response(progress=100),
                            self.response('completed', 100, 'https://fixture.invalid/video')])
        self.assertEqual(self.clock.delays, [3, 3, 2, 2])
        self.assertEqual(self.events[-2:], [('completed', 10), ('download', 10)])
        task = self.execution.task
        for key in ('last_processing_at', 'first_completed_at', 'download_started_at', 'download_finished_at'):
            self.assertTrue(task[key], key)
        self.assertEqual(task['completion_detection_gap_seconds'], 2)
        self.assertEqual(task['completed_to_download_seconds'], 0)
        self.assertEqual(task['download_elapsed_seconds'], .25)
        self.assertTrue(any('检测到完成相对上次轮询耗时约2.00秒' in text for text in self.logs))
        self.assertTrue(any('待提供网页完成时刻' in text for text in self.logs))
        self.client.create_task.assert_not_called()

    def test_completed_without_url_rechecks_at_two_seconds(self):
        self.run_responses([self.response(progress=95), self.response('completed', 100),
                            self.response('completed', 100),
                            self.response('completed', 100, 'https://fixture.invalid/video')])
        self.assertEqual(self.clock.delays, [2, 2, 2])
        self.assertEqual(self.events[-2:], [('completed', 6), ('download', 6)])
        self.assertEqual(self.execution.task['completed_to_download_seconds'], 4)

    def test_missing_url_stops_at_forty_five_seconds_and_keeps_paid_id(self):
        self.client.query_task.return_value = self.response('completed', 100)
        with self.assertRaisesRegex(RuntimeError, '完成响应没有视频下载地址'):
            self.execution._attempt('fixture', self.client, Mock(), self.downloader, Mock())
        self.assertEqual(self.clock.now, 45)
        self.assertTrue(all(0 < seconds <= 2 for seconds in self.clock.delays))
        self.assertEqual(self.execution.task['task_id'], 'already-paid')
        self.downloader.download_video.assert_not_called()
        self.client.create_task.assert_not_called()

    def test_long_low_progress_processing_uses_fast_polling_without_a_busy_loop(self):
        self.run_responses([self.response(progress=45)] * 40 +
                           [self.response('completed', 100, 'https://fixture.invalid/video')])
        self.assertLessEqual(max(self.clock.delays), 15)
        self.assertEqual(self.clock.delays[-10:], [2] * 10)
        self.assertTrue(any('上游进度未更新' in text for text in self.logs))

    def test_slow_queries_add_network_time_without_increasing_final_wait(self):
        def query(*args):
            self.clock.now += 4.11
            completed = self.clock.now >= 120
            self.events.append(('completed' if completed else 'processing', self.clock.now))
            return self.response('completed', 100, 'https://fixture.invalid/video') if completed else self.response(progress=45)
        self.client.query_task.side_effect = query
        self.execution._attempt('fixture', self.client, Mock(), self.downloader, Mock())
        self.assertEqual(self.clock.delays[-2:], [2, 2])
        self.assertAlmostEqual(self.execution.task['completion_detection_gap_seconds'], 6.11)
        self.assertEqual(self.events[-1], ('download', self.events[-2][1]))
        self.client.create_task.assert_not_called()

    def test_poll_timeout_bounds_wait_and_preserves_task_id(self):
        self.execution.config['workspace']['poll_timeout'] = 7
        self.client.query_task.return_value = self.response(progress=20)
        with self.assertRaisesRegex(TimeoutError, '保留 task_id'):
            self.execution._attempt('fixture', self.client, Mock(), self.downloader, Mock())
        self.assertEqual(self.clock.now, 7)
        self.assertEqual(self.execution.task['task_id'], 'already-paid')
        self.downloader.download_video.assert_not_called()

    def test_remote_failure_never_downloads(self):
        self.client.query_task.return_value = self.response('failed', 80)
        with self.assertRaises(RemoteGenerationFailed):
            self.execution._attempt('fixture', self.client, Mock(), self.downloader, Mock())
        self.assertEqual(self.clock.delays, [])
        self.downloader.download_video.assert_not_called()

    def test_authentication_query_failure_is_not_treated_as_completion(self):
        self.client.query_task.side_effect = RequestError('unauthorized', 401)
        with self.assertRaises(RequestError):
            self.execution._attempt('fixture', self.client, Mock(), self.downloader, Mock())
        self.assertEqual(self.execution.task['task_id'], 'already-paid')
        self.downloader.download_video.assert_not_called()
        self.client.create_task.assert_not_called()


class QueryFreshnessTests(unittest.TestCase):
    def test_query_headers_are_forwarded_for_both_endpoint_families(self):
        for model, endpoint in (('video-v3', '/videos/'), ('video-v1', '/video/generations/')):
            with self.subTest(model=model):
                session = Mock()
                response = session.request.return_value
                response.status_code = 200
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.json.return_value = {'status': 'completed', 'progress': 100, 'result_url': 'https://fixture/video'}
                client = ApiClient('https://fixture.invalid', 'fixture-key', session=session)
                self.addCleanup(client.close)
                client.query_task('paid/id', model)
                call = session.request.call_args
                self.assertEqual(call.args, ('GET', 'https://fixture.invalid' + endpoint + 'paid%2Fid'))
                self.assertEqual(call.kwargs['headers']['Cache-Control'], 'no-cache')
                self.assertEqual(call.kwargs['headers']['Pragma'], 'no-cache')
                self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer fixture-key')

    def test_repeated_queries_always_issue_a_new_request(self):
        response = Mock(status_code=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.json.side_effect = [{'status': 'processing', 'progress': 95},
                                     {'status': 'completed', 'result_url': 'https://fixture/video'}]
        session = Mock()
        session.request.return_value = response
        client = ApiClient('https://fixture.invalid', 'fixture-key', session=session)
        self.addCleanup(client.close)
        self.assertEqual(client.query_task('paid', 'video-v3')['status'], 'processing')
        self.assertEqual(client.query_task('paid', 'video-v3')['status'], 'completed')
        self.assertEqual(session.request.call_count, 2)
