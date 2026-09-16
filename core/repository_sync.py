"""Guarded code-only Git commits and bounded pushes using locally stored Git auth."""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .log_redaction import redact_text

GITHUB_REPOSITORY = 'https://github.com/admin11044/StoryboardVideoStudio.git'


class GitSyncError(RuntimeError):
    pass


def repository_secrets(config):
    """Protect credentials for both the selected provider and saved stations."""
    sections = [config.get('api')]
    stations = config.get('stations')
    if isinstance(stations, list):
        sections.extend(stations)
    secrets = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        for key in ('api_key', 'upload_api_key'):
            value = section.get(key)
            if isinstance(value, str) and value.strip():
                secrets.append(value.strip())
    return tuple(dict.fromkeys(secrets))


class RepositorySync:
    _lock = threading.Lock()

    def __init__(self, root=None, secrets=(), log=None, expected_origin=GITHUB_REPOSITORY, retry_delay=2):
        self.root = Path(root or Path(__file__).resolve().parent.parent).resolve()
        self.secrets = tuple(secret for secret in secrets if secret)
        self.log = log or (lambda *_: None)
        self.expected_origin = expected_origin
        self.retry_delay = retry_delay

    def _git(self, args, timeout=45, allow_codes=(0,), raw=False):
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never', GCM_GUI_PROMPT='0')
        try:
            process = subprocess.run(['git', *args], cwd=self.root, env=env, capture_output=True,
                                     timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitSyncError(redact_text(f'Git执行失败：{error}', self.secrets)) from None
        if process.returncode not in allow_codes:
            output = (process.stderr or process.stdout).decode('utf-8', errors='replace').strip()
            raise GitSyncError(redact_text(output or f'Git退出码{process.returncode}', self.secrets))
        output = process.stdout.decode('utf-8', errors='replace')
        return output if raw else output.rstrip('\r\n')

    @staticmethod
    def _sensitive_path(name):
        path = Path(name)
        basename = path.name.casefold()
        folders = {part.casefold() for part in path.parts}
        return (basename in {'config.json', 'models_cache.json', 'models_cache.json.tmp'} or basename.startswith('.env') or
                basename.startswith(('submissions.sqlite3', '.storyboard-submissions.sqlite3', 'history.sqlite3')) or
                path.suffix.casefold() in {'.mp4', '.mov', '.avi', '.log', '.pyc', '.pyo', '.key', '.pem'} or
                bool(folders & {'artifacts', 'screenshots', 'temp', 'tmp', '__pycache__', '.vscode', '.idea', '.venv', 'venv', '视频输出目录', '视频保存目录'}))

    def _check_snapshot(self, staged=False):
        command = ['ls-files', '-z'] if staged else ['ls-files', '--cached', '--others', '--exclude-standard', '-z']
        names = self._git(command).split('\0')
        for name in filter(None, names):
            if self._sensitive_path(name):
                raise GitSyncError('已阻止同步敏感/生成文件：' + name)
            path = self.root / name
            if staged:
                content = self._git(['show', ':' + name], raw=True)
            else:
                if not path.exists():
                    continue
                if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                    raise GitSyncError('已阻止同步项目外链接：' + name)
                content = path.read_bytes().decode('utf-8', errors='replace')
            self._check_content(name, content)

    def _check_content(self, name, content):
        for secret in self.secrets:
            forms = {secret, json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret, ensure_ascii=True)[1:-1]}
            if any(value in content for value in forms):
                raise GitSyncError('已阻止同步含本地凭据的文件：' + name)
        if re.search(r'\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b', content):
            raise GitSyncError('已阻止同步疑似API Key或GitHub凭据的文件：' + name)

    def _check_pending_history(self):
        base = self._git(['rev-parse', '--verify', '--quiet', 'refs/remotes/origin/main'], allow_codes=(0, 1))
        commits = self._git(['rev-list', 'HEAD', *(['^' + base] if base else [])]).splitlines()
        checked = set()
        for commit in commits:
            entries = self._git(['ls-tree', '-r', '-z', commit], raw=True).split('\0')
            for entry in filter(None, entries):
                metadata, name = entry.split('\t', 1)
                mode, kind, oid = metadata.split()
                if self._sensitive_path(name):
                    raise GitSyncError('已阻止待推送历史中的敏感文件：' + name + ' @ ' + commit[:12])
                if kind != 'blob' or mode == '120000':
                    raise GitSyncError('已阻止待推送历史中的链接/子模块：' + name)
                if oid not in checked:
                    self._check_content(name + ' @ ' + commit[:12], self._git(['cat-file', 'blob', oid], raw=True))
                    checked.add(oid)

    def sync(self, message='refactor: 同步软件代码修改'):
        if not self._lock.acquire(blocking=False):
            raise GitSyncError('已有GitHub同步正在运行')
        try:
            root = Path(self._git(['rev-parse', '--show-toplevel'])).resolve()
            if root != self.root:
                raise GitSyncError('同步目录必须是项目Git根目录')
            if self._git(['branch', '--show-current']) != 'main':
                raise GitSyncError('请先切换到main分支再同步')
            if self._git(['remote', 'get-url', 'origin']).rstrip('/') != self.expected_origin.rstrip('/'):
                raise GitSyncError('origin与项目授权仓库不一致，停止同步')
            for name in ('config.json', 'models_cache.json', 'history.sqlite3', 'history.sqlite3-wal', 'history.sqlite3-shm',
                         'sample.mp4', 'sample.log', '__pycache__/sample.pyc', 'screenshots/sample.png', 'temp/sample.txt'):
                self._git(['check-ignore', '--no-index', name])
            self._check_snapshot()
            self._git(['add', '.'])
            self._check_snapshot(staged=True)
            changed = bool(self._git(['diff', '--cached', '--name-only']))
            if changed:
                if not re.match(r'^(feat|fix|refactor|docs):\s*\S', message):
                    raise GitSyncError('提交说明需使用feat/fix/refactor/docs前缀')
                self._git(['commit', '-m', message])
            commit = self._git(['rev-parse', 'HEAD'])
            self._check_pending_history()
            for attempt in range(3):
                try:
                    self._git(['push', '-u', 'origin', 'main'], timeout=90)
                    remote = self._git(['ls-remote', 'origin', 'refs/heads/main'])
                    if not remote or remote.split()[0] != commit:
                        raise GitSyncError('远端commit尚未与本地一致')
                    self.log('已同步到GitHub，最新commit: ' + commit[:12], 'success')
                    return dict(commit=commit, changed=changed, attempts=attempt + 1)
                except GitSyncError as error:
                    if attempt == 2:
                        raise GitSyncError(f'推送失败（共3次尝试）：{error}。本地提交已保留；请检查网络/GitHub登录后重试。') from None
                    self.log(f'GitHub推送失败，准备重试{attempt+1}/2：{error}', 'warning')
                    time.sleep(self.retry_delay)
        finally:
            self._lock.release()
