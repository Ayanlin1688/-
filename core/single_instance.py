"""单实例守卫：同一数据目录只允许一个应用实例运行。

- 唯一键按配置目录派生：不同数据目录（如便携版与正式版、隔离测试）可并存互不干扰。
- 已在运行时，新实例请求既有实例前置窗口后自行退出，避免定时任务双触发、
  配置写入互相覆盖与界面状态发散。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance(QObject):
    activation_requested = pyqtSignal()

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = str(key)
        self.server = None

    @staticmethod
    def key_for(config_dir):
        """按数据目录派生稳定唯一键（同目录互斥、异目录并存）。"""
        digest = hashlib.sha1(str(Path(config_dir).resolve()).encode('utf-8')).hexdigest()[:12]
        return f'YanlinMatrix-{digest}'

    def acquire(self):
        """返回 True = 本进程成为唯一实例；False = 已有实例（已请求其前置窗口）。"""
        probe = QLocalSocket()
        probe.connectToServer(self.key)
        if probe.waitForConnected(300):
            probe.write(b'activate')
            probe.flush()
            probe.waitForBytesWritten(500)
            probe.disconnectFromServer()
            probe.abort()
            return False
        probe.abort()
        # 没有响应：清理陈旧端点（上次崩溃残留）后监听。
        QLocalServer.removeServer(self.key)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept_connections)
        if not self.server.listen(self.key):
            # 极端竞态（同名端点刚被他人抢占）：再探测一次，能连上就视为已有实例。
            again = QLocalSocket()
            again.connectToServer(self.key)
            connected = again.waitForConnected(300)
            again.abort()
            if connected:
                return False
            QLocalServer.removeServer(self.key)
            self.server.listen(self.key)
        return True

    def _accept_connections(self):
        while self.server is not None and self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                break
            try:
                socket.disconnected.connect(socket.deleteLater)
            except (RuntimeError, TypeError):
                pass
            self.activation_requested.emit()

    def close(self):
        """释放监听端点（测试与有序退出用）。"""
        if self.server is not None:
            try:
                self.server.close()
            except RuntimeError:
                pass
            self.server = None
        QLocalServer.removeServer(self.key)
