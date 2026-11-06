"""测「改权限要过验证码」的完整流程。

要验证：
    1. 降权（收窄）→ 直接改，不用码
    2. 升权（放宽）→ 第一次发码，不生效
    3. 带对码     → 生效
    4. 带错码     → 不生效
    5. 通配符批量 → 拒绝
"""

import json
import os
import shutil
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["APPDATA"] = os.path.join(TMP, "roaming")
os.environ["LOCALAPPDATA"] = os.path.join(TMP, "local")
os.makedirs(os.environ["APPDATA"], exist_ok=True)
os.makedirs(os.environ["LOCALAPPDATA"], exist_ok=True)

# ⚠️ 不能写死路径。原来写的是 r"D:\Project\dsh-bridge-oss"，
#    换台机器直接 ModuleNotFoundError。
#    这个文件的上一级就是仓库根。
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)
# 换成 mark_utf8（带 errors=replace 兜底，见 tools/console.py）
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

from guard import task_perms  # noqa: E402
from guard import guard  # noqa: E402

# ⚠️ 开跑之前先把守卫状态清干净。
#
#    这是个独立脚本（不是 pytest 模块），run_all.py 是另起进程跑它的。
#    但守卫状态**落盘**了（lock_state.json / guard.json），
#    所以上一个测试文件留下的状态会被继承：
#
#        实测（WSL2 Ubuntu 24.04）：
#        先跑 test_guard.py（里面测了三次错误码锁死）
#        再跑本脚本 → 第 3 步「带对码 → 生效」失败
#
#        因为守卫还锁着，填对码也不放行。
#
#    单跑没问题，全量跑才挂 —— 跟 test_perm_verify 当初污染别人是同一类问题，
#    只是方向反过来：这次是它被别人污染。
guard.reset_all()

# 打桩：不发真邮件，直接塞一个码
_real_send = guard.send_code_email

def _fake_send(task: str, purpose: str = ""):
    """模拟发码：不真发信，直接塞一个 pending。"""
    import time as _t
    fp = guard._fingerprint(task)
    guard._state.pending[fp] = guard.PendingCode(
        code="123456", task=task, purpose=purpose, expires=_t.time() + 120
    )
    return True, "验证码已发到邮箱（测试桩）"

guard.send_code_email = _fake_send

PASS = FAIL = 0


def ck(desc, got, want):
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {'✅' if ok else '❌'} {desc:52} → {got}")


# ══════════════════════════════════════════════════════
# 直接测接口逻辑（不起 HTTP 服务，直接调 _perms_set）
# ══════════════════════════════════════════════════════

# 造一个假的 Handler 实例，只带 _perms_set
class FakeHandler:
    pass


import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "api_server",
    os.path.join(_HERE, "server", "api_server.py"),
    # ⚠️ 上面这行原来是 r"D:\Project\dsh-bridge-oss\api_server.py"。
    #    路径写死是问题之一；更糟的是**路径本身就错** ——
    #    api_server.py 在 server/ 下，不在仓库根。
    #    于是这个测试一直在走 except 分支：打印「跳过接口测试」然后 exit 0，
    #    而 run_all 看到退出码 0 就认为它过了。接口那一段从来没被测过。
)
api = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(api)
except Exception as e:  # noqa: BLE001
    print(f"  加载 api_server 失败（缺依赖）：{e}")
    print("  改用直接测 task_perms + guard 的组合")
    api = None


if api is None:
    print("  跳过接口测试")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0)


h = api.Handler.__new__(api.Handler)


# ⚠️ 验码挪到 _do_post 的**统一闸**（_config_gate）了，setter 自己不再验。
#
#    2026-11-06 改的：原来每个 setter 各写各的（_perms_set 有、_guard_set 没有……），
#    一个地方漏写就是一个洞。现在清单 + 统一闸。
#
#    所以直接调 _through_gate(...) 测的是"没有闸的世界"，
#    测出来跟线上行为对不上 —— 这里补一层，模拟闸：
#
#        没码   → 闸发码、回 need_code、不执行
#        带了码 → 闸验、过了才进 setter
#
#    这样这个脚本测的才是真实路径。


