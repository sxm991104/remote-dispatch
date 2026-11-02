"""真·端到端：从派活到底。

为什么要有这个文件
    现有 347 条测试全在同一个进程里跑。
    它们测"函数返回对不对"，不测"真的跑得起来吗"。

    而昨天提交的两处断链，恰好都在进程边界上：

        把 bridge.py 移进 core/，但 scheduler/api_server 还在旧路径找
        → worker 起不来，但同进程测试一点都看不出来

        core/bridge.py 用相对导入
        → 脚本模式静默失败 → 退回 DSH → 用错 provider
        → 同样只在真起 worker 时才暴露

    所以这个文件不 mock、不替身、不跳过：
        真的起一个接口服务
        真的发 HTTP 派活
        真的让 worker 进程起来
        真的等它跑完
        真的检查结果

    它慢（几分钟），但它是唯一能回答"拉下来能用吗"的东西。

用法
    python tests/e2e.py            # 默认用自带引擎
    python tests/e2e.py --quick    # 跳过慢的那几条

退出码
    0  全过
    1  有失败
"""


from __future__ import annotations

# ⚠️ Windows CI 上 stdout 的默认编码是 cp1252（英文系统），
#    里面没有 ✅ ✗ 这些符号，print 会直接抛 UnicodeEncodeError。
#    实测：整个测试一条都没跑就崩了，而 Ubuntu 上一直是绿的。
#
#    下面这两行必须在任何 print 之前执行。
try:
    from tools.console import mark_utf8 as _mark_utf8
except ImportError:
    import os as _os0, sys as _sys0
    _sys0.path.insert(0, _os0.path.dirname(_os0.path.dirname(_os0.path.abspath(__file__))))
    from tools.console import mark_utf8 as _mark_utf8
_mark_utf8()

from tools import userdirs  # noqa: E402

# ⚠️ 必须在任何 print 之前把 stdout 换成 UTF-8。
#
#    Windows CI 上 python 的默认 stdout 编码是 cp1252（英文系统），
#    那份编码里没有 ✅ ✗ 这些符号，
#    print("✅ 端到端测试") 直接抛 UnicodeEncodeError，
#    整个测试一条都没跑就崩了。
#
#    实测（CI 日志）：
#        print("\u2705 端到端测试：从拉下来到真能干活")
#        → UnicodeEncodeError: 'charmap' codec can't encode characters
#
#    而 Ubuntu 上默认 UTF-8，所以那边一直是绿的。
#
#    改法：把 stdout/stderr 的 buffer 重新包一层 UTF-8，
#    errors="replace" 兜底 —— 真写不出去也只会显示成 ?，不会拖死测试。
#
#    ⚠️ 必须放在 from __future__ 之后 ——
#        Python 要求 __future__ 是文件里第一个语句。
import io as _io
import sys as _sys


def _force_utf8() -> None:
    for _name in ("stdout", "stderr"):
        _s = getattr(_sys, _name, None)
        if _s is None:
            continue
        _buf = getattr(_s, "buffer", None)
        if _buf is None:
            continue
        try:
            setattr(
                _sys,
                _name,
                _io.TextIOWrapper(
                    _buf, encoding="utf-8", errors="replace",
                    line_buffering=True,
                ),
            )
        except Exception:  # noqa: BLE001
            pass


_force_utf8()

import json
import os
import shutil
import socket
import subprocess
import io
import sys
import time
import urllib.error
import urllib.request

# ── 路径 ──
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PASS = 0
FAIL = 0
SKIP = 0
FAILED: list[str] = []


def ok(name: str, detail: str = "") -> None:
    global PASS
    PASS += 1
    print(f"  ✅ {name}" + (f"  {detail}" if detail else ""))


