"""安全模式：用户选哪一套防护。

## 三种模式

    模式一  严格（strict）
        · 动作守卫        命令词表 + 路径 + 权限
        · 影子执行        改动先落影子
        · 审计员          看改动对不对
        · 验证码          危险操作要码
        · 版本回退        **不要**

        "我不让它乱来" —— 默认

    模式二  回滚（rollback）
        · 动作守卫        **不要**
        · 影子执行        **Windows 不要 / Linux 要**
        · 审计员          **不要**
        · 验证码          **不要**
        · 版本回退        **要**

        "我让它快，出事我自己退回去"

    模式三  全开（both）
        全部都要

## ⚠️ 为什么 Linux 的模式二要影子层

    Windows 的回退靠**系统卷影副本**（跟我们的影子无关）。
    Linux 的 ext4 没有系统快照 ——
    版本历史**就是**影子层给的（overlay 的每一层）。

    所以 Linux 上：
        不要影子 = 不要回退
    Windows 上：
        不要影子也能回退（系统快照在）

    这不是"多余的依赖"，是两个平台的客观差异。

## 存在哪

    跟 auditor.json / guard.json 同一个目录。
    文件名 security_mode.json
"""

from __future__ import annotations

import json
import os
import sys

MODE_STRICT = "strict"       # 模式一：严格
MODE_ROLLBACK = "rollback"   # 模式二：回滚
MODE_BOTH = "both"           # 模式三：全开

DEFAULT = MODE_STRICT

# 每种模式下，各个防护开不开
#
#   shadow  要不要影子执行
#   guard   要不要动作守卫
#   audit   要不要审计员
#   code    要不要验证码
#   exfil   要不要外传拦截
#   rewind  要不要版本回退
FEATURES = {
    MODE_STRICT: {
        "shadow": True,
        "guard": True,
        "audit": True,
        "code": True,
        "exfil": True,
        "rewind": False,
    },
    MODE_ROLLBACK: {
        # ⚠️ shadow 在运行时按平台再判一次 —— 见 features()
        "shadow": True,
        "guard": False,
        "audit": False,
        "code": False,
        # ⚠️ 这一档**不做外传拦截**。
        #
        #    它整个思路就是"不拦不审不要码，出事了退回去"。
        #    加一道拦截就自相矛盾了。
        "exfil": False,
        "rewind": True,
    },
    MODE_BOTH: {
        "shadow": True,
        "guard": True,
        "audit": True,
        "code": True,
        "exfil": True,
        "rewind": True,
    },
}

MODE_LABEL = {
    MODE_STRICT: "严格防护",
    MODE_ROLLBACK: "出事可回退",
    MODE_BOTH: "全开",
}


def _path() -> str:
    from tools import userdirs

    return userdirs.config_file("security_mode.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save(d: dict) -> None:
    p = _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError:
        pass


def get() -> str:
    """当前模式。"""
    m = _load().get("mode") or DEFAULT
    if m not in FEATURES:
        return DEFAULT
    return m


def set_mode(mode: str) -> tuple[bool, str]:
    """改模式。返回 (成功没, 说明)。"""
    if mode not in FEATURES:
        return False, f"没有这个模式：{mode}"

    d = _load()
    d["mode"] = mode
    _save(d)
    return True, f"已切到「{MODE_LABEL.get(mode, mode)}」"


def features(mode: str | None = None) -> dict:
    """当前模式启用了哪些防护。

    ⚠️ shadow 要按平台再判一次：

        Windows 模式二  不要影子（回退靠系统快照）
        Linux  模式二   **要**影子（回退就是影子给的）
    """
    m = mode or get()
    f = dict(FEATURES.get(m, FEATURES[DEFAULT]))

    # 模式二在 Windows 上不需要影子
    if m == MODE_ROLLBACK and sys.platform == "win32":
        f["shadow"] = False

    return f


def wants(feature: str, mode: str | None = None) -> bool:
    """这个防护要不要开。

        wants("guard")    要不要动作守卫
        wants("shadow")   要不要影子执行
        wants("rewind")   要不要版本回退
    """
    return bool(features(mode).get(feature))


def describe(mode: str | None = None) -> str:
    """一句话说清楚当前模式。给界面用。"""
    m = mode or get()
    f = features(m)
    label = MODE_LABEL.get(m, m)

    on = []
    if f.get("guard"):
        on.append("动作守卫")
    if f.get("shadow"):
        on.append("影子执行")
    if f.get("audit"):
        on.append("审计员")
    if f.get("code"):
        on.append("验证码")
    if f.get("rewind"):
        on.append("版本回退")

    return f"「{label}」：{' + '.join(on) if on else '什么都不开'}"


def status() -> dict:
    """给接口用。"""
    m = get()
    return {
        "mode": m,
        "label": MODE_LABEL.get(m, m),
        "features": features(m),
        "describe": describe(m),
        # Windows 上模式二没有影子，Linux 上有 —— 说清楚免得用户困惑
        "platform": sys.platform,
    }
