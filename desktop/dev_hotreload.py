"""开发用热部署：盯着桌面端源码，一保存就自动重启窗口，不用手动关了再开。

用法（在 venv 里）：
    python desktop/dev_hotreload.py

只监听 desktop 目录下的 .py。改完按 Ctrl+S 保存，窗口自己会刷新。
这是开发辅助，不影响正式的 start_desktop.pyw 启动流程。
"""
import os
import subprocess
import sys

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer
from PySide6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_ENTRY = os.path.join(ROOT, "app.py")
PYTHON = sys.executable


def _py_files() -> list:
    out = []
    for dirpath, _dirs, names in os.walk(ROOT):
        for n in names:
            if n.endswith(".py") and n != os.path.basename(__file__):
                out.append(os.path.join(dirpath, n))
    return out


class HotReloader(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.proc = None
        self.watcher = QFileSystemWatcher()
        files = _py_files()
        self.watcher.addPaths(files)
        # 防抖：一次保存可能触发多个信号，攒 300ms 再重启
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self.restart)
        self.watcher.fileChanged.connect(self._on_change)
        self.start()

    def _on_change(self, _path: str) -> None:
        self._debounce.start()

    def start(self) -> None:
        self.proc = subprocess.Popen([PYTHON, APP_ENTRY], cwd=os.path.dirname(ROOT))

    def restart(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        # 重新扫一遍文件（新建的文件也要盯）
        existing = set(self.watcher.files())
        for f in _py_files():
            if f not in existing:
                self.watcher.addPath(f)
        self.start()


def main() -> int:
    app = QApplication(sys.argv)
    reloader = HotReloader()
    app.aboutToQuit.connect(reloader.restart)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