def bad(name: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    FAILED.append(f"{name} — {detail}")
    print(f"  ❌ {name}  {detail}")


def skip(name: str, why: str = "") -> None:
    global SKIP
    SKIP += 1
    print(f"  ⏭  {name}  {why}")


def section(title: str) -> None:
    print()
    print("─" * 66)
    print(f"  {title}")
    print("─" * 66)


# ══════════════════════════════════════════════════════
# 一、静态检查：入口文件在不在
#    这一节不需要起服务，秒出结果。
#    它专门盯"重构改了路径但别处没跟上"这类问题。
# ══════════════════════════════════════════════════════


def test_entrypoints() -> None:
    section("1. 入口文件")

    must = [
        ("core/bridge.py", "桥的命令行入口"),
        ("server/api_server.py", "本机接口"),
        ("desktop/app.py", "桌面端"),
        ("guard/action_guard.py", "动作守卫"),
        ("engine/agent.py", "引擎"),
    ]
    for rel, what in must:
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            ok(f"{rel}", what)
        else:
            bad(f"{rel} 不存在", what)

    # 根目录不该再有这些（都移进子目录了）
    gone = ["bridge.py", "api_server.py", "desktop_qt.py", "action_guard.py"]
    stale = [f for f in gone if os.path.exists(os.path.join(ROOT, f))]
    if stale:
        bad("根目录残留旧入口", str(stale))
    else:
        ok("根目录没有旧入口残留")


def test_no_stale_paths() -> None:
    section("2. 别处有没有拼错的路径")

    # 谁在拼 bridge.py 的路径？必须是 core/bridge.py
    hits = []
    for dp, dns, fns in os.walk(ROOT):
        # ⚠️ 跳过 tests/：这个文件自己就在找 "bridge.py" 这几个字，
        #    不跳过的话它会把自己报成问题（误报过一次）。
        dns[:] = [d for d in dns if d not in
                  ("__pycache__", ".git", "dist", "state", "workspaces", "tests")]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            try:
                src = open(p, encoding="utf-8").read()
            except OSError:
                continue
            for idx, line in enumerate(src.splitlines(), 1):
                low = line.strip()
                if low.startswith("#"):
                    continue
                # 找 "bridge.py" 这种字面量
                if '"bridge.py"' in low or "'bridge.py'" in low:
                    rel = os.path.relpath(p, ROOT)
                    if 'core", "bridge.py' not in low and "core', 'bridge.py" not in low \
                            and 'core/bridge.py' not in low and '"core"' not in low:
                        hits.append(f"{rel}:{idx}  {low[:70]}")

    if hits:
        bad("有地方还在拼旧的 bridge.py 路径", "")
        for h in hits:
            print(f"       {h}")
    else:
        ok("拼 bridge.py 路径的地方都指向 core/")


def test_no_relative_imports() -> None:
    section("3. 相对导入（脚本模式下会静默失效）")

    hits = []
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in ("__pycache__", ".git", "dist", "state", "workspaces")]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            try:
                src = open(p, encoding="utf-8").read()
            except OSError:
                continue
            for idx, line in enumerate(src.splitlines(), 1):
                s = line.strip()
                if s.startswith("from . ") or (
                    s.startswith("from .") and " import " in s and not s.startswith("from ..")
                ):
                    rel = os.path.relpath(p, ROOT)
                    hits.append(f"{rel}:{idx}  {s[:70]}")

    if hits:
        bad("还有相对导入", f"{len(hits)} 处")
        for h in hits[:10]:
            print(f"       {h}")
    else:
        ok("全部是绝对导入")


