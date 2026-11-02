"""pytest 全局设置：每个测试都跑在隔离的环境里。

⚠️ 为什么需要这个文件

    2026-11-02 发现：`python -m pytest tests/` 全量跑会挂 2 条，
    而 `python tests/run_all.py`（每个文件单独一个进程）全过。

    根因：有些测试文件在**模块导入阶段**就改了 os.environ：

        # test_perm_verify.py 开头
        os.environ["APPDATA"] = os.path.join(TMP, "roaming")
        os.environ["LOCALAPPDATA"] = os.path.join(TMP, "local")

    pytest 收集测试时会把所有文件 import 一遍 —— 于是这个改动
    影响到了**后面所有测试**（包括别的文件里的）。

    结果：
        前一个文件把 APPDATA 指到一个空目录 →
        后一个文件读守卫配置读到空 → 行为变了 → 断言失败

    而 run_all.py 是每个文件启动一个子进程，天然隔离，所以看不出来。
    CI 用的也是 run_all.py，所以这个问题一直没被发现。

现在怎么做

    1. 每个测试函数跑之前，APPDATA / LOCALAPPDATA 指向一个**新的空目录**
    2. 跑完之后恢复原值
    3. 守卫状态也要清（它有跨进程落盘，不清理会串）

    这样无论测试文件自己怎么改环境变量，都不影响别人。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 保存进程启动时最原始的值
_ORIG = {
    "APPDATA": os.environ.get("APPDATA"),
    "LOCALAPPDATA": os.environ.get("LOCALAPPDATA"),
}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """每个测试一个干净的用户目录。

    autouse=True：不用每个测试自己声明，全部自动生效。

    ⚠️ 用 monkeypatch.setenv 而不是直接改 os.environ：
        pytest 会在这个测试结束后自动恢复，不用我们手动还原。
    """
    roaming = tmp_path / "roaming"
    local = tmp_path / "local"
    roaming.mkdir(parents=True, exist_ok=True)
    local.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    yield

    # 测试结束后把守卫的**内存状态**也清掉。
    #
    # 守卫把状态落在盘上（guard.json / lock_state.json），
    # 而内存里还有一份。每个测试的盘是新的，但内存是共享的 ——
    # 不清的话，上一个测试锁死的状态会带到下一个。
    try:
        from guard import guard as _g

        _g.reset_all()
    except Exception:  # noqa: BLE001
        pass