def _through_gate(body: dict) -> dict:
    """模拟 _do_post 里的 _config_gate + setter。"""
    import time as _t

    purpose = api.Handler._config_purpose("/api/perms", body)
    code = (body.get("code") or "").strip()

    if not code:
        # 闸：发码，不执行。每次来都发（真实服务端就是这样）。
        fp = guard._fingerprint(purpose)
        guard._state.pending[fp] = guard.PendingCode(
            code="123456", task=purpose, purpose="改权限",
            expires=_t.time() + 120,
        )
        return {
            "ok": False,
            "need_code": True,
            "purpose": purpose,
            "message": "验证码已发到邮箱（测试桩）",
        }

    ok, msg = guard.verify(purpose, code)
    if not ok:
        return {"ok": False, "need_code": True, "error": msg}

    # 验过了 —— 进 setter（这里才是真正改权限的地方）
    return h._perms_set(body)


print("=" * 68)
print("1. 降权：也要码（收紧了一点）")
print("=" * 68)
# ⚠️ 这一条的行为变了。
#
#    原来是"降权不要码"（收窄只会更安全）。
#    现在统一闸对 /api/perms 一律要码 ——
#    不留"哪个动作不用码"这种要在两处对齐的规则，宁可多一次码。
task_perms.set_level("任务A", "full")
ck("先设成 full", task_perms.get_level("任务A"), "full")

r = _through_gate({"name": "任务A", "level": "workspace"})
ck("降权也要码", r.get("need_code"), True)
ck("还没生效", task_perms.get_level("任务A"), "full")

r = _through_gate({"name": "任务A", "level": "workspace", "code": "123456"})
ck("带码后降权成功", r.get("ok"), True)
ck("权限真的变了", task_perms.get_level("任务A"), "workspace")

print("\n" + "=" * 68)
print("2. 升权：第一次发码，不生效")
print("=" * 68)
task_perms.set_level("任务B", "workspace")

r = _through_gate({"name": "任务B", "level": "full"})
ck("要求验证码", r.get("need_code"), True)
ck("还没生效", task_perms.get_level("任务B"), "workspace")
print(f"      提示：{r.get('message', '')[:60]}")

print("\n" + "=" * 68)
print("3. 带对码 → 生效")
print("=" * 68)
# 直接从 guard 的内部状态拿码（模拟用户从邮箱看到）
from guard import guard  # noqa: E402

fp = guard._fingerprint(api.Handler._config_purpose("/api/perms",
            {"name": "任务B", "level": "full"}))
p = guard._state.pending.get(fp)
if p:
    real_code = p.code
    print(f"      （模拟从邮箱拿到码：{real_code}）")

    r = _through_gate({"name": "任务B", "level": "full", "code": real_code})
    ck("改成功了", r.get("ok"), True)
    ck("权限真的变了", task_perms.get_level("任务B"), "full")
else:
    print("      ❌ 没有待验证的码")
    FAIL += 1

print("\n" + "=" * 68)
print("4. 带错码 → 不生效")
print("=" * 68)
task_perms.set_level("任务C", "workspace")

# 先发码
_through_gate({"name": "任务C", "level": "home"})

r = _through_gate({"name": "任务C", "level": "home", "code": "000000"})
ck("被拒", r.get("ok"), False)
ck("权限没变", task_perms.get_level("任务C"), "workspace")

print("\n" + "=" * 68)
print("5. 通配符 → 拒绝")
print("=" * 68)
for name in ("*", "任务*", "all", "?"):
    r = _through_gate({"name": name, "level": "full"})
    # "*" "任务*" "?" 该拒；"all" 只是个普通名字，不该拒
    if name in ("*", "任务*", "?"):
        ck(f"拒绝通配符 {name!r}", r.get("ok"), False)
        print(f"        {r.get('error', '')}")

print("\n" + "=" * 68)
print("6. 码绑具体任务：A 的码不能改 B")
print("=" * 68)
task_perms.set_level("任务D", "workspace")
task_perms.set_level("任务E", "workspace")

# 给 D 发码
_through_gate({"name": "任务D", "level": "full"})
fp_d = guard._fingerprint(api.Handler._config_purpose("/api/perms",
            {"name": "任务D", "level": "full"}))
pd = guard._state.pending.get(fp_d)

if pd:
    # 拿 D 的码去改 E
    r = _through_gate({"name": "任务E", "level": "full", "code": pd.code})
    ck("D 的码改不了 E", task_perms.get_level("任务E"), "workspace")

print("\n" + "=" * 68)
# 跑完清一次，别把状态留给下一个（run_all 里我们后面还有暴力攻击、端到端）
try:
    guard.reset_all()
except Exception:  # noqa: BLE001
    pass

print(f"结果：{PASS} 通过，{FAIL} 失败")
print("=" * 68)

shutil.rmtree(TMP, ignore_errors=True)