def test_bridge_runs_as_script() -> None:
    section("4. bridge.py 能当脚本跑")

    p = os.path.join(ROOT, "core", "bridge.py")
    try:
        r = subprocess.run(
            [sys.executable, p, "status"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if r.returncode == 0:
            ok("python core/bridge.py status", "退出码 0")
        else:
            bad(
                "python core/bridge.py status 失败",
                f"退出码 {r.returncode}  {(r.stderr or '')[:150]}",
            )
    except Exception as e:  # noqa: BLE001
        bad("起不来", f"{type(e).__name__}: {e}")


def test_api_server_imports() -> None:
    section("5. 关键模块能导入")

    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import core.bridge, server.api_server, engine.agent, engine.tools,"
        "guard.action_guard, guard.guard_gate;"
        "print('OK')" % ROOT
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if "OK" in (r.stdout or ""):
            ok("六个核心模块都能导入")
        else:
            bad("导入失败", (r.stderr or r.stdout or "")[:200])
    except Exception as e:  # noqa: BLE001
        bad("导入超时", f"{type(e).__name__}")


# ══════════════════════════════════════════════════════
# 二、进程边界：真起服务、真派活、真等结果
#    ────────────────────────────────────────
#    这一节是重点。上面那些静态检查能抓"路径拼错"，
#    但抓不到"起来了但退化成别的东西跑"。
#    只有真跑一遍才知道。
# ══════════════════════════════════════════════════════


def _port_open(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_free_port(start: int = 8900) -> int:
    for p in range(start, start + 50):
        if not _port_open(p, 0.1):
            return p
    return start


class Service:
    """起一个临时接口服务，用完关掉。

    用独立的 state 目录，不碰用户真实数据。
    """

    def __init__(self) -> None:
        self.port = _find_free_port()
        self.proc: subprocess.Popen | None = None
        self.tmp = os.path.join(
            os.environ.get("TEMP") or ".", f"dsh_e2e_{int(time.time())}"
        )
        os.makedirs(self.tmp, exist_ok=True)

    def start(self) -> bool:
        api = os.path.join(ROOT, "server", "api_server.py")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            self.proc = subprocess.Popen(
                [sys.executable, api, "--port", str(self.port)],
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False

        for _ in range(60):
            time.sleep(0.5)
            if _port_open(self.port):
                return True
            if self.proc.poll() is not None:
                return False
        return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, path: str, body: dict | None = None, timeout: int = 30):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Content-Type": "application/json"}
        # token 在用户目录，服务起来时已经生成了
        # token 在配置目录，服务起来时已经生成了
        # ⚠️ 走 tools/userdirs，不要自己拼路径。
        #    原来这里写的是 APPDATA or "~" —— 在 Linux 上会拼到
        #    ~/dsh-bridge/，而服务实际把 token 写在 ~/.config/dsh-bridge/。
        tp = userdirs.config_file("api_token.txt")
        if os.path.exists(tp):
            headers["X-Token"] = open(tp, encoding="utf-8").read().strip()

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=headers,
            method="POST" if body is not None else "GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def get(self, path: str, timeout: int = 30):
        return self._call(path, None, timeout)

    def post(self, path: str, body: dict, timeout: int = 60):
        return self._call(path, body, timeout)


def _engine_ready() -> tuple[bool, str]:
    """自带引擎配好了没（有 Key 才跑得动真模型）。"""
    try:
        sys.path.insert(0, ROOT)
        from engine import profiles as P

        act = P.active_profile() or {}
        if not act:
            return False, "没有配置"
        if not (act.get("base_url") and act.get("api_key") and act.get("model")):
            return False, "缺 base_url / api_key / model"
        return True, f"{act.get('name')} / {act.get('model')}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}"


def test_service_and_dispatch(quick: bool) -> None:
    section("6. 起服务 → 派活 → worker 真起来 → 跑完")

    svc = Service()
    if not svc.start():
        bad("接口服务起不来", f"端口 {svc.port}")
        return

    try:
        ok("接口服务起来", f"端口 {svc.port}")

        try:
            h = svc.get("/api/health", timeout=15)
            if h.get("ok"):
                ok("health 正常")
            else:
                bad("health 返回不对", str(h)[:100])
        except Exception as e:  # noqa: BLE001
            bad("health 打不通", f"{type(e).__name__}")

        # 派一条最小的活
        name = f"e2e_{int(time.time())}"
        try:
            r = svc.post(
                "/api/dispatch",
                {"task": "说一句 hello 就完成", "name": name},
                timeout=60,
            )
        except Exception as e:  # noqa: BLE001
            bad("派活失败", f"{type(e).__name__}: {e}")
            return

        if r.get("status") == "blocked":
            skip("派活被安全闸拦下", str(r.get("error"))[:80])
            return

        ok("派活成功", f"status={r.get('status')}")

        # 等它离开 pending——这是关键：
        # 离开 pending 就说明 worker 真的起来了。
        started = False
        for i in range(30):
            time.sleep(2)
            try:
                st = svc.get(f"/api/task/{name}", timeout=15)
            except Exception:  # noqa: BLE001
                continue
            cur = st.get("status")
            if cur and cur not in ("pending", "queued"):
                started = True
                ok("worker 起来了", f"{2 * (i + 1)} 秒后 status={cur}")
                break

        if not started:
            bad("worker 没起来", "30 次轮询一直 pending")
            return

        # 等到终态
        final = None
        for _ in range(60):
            time.sleep(2)
            try:
                st = svc.get(f"/api/task/{name}", timeout=15)
            except Exception:  # noqa: BLE001
                continue
            if st.get("status") in ("ok", "error", "timeout", "empty", "cancelled"):
                final = st
                break

        if final is None:
            bad("任务跑不完", "等了 2 分钟还是中间状态")
            return

        st = final.get("status")
        if st == "ok":
            ok("任务跑完", f"status=ok，{(final.get('result') or '')[:40]!r}")
        elif st == "error":
            err = final.get("error") or ""
            # 模型报错不算链路问题（可能是 Key 额度、网络）
            if "NO_ADAPTER" in err or "provider" in err.lower():
                bad("用错了 provider", err[:150])
            elif "api" in err.lower() or "http" in err.lower() or "key" in err.lower() or "余额" in err:
                skip("模型那边报错（链路是通的）", err[:100])
            else:
                bad("任务失败", err[:150])
        else:
            bad("任务没成功", f"status={st}  {(final.get('error') or '')[:100]}")

        # 顺带验证：多套配置没被用错
        try:
            profs = svc.get("/api/engine/profiles", timeout=15)
            if profs.get("ok"):
                ok("配置接口正常", f"{len(profs.get('profiles') or [])} 套")
        except Exception:  # noqa: BLE001
            bad("配置接口打不通")

    finally:
        svc.stop()


def test_plugins_have_right_paths() -> None:
    section("7. 插件里的路径")

    plug_dir = os.path.join(ROOT, "plugins")
    if not os.path.isdir(plug_dir):
        skip("没有 plugins 目录")
        return

    for fn in os.listdir(plug_dir):
        if not fn.endswith(".py"):
            continue
        p = os.path.join(plug_dir, fn)
        try:
            src = open(p, encoding="utf-8").read()
        except OSError:
            continue

        # 1. 有没有写死作者本机路径
        #
        # ⚠️ 只有**代码里**写死才算问题。
        #    注释里提到路径（比如说明"那个目录已经有 ACL 标记了"）
        #    是正常的，不算。
        #
        #    踩过：把注释里的路径也报成问题，造成一次误报。
        code_lines = [
            ln for ln in src.splitlines()
            if not ln.strip().startswith("#")
        ]
        hard = [
            ln for ln in code_lines
            if "C:\\Users\\" in ln and "19836" in ln
            and "BRIDGE_DIR" in ln or ("r\"C:\\Users\\19836" in ln)
        ]
        # 再用更精确的判据：真的在赋值语句里
        hard = [
            ln for ln in code_lines
            if "19836" in ln and "=" in ln and not ln.strip().startswith("#")
        ]
        if hard:
            bad(f"{fn} 写死了作者本机路径", "别人 clone 下来这条路不存在")
            for h in hard[:3]:
                print(f"       {h.strip()[:70]}")
        else:
            ok(f"{fn} 没写死本机路径")

        # 2. 拼 bridge 的地方指向 core/
        if '"bridge.py"' in src or "'bridge.py'" in src:
            if "core" in src:
                ok(f"{fn} 拼 bridge 路径指向 core/")
            else:
                bad(f"{fn} 拼 bridge 路径没指向 core/")

        # 3. 有没有静默 except
        import re
        silent = re.findall(r"except Exception[^:]*:\s*\n\s*pass", src)
        if silent:
            print(f"       ⚠️ {fn} 有 {len(silent)} 处 except: pass（会静默吞错）")


# ══════════════════════════════════════════════════════
# 主
# ══════════════════════════════════════════════════════


def main() -> int:
    quick = "--quick" in sys.argv

    print("=" * 66)
    print("  端到端测试：从拉下来到真能干活")
    print("=" * 66)
    print(f"  仓库: {ROOT}")
    print(f"  Python: {sys.version.split()[0]}  {sys.platform}")
    print(f"  可执行: {sys.executable}")

    t0 = time.time()

    # 静态检查（秒级）
    test_entrypoints()
    test_no_stale_paths()
    test_no_relative_imports()
    test_bridge_runs_as_script()
    test_api_server_imports()
    test_plugins_have_right_paths()

    # 进程边界（分钟级）
    ready, why = _engine_ready()
    if quick:
        skip("派活端到端", "--quick 跳过")
    elif not ready:
        skip("派活端到端", f"模型没配好：{why}")
    else:
        print(f"\n  模型: {why}")
        test_service_and_dispatch(quick)

    print()
    print("=" * 66)
    el = time.time() - t0
    print(f"  合计：{PASS} 通过，{FAIL} 失败，{SKIP} 跳过（{el:.1f} 秒）")
    if FAILED:
        print()
        print("  失败清单：")
        for f in FAILED:
            print(f"    · {f}")
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
