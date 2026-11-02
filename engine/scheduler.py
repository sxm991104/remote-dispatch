"""并发控制：每套模型配置各跑各的，满了排队。

设计要点
    多套配置不是"切换"关系，是同时都在干活：
        DeepSeek 那套可以同时跑 3 个任务
        Kimi 那套同时跑 5 个
        千问那套同时跑 1 个

    每套的并发上限由用户自己填（受服务商限额约束）。

排队怎么跑
    派活时如果这套满了，任务状态标成 queued 并记下排队位置。
    然后有一个后台线程定期检查：哪套有空位了，就把最早排队的那个拉起来。

    为什么不让用户自己盯：
        用户派完就关窗口了，没人会回来点"重试"。
        队列得自己动。

和限流的关系
    这里管的是"同时跑几个任务"（并发数）。
    engine/ratelimit.py 管的是"每分钟能发几次请求"（频率）。
    两个是不同的东西：
        并发 5 个任务，每个任务每轮发 1 次请求 → 可能一分钟发几十次 → 撞限流
        并发 1 个任务，但任务很长 → 一分钟可能只发几次 → 不撞限流
    所以要分开管。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

# ⚠️ 这个文件在 engine/ 子目录里，桥的根目录是它上一级。
#
#    踩过：原来写 dirname(__file__)，算出来是 engine/ 自己，
#    于是去找 engine/tasks.jsonl —— 那个文件不存在，
#    读出来永远是空列表，并发控制完全失效（等于没有上限）。
#
#    所以要用 dirname(dirname(__file__))。
BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 每套配置现在有几个任务在跑
_running: dict[str, int] = {}
_lock = threading.Lock()


def _tasks_path() -> str:
    return os.path.join(BRIDGE_DIR, "tasks.jsonl")


def _read_tasks() -> list[dict]:
    """读任务列表。跟 bridge 那边的格式一致。"""
    p = _tasks_path()
    out = []
    if not os.path.exists(p):
        return out
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def running_count(profile_id: str) -> int:
    """这套配置现在占了几个位子。

    ⚠️ 这里数的不只是 running，还要算上刚派出去、马上要跑的 pending。
        踩过的坑：原来只数 running，于是连着派 3 个任务时，
        它们全是 pending 状态，检查都通过 → 3 个一起起来了，
        并发上限等于没有。

        pending 就是"已经决定要跑了、只是进程还没起来"，
        它必须占位，否则并发控制形同虚设。

    不看内存里的计数——进程重启就丢了，直接从文件数。
    """
    n = 0
    for t in _read_tasks():
        if t.get("profile") != profile_id:
            continue
        if t.get("status") in ("running", "pending"):
            n += 1
    return n


# pending 最晚多久之内要变成 running。
#
# 为什么要有一个上限：
#     pending 占并发位子是对的（不然并发控制形同虚设）。
#     但如果 worker 起不来——崩溃、被杀、断电——
#     它会永远停在 pending，也就永远占一个位子。
#
#     派满 N 个 → 后面全排队 → pump 找不到空位 → 永远排着。
#     表现就是"活完全派不出去"，而且看不出原因。
#
#     实测：仓库里 3 条测试残留的 pending 把并发 3 那套占满，
#     e2e 派的两条只拿到 queued，30 次轮询一直不动。
PENDING_DEADLINE = 120      # 2 分钟还没起来 = 这条路断了
QUEUED_DEADLINE = 1800      # 排队 30 分钟还没轮到 = 前面堵死了


def reap_stuck(limit_pending: int = PENDING_DEADLINE,
               limit_queued: int = QUEUED_DEADLINE) -> int:
    """把卡死的 pending / queued 标成 error。返回处理了几条。

    判定依据是**时间**，不是心跳——
    这个阶段还没 worker 进程，没有心跳可看。

    判死之后：
        状态改成 error，原因写清楚
        于是它不再占并发位子，后面的任务能跑起来
    """
    import datetime as _dt

    now = _dt.datetime.now().astimezone()
    changed = 0

    for t in _read_tasks():
        st = t.get("status")
        if st not in ("pending", "queued"):
            continue

        # 从哪一刻开始算
        stamp = t.get("created_at") or t.get("updated_at") or ""
        try:
            t0 = _dt.datetime.fromisoformat(str(stamp))
            if t0.tzinfo is None:
                t0 = t0.astimezone()
        except (ValueError, TypeError):
            continue

        age = (now - t0).total_seconds()
        limit = limit_pending if st == "pending" else limit_queued
        if age <= limit:
            continue

        why = (
            f"派下去 {int(age // 60)} 分钟了还没开始跑。"
            "多半是 worker 起不来（崩溃、被杀、或者机器重启过）。"
            "这条已经标成失败，不再占并发位子——重新派一次就行。"
        )
        try:
            _update_task_by_id(
                t.get("id") or "",
                status="error",
                error=why,
                finished_at=now.isoformat(timespec="seconds"),
            )
            changed += 1
        except Exception:  # noqa: BLE001
            continue

    return changed


def _update_task_by_id(task_id: str, **fields) -> None:
    """改一条任务。单独写一个，避免和 bridge 的循环依赖。

    ⚠️ 不能在这里 import core.bridge——scheduler 是被它间接调用的，
       顶层 import 会成环。所以延迟导入。
    """
    try:
        from core import bridge

        bridge._update_task(task_id, **fields)
    except Exception:  # noqa: BLE001
        pass


def has_slot(profile_id: str, concurrency: int) -> bool:
    """这套还有没有空位。"""
    return running_count(profile_id) < max(1, concurrency)


def queue_position(profile_id: str, task_id: str) -> int:
    """这个任务在这套的队列里排第几（1 开始）。没排队返回 0。"""
    queued = [
        t for t in _read_tasks()
        if t.get("profile") == profile_id and t.get("status") == "queued"
    ]
    # 按创建时间排，最早的在前面
    queued.sort(key=lambda t: t.get("created_at") or "")
    for i, t in enumerate(queued, 1):
        if t.get("id") == task_id:
            return i
    return 0


def _worker_log_path(task_id: str) -> str:
    """每个任务的 worker 日志。出问题能翻。"""
    safe = "".join(
        c if (c.isalnum() or c in "-_.") else "_" for c in str(task_id)
    ) or "task"
    return os.path.join(BRIDGE_DIR, "workspaces", safe, "worker.log")

def start_worker(task_id: str) -> None:
    """起一个后台进程真干活。跟 api_server 里那个一样。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    flags = 0x08000000 | 0x00000008 if sys.platform == "win32" else 0
    # ⚠️ 输出别扔 DEVNULL。
    #
    #    原来 stdout/stderr 全是 DEVNULL。worker 挂了之后
    #    **一点痕迹都没有** —— 任务状态停在 running，没人知道为什么。
    #
    #    实测：全新环境派活，worker 因为找不到模型挂掉，
    #    而 tasks.jsonl 里状态永远 running，界面一直转圈。
    #
    #    现在写到 workspaces/<id>/worker.log 里，出问题能看。
    log_path = _worker_log_path(task_id)
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        _logf = open(log_path, "ab")
    except OSError:
        _logf = subprocess.DEVNULL

    try:
        subprocess.Popen(
            [sys.executable, os.path.join(BRIDGE_DIR, "core", "bridge.py"),
             "run", "--id", task_id, "--timeout", "1800"],
            cwd=BRIDGE_DIR,
            env=env,
            stdout=_logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
    except OSError as e:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[scheduler] 起不来：{e}\n")
        except OSError:
            pass
    finally:
        # 父进程这边不用持有它，子进程会继承句柄
        if _logf not in (None, subprocess.DEVNULL):
            try:
                _logf.close()
            except OSError:
                pass


def pump() -> int:
    """把排队的任务拉起来。返回这次启动了几个。

    从每套配置的队列里，按创建时间取最早的，
    空位有几个就拉几个。
    """


    # 先把卡死的清掉——不然它们永远占着并发位子，
    # 后面的任务排到天荒地老。
    reap_stuck()
    import engine.profiles as P

    started = 0
    profiles = P.load_all().get("profiles") or []

    for prof in profiles:
        pid = prof.get("id") or ""
        if not prof.get("enabled"):
            continue
        if not (prof.get("base_url") and prof.get("api_key") and prof.get("model")):
            continue

        limit = int(prof.get("concurrency") or 1)
        now_running = running_count(pid)
        free = max(0, limit - now_running)
        if free <= 0:
            continue

        queued = [
            t for t in _read_tasks()
            if t.get("profile") == pid and t.get("status") == "queued"
        ]
        queued.sort(key=lambda t: t.get("created_at") or "")

        for t in queued[:free]:
            start_worker(t.get("id") or "")
            started += 1
            time.sleep(0.2)   # 别一下子全拉起来

    return started


def pump_loop(interval: float = 5.0, stop_event: threading.Event | None = None) -> None:
    """后台线程：定期看有没有空位。

    每 5 秒看一次就够了——任务一般要跑几十秒，
    查太勤只是在读文件。
    """
    while True:
        if stop_event and stop_event.is_set():
            return
        try:
            pump()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)


def start_pump_in_background(interval: float = 5.0) -> threading.Thread:
    """在后台起一个 pump 线程。返回线程对象，方便测试时停。"""
    t = threading.Thread(
        target=pump_loop, args=(interval,), daemon=True, name="pump"
    )
    t.start()
    return t
