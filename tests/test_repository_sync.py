import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.repository_sync import RepositorySync, GitSyncError


class RepositorySyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'; self.root.mkdir()
        self.remote = Path(self.temp.name) / 'remote.git'
        self.mirror = Path(self.temp.name) / 'mirror.git'
        self.git('init', '--bare', str(self.remote), cwd=self.root)
        self.git('init', '--bare', str(self.mirror), cwd=self.root)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('remote', 'add', 'ayanlin', str(self.mirror))
        self.git('config', 'core.autocrlf', 'false')
        ignore = Path(__file__).resolve().parent.parent / '.gitignore'
        (self.root / '.gitignore').write_bytes(ignore.read_bytes())
        (self.root / 'main.py').write_text('print("ready")\n', encoding='utf-8')
        (self.root / 'config.json').write_text('{"api_key":"local-secret-key"}', encoding='utf-8')
        (self.root / 'example.mp4').write_bytes(b'local-video')
        self.sync = RepositorySync(self.root, secrets=('local-secret-key',), expected_origin=str(self.remote),
                                    expected_mirror=str(self.mirror), retry_delay=0)

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd or self.root, stderr=subprocess.STDOUT).decode('utf-8').strip()

    def assert_remote_commit(self, commit):
        for remote in ('origin', 'ayanlin'):
            self.assertEqual(self.git('ls-remote', remote, 'refs/heads/main').split()[0], commit)

    def assert_remotes_empty(self):
        for remote in ('origin', 'ayanlin'):
            self.assertEqual(self.git('ls-remote', remote, 'refs/heads/main'), '')

    def test_commits_and_pushes_only_code_and_can_push_unchanged_tree(self):
        result = self.sync.sync('feat: 测试同步完整代码')
        self.assertEqual(result['commit'], self.git('rev-parse', 'HEAD'))
        self.assert_remote_commit(result['commit'])
        self.assertEqual(result['remotes'], ('origin', 'ayanlin'))
        self.assertEqual(self.git('rev-parse', '--abbrev-ref', '@{upstream}'), 'origin/main')
        self.assertEqual(set(self.git('ls-files').splitlines()), {'.gitignore', 'main.py'})
        self.assertFalse(self.sync.sync('refactor: 同步代码')['changed'])

    def test_rejects_known_secret_in_source_before_commit_or_push(self):
        (self.root / 'main.py').write_text('key="local-secret-key"\n', encoding='utf-8')
        with self.assertRaises(GitSyncError):
            self.sync.sync('fix: 阻止泄露')
        self.assert_remotes_empty()

    def test_rejects_forced_sensitive_staging(self):
        self.git('add', '-f', 'config.json')
        with self.assertRaises(GitSyncError):
            self.sync.sync('fix: 阻止敏感文件')
        self.assert_remotes_empty()

    def test_model_cache_never_reaches_remote_even_when_forced_into_index(self):
        (self.root / 'models_cache.json').write_text('{"models":{}}', encoding='utf-8')
        result = self.sync.sync('feat: 测试模型缓存排除')
        self.assertNotIn('models_cache.json', self.git('ls-files').splitlines())
        self.git('add', '-f', 'models_cache.json')
        with self.assertRaises(GitSyncError):
            self.sync.sync('fix: 阻止模型缓存强制暂存')
        self.assert_remote_commit(result['commit'])

    def test_submission_ledgers_are_ignored_and_forced_staging_is_blocked(self):
        names = ('submissions.sqlite3', 'submissions.sqlite3-wal', '.storyboard-submissions.sqlite3')
        for name in names:
            (self.root / name).write_bytes(b'private submission audit')
        result = self.sync.sync('feat: 测试提交账本排除')
        tracked = self.git('ls-files').splitlines()
        self.assertTrue(all(name not in tracked for name in names))
        self.git('add', '-f', *names)
        with self.assertRaises(GitSyncError):
            self.sync.sync('fix: 阻止账本强制暂存')
        self.assert_remote_commit(result['commit'])

    def test_push_retries_twice_then_both_remotes_match(self):
        real = self.sync._git
        attempts = []
        def intermittent(args, **kwargs):
            if args[0] == 'push':
                attempts.append(args[2] if args[1] == '-u' else args[1])
                if len(attempts) < 3:
                    raise GitSyncError('temporary network failure')
            return real(args, **kwargs)
        with patch.object(self.sync, '_git', side_effect=intermittent):
            result = self.sync.sync('feat: 重试同步')
        self.assertEqual(attempts, ['origin', 'origin', 'origin', 'ayanlin'])
        self.assertEqual(result['attempts'], 3)
        self.assert_remote_commit(result['commit'])

    def test_failed_mirror_retries_without_new_commit_or_second_origin_push(self):
        real = self.sync._git
        pushes, commits = [], []
        def flaky(args, **kwargs):
            if args[0] == 'commit':
                commits.append(args[-1])
            if args[0] == 'push':
                remote = args[2] if args[1] == '-u' else args[1]
                pushes.append(remote)
                if remote == 'ayanlin' and pushes.count('ayanlin') == 1:
                    raise GitSyncError('temporary mirror failure')
            return real(args, **kwargs)
        with patch.object(self.sync, '_git', side_effect=flaky):
            result = self.sync.sync('fix: 镜像重试不重复提交')
        self.assertEqual(commits, ['fix: 镜像重试不重复提交'])
        self.assertEqual(pushes, ['origin', 'ayanlin', 'ayanlin'])
        self.assertEqual(result['attempts'], 2)
        self.assert_remote_commit(result['commit'])
        self.assertEqual(self.git('rev-parse', '--abbrev-ref', '@{upstream}'), 'origin/main')

    def test_missing_mirror_remote_is_added_without_changing_origin(self):
        self.git('remote', 'remove', 'ayanlin')
        result = self.sync.sync('feat: 自动添加第二仓库')
        self.assertEqual(self.git('remote', 'get-url', 'origin'), str(self.remote))
        self.assertEqual(self.git('remote', 'get-url', 'ayanlin'), str(self.mirror))
        self.assert_remote_commit(result['commit'])
        self.assertEqual(self.git('rev-parse', '--abbrev-ref', '@{upstream}'), 'origin/main')

    def test_mirror_url_mismatch_stops_before_commit_or_push(self):
        self.git('remote', 'set-url', 'ayanlin', 'https://example.invalid/other.git')
        with self.assertRaises(GitSyncError) as caught:
            self.sync.sync('fix: 阻止错误镜像')
        self.assertIn('ayanlin', str(caught.exception))
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main'), '')
        failed = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], cwd=self.root, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)

    def test_authorized_repositories_keep_origin_and_name_the_mirror(self):
        from core.repository_sync import GITHUB_REPOSITORY, GITHUB_MIRROR_REPOSITORY, MIRROR_REMOTE_NAME
        self.assertEqual(GITHUB_REPOSITORY, 'https://github.com/admin11044/StoryboardVideoStudio.git')
        self.assertEqual(GITHUB_MIRROR_REPOSITORY, 'https://github.com/Ayanlin1688/-.git')
        self.assertEqual(MIRROR_REMOTE_NAME, 'ayanlin')

    def test_secret_deleted_at_head_is_still_blocked_in_pending_commit_history(self):
        (self.root / 'main.py').write_text('key="local-secret-key"\n', encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-m', 'test: unsafe ancestor')
        (self.root / 'main.py').write_text('print("clean head")\n', encoding='utf-8')
        self.git('add', '.'); self.git('commit', '-m', 'test: remove key')
        with self.assertRaises(GitSyncError):
            self.sync.sync('fix: 阻止历史凭据')
        self.assert_remotes_empty()

    def test_leading_space_filename_is_checked_without_normalizing_its_name(self):
        (self.root / ' secret.py').write_text('key="local-secret-key"\n', encoding='utf-8')
        with self.assertRaises(GitSyncError):
            self.sync.sync('fix: 检查特殊文件名')
        self.assert_remotes_empty()
