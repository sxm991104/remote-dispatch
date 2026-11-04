"""危险操作的验证码门。

为什么要这个
    前台丫头能派活。派出去的活能删库、能回滚代码、能读密钥文件。
    而她和用户之间的通道（QQ / 飞书）是**可以被盗用的**——
    拿到手机的人能翻聊天记录，能学用户的语气，能说「确认」。

    所以「问用户确认」这个方案挡不住恶意，只能挡手滑。

    真正的独立通道得是**不在对话里的东西**——邮箱。

三级递进
    第 1 次   提醒      「这会删掉整个数据库，确定吗？」
    第 2 次   警告      「再说一次：不可逆。真的要做？」
    第 3 次   验证码    坚持 → 发到邮箱，拿到码才执行

    前两次是给用户机会自己停下的（说错话、用词夸张、手滑）。
    第三次才是真正的身份验证：贼能说三次「确认」，
    但拿不到邮箱里的那串数字。

几个必须守住的点
    一、验证码只发到邮箱，**绝不出现 IM 通道里**。
        一旦出现在对话里，它就跟聊天记录一起被偷走了。

    二、一次性。验过就作废，不能复用。

    三、有次数上限。猜错了锁一段时间，不让暴力枚举。

    四、绑定到具体的操作。
        给「删 A 库」发的码，不能拿去「删 B 库」。
"""

from __future__ import annotations

import hashlib
import json
import os
import smtplib
import ssl
import secrets
import time
from dataclasses import dataclass, field
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

# 各平台该把配置放哪儿，见 tools/userdirs.py
import tools.userdirs as _userdirs

# 验证码有效期（秒）。
#
# 用户定的：120 秒。短信验证码通常是 5 分钟，但这里是本机操作，
# 用户就在电脑前面，两分钟足够翻一下邮箱。
# 给太长的窗口反而让"贼在旁边等着"有可乘之机。
CODE_TTL = 120

# ⚠️ 已废弃：锁不再自动过期。
#
#    这里原来写的是"验证码过期后再锁 10 分钟，锁满自动解"。
#    那个设计给攻击者留了节奏：
#        试 → 等十分钟 → 再试
#    对普通用户却毫无意义（他本来就不会撞到 3 次）。
#
#    用户的原话：
#        「锁死十分钟后解锁，或者这个锁死直接锁死，
#          必须要验证码，不给验证码，那就什么都干不了。
#          用户肯定能有验证码，而黑客被锁在了外面。」
#
#    现在用 LOCK_FOREVER：锁到验证通过为止。
#
#    这个常量留着只是因为还有几处老代码引用它。
#    新代码请用 LOCK_FOREVER。
LOCK_AFTER_EXPIRE = 600

# 锁到验证通过为止。
#
# 用一个很远的未来时间戳，而不是 None——
# 因为一大堆地方是拿它跟 time.time() 比较大小，
# 改成 None 会到处崩。用大数最省事，语义也对：
# "到 9999 年才过期"等于"不会自己好"。
LOCK_FOREVER = 4102444800.0     # 2100-01-01 00:00:00 UTC

# 最多猜几次验证码
MAX_TRIES = 3

# ⚠️ 只剩 _state.locked_until 按"谁在猜"记的时候用到。
#    真正决定"锁多久"的是 _force_locked[fp] = LOCK_FOREVER。
LOCK_SECONDS = 600

# 危险操作清单。
#
# ⚠️ 这只是"初筛"。真正的判断还是在前台那个模型那里——
#    她看到用户说「把数据库清一下」，即使一个关键词都没匹配上，
#    也该自己意识到这是危险操作。
#    这里的关键词是给**代码层**做最后一道检查用的。
DANGEROUS_PATTERNS = (
    # 删除
    "rm -rf", "rm -r", "del /f", "del /s", "rd /s", "rmdir /s",
    "drop table", "drop database", "truncate", "delete from",
    "清空", "删库", "删掉整个", "全部删",
    # 版本控制
    "git reset --hard", "git push -f", "git push --force",
    "git clean -fd", "git branch -D", "回滚到", "重置到",
    # 磁盘
    "format ", "diskpart", "mkfs",
    # 系统
    "reg delete", "setx ", "环境变量",
    # 密钥
    ".env", "credentials", "id_rsa", "api_key", "secret_key",
)


@dataclass
class PendingCode:
    """一次待验证的申请。"""
    code: str
    task: str              # 要执行的任务
    purpose: str           # 用途（用于邮件正文）
    expires: float = 0.0
    tries: int = 0


@dataclass
class State:
    pending: dict[str, PendingCode] = field(default_factory=dict)
    # 按"谁"的失败次数记录，防止暴力试
    fails: dict[str, int] = field(default_factory=dict)
    locked_until: dict[str, float] = field(default_factory=dict)


_state = State()

# 上次见到的重置令牌。
#
# reset_all() 会往 lock_state.json 写一个新的 _reset_token。
# 别的进程 sync 时比较这个值——不一样就说明有人重置过，
# 自己也要清空。
#
# 为什么用令牌而不是"磁盘是真相"：
#    有些状态本来就该只在内存（比如测试注入的验证码），
#    "磁盘是真相"会把它们冲掉，test_guard 挂了 16 条。
# 为什么不干脆不管跨进程：
#    实测撞到过——一个进程清了，另一个还一直在拦。
_seen_reset_token: str = ""

# 已经通过验证码的任务指纹。
#
# 为什么不复用 _state.pending：
#    pending 是"等验证的"，验过就删了。
#    这个记的是"验过之后允许执行"，两者生命周期不一样。
#
# 为什么要记：
#    验过之后，用户可能隔几秒才真正派活（中间还要说句话）。
#    不记的话，派活时再查就查不到"验过"的痕迹，会被重新拦下。
#
# 有效期 10 分钟——比验证码本身长，因为中间可能聊几句。
_verified: dict[str, float] = {}


def mark_verified(task: str) -> None:
    """记下这个任务已经验过了。

    ⚠️ 原来只管 600 秒，过了又要重验。

        用户的规则是「验证码拿到，数字清零」——
        验过就是验过。

        而且实际用起来 600 秒太短：锁死之后用户去翻邮箱、
        填码，这中间经常超过十分钟，等于白验一次。

        现在验过一直有效（进程重启后由共享状态文件带回来）。
    """
    _verified[_fingerprint(task)] = time.time() + 86400 * 30
    _flush_to_disk()


def is_verified(task: str) -> bool:
    """这个任务验过吗（且没过期）。"""
    fp = _fingerprint(task)
    exp = _verified.get(fp)
    if exp is None:
        return False
    if exp < time.time():
        _verified.pop(fp, None)
        return False
    return True


# 安全锁：验证码过期后还要关多久。
#
# 结构：{指纹: 解锁时刻}
# 有这个记录、且没到时刻 → 锁着
_locked: dict[str, float] = {}

# 强制锁：验证码错满三次之后。
#
# 跟普通锁的区别：
#     普通锁（验证码过期）  → 可以重新发码，重置倒计时
#     强制锁（错满三次）    → **不许重新发码**，只能等它自己满
#
# 为什么分开：
#     两个人可能遇到的情况完全不一样。
#
#     验证码过期   → 用户可能就是没看到邮件。让他重发是合理的。
#     验证码错三次  → 可能是有人在猜。这时候再给他无限次重发，
#                    等于把 10 万种可能（6 位数字）摊在十分钟里试。
#
#     用户的原话：
#         「验证码错误（满三次）→ 告知用户还剩几次机会
#           → 三次全错 → 强制锁十分钟（无法重新获取验证码）」
_force_locked: dict[str, float] = {}


# ══════════════════════════════════════════════════════
# 跨进程的锁状态
#
# 为什么要有这个
#     上面几个字典都是**进程内存**。而这个程序有两个进程：
#         AstrBot  跑前台（插件里 import guard）
#         桥接口    跑任务端（server/api_server.py）
#
#     各有一份 → 前台锁了桥不知道，桥解了前台不知道。
#
#     实测撞到过：
#         前台说「验证码超时了，还要锁约 10 分钟」
#         桥那边 check() 返回 fenced=False，干干净净
#         用户不知道该信哪个，我也不知道。
#
# 存哪
#     %APPDATA%/dsh-bridge/lock_state.json
#
# 存什么
#     {
#       "locked":      {"<指纹>": 到什么时候, "_all": 全部锁},
#       "force_locked": {"<指纹>": 到什么时候},
#       "pending":     {"<指纹>": {code, task, purpose, expires}},
#       "verified":    {"<指纹>": 什么时候验的}
#     }
#
# ⚠️ 验证码明文存在这个文件里。
#    本来验证码在内存里，进程一死就没了；现在落盘，重启还在。
#    这是有意的——用户拿到码之后重启了程序，码应该还有效。
#    文件权限靠 _restrict_to_me（跟 token 一样，只让当前用户读）。
# ══════════════════════════════════════════════════════


def _lock_path() -> str:
    return _userdirs.config_file("lock_state.json")


def _load_state() -> dict:
    """读共享锁状态。坏了就当空。"""
    p = _lock_path()
    try:
        if os.path.exists(p):
            d = json.loads(open(p, encoding="utf-8").read())
            if isinstance(d, dict):
                return d
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_state(d: dict) -> None:
    """写共享锁状态。

    ⚠️ 两个进程会同时写，所以：
        1. 先写临时文件再 replace（原子，避免读到半个文件）
        2. 失败就算了——不能让"存不了锁状态"把正常功能弄挂
    """
    p = _lock_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + f".tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        pass


def _merge_and_save(**parts) -> dict:
    """把这次的变化并进共享状态，返回合并后的。

    为什么要"读-改-写"而不是直接覆盖：
        另一个进程可能刚写了别的东西。
        每次只改自己那部分。
    """
    d = _load_state()
    for k, v in parts.items():
        d[k] = v
    d["_saved_at"] = int(time.time())
    _save_state(d)
    return d


# 把内存里的几个字典改成"跟共享状态同步"的读写。
#
# 为什么不干脆删掉内存字典：
#    代码里到处在用（_state.pending、_locked……），
#    全改一遍风险大。用一个薄薄的同步层更稳：
#    每次读写都跟文件对一下。


def _disk() -> dict:
    """读共享状态。每次都读，文件很小。"""
    return _load_state()


def _sync_from_disk() -> None:
    """把磁盘上"我没有"的东西补进来。

    ⚠️ 这是**补缺**，不是"清空重建"。

        清空重建会把内存里还没落盘的冲掉：
            发码写好内存 → 另一次同步 → 码没了 → 用户永远验不过
        实测撞到过好几次。

        合并/判新旧也试过，太复杂、还引入了新 bug。

        其实不需要判新旧：
            验证码是发给同一个用户的，不会有"两边各有一个不同的码"。
            内存里有就用内存的；内存里没有才去磁盘拿。

    删除交给 expires 自然过期兜底，不做显式删。
    """
    d = _disk()
    now = time.time()

    # 先看有没有人重置过。
    #
    # 别的进程调了 reset_all() → 磁盘上的 _reset_token 变了。
    # 这时候要把自己的内存全清掉，不然会一直用旧状态拦人。
    tok = str(d.get("_reset_token") or "")
    global _seen_reset_token
    if tok and tok != _seen_reset_token:
        _seen_reset_token = tok
        _state.pending.clear()
        _state.locked_until.clear()
        _state.fails.clear()
        _locked.clear()
        _force_locked.clear()
        _verified.clear()
        return

    for k, v in (d.get("locked") or {}).items():
        if k in _locked:
            continue
        try:
            until = float(v)
        except (TypeError, ValueError):
            continue
        if until > now:
            _locked[k] = until

    for k, v in (d.get("force_locked") or {}).items():
        if k in _force_locked:
            continue
        try:
            until = float(v)
        except (TypeError, ValueError):
            continue
        if until > now:
            _force_locked[k] = until

    for k, v in (d.get("verified") or {}).items():
        if k in _verified:
            continue
        try:
            until = float(v)
        except (TypeError, ValueError):
            continue
        if until > now:
            _verified[k] = until

    for k, v in (d.get("pending") or {}).items():
        if k in _state.pending:
            continue
        if not isinstance(v, dict):
            continue
        try:
            exp = float(v.get("expires") or 0)
        except (TypeError, ValueError):
            continue
        if exp > now:
            _state.pending[k] = PendingCode(
                code=str(v.get("code") or ""),
                task=str(v.get("task") or ""),
                purpose=str(v.get("purpose") or ""),
                expires=exp,
            )


def _flush_to_disk() -> None:
    """把内存里的状态写出去。

    先读磁盘（保住别的进程写的东西），再把自己的并进去，
    最后整体写回。

    因为每次都是"读-改-写"，所以不会覆盖掉别人的。
    并发写的最坏情况是丢一次更新——锁这种场景能接受，
    下一次拦截/验证会再写一次。
    """
    now = time.time()
    d = _disk()

    d["locked"] = {k: v for k, v in _locked.items() if v > now}
    d["force_locked"] = {k: v for k, v in _force_locked.items() if v > now}
    d["verified"] = {k: v for k, v in _verified.items() if v > now}
    d["pending"] = {
        k: {
            "code": v.code,
            "task": v.task,
            "purpose": v.purpose,
            "expires": v.expires,
        }
        for k, v in _state.pending.items()
        if v.expires > now
    }
    d["_saved_at"] = int(now)
    _save_state(d)


def _expire_pending(fp: str) -> None:
    """一个验证码过期了：删掉它，锁上。

    ⚠️ 这里是这次改的重点之一。

        原来：码过期 → 锁 10 分钟 → **时间到了自动开**

        那等于给攻击者一个节奏：
            试三次 → 等十分钟 → 再试三次 → ……
        十分钟对普通用户毫无意义（他本来就不会撞到），
        对攻击者却是一张"稍等继续"的通行证。

        用户的原话：
            「锁死十分钟后解锁，或者这个锁死直接锁死，
              必须要验证码，不给验证码，那就什么都干不了。
              用户肯定能有验证码，而黑客被锁在了外面。」

        现在：码过期 → 锁上，**只有验证通过才开**。
             没有"等一会儿自己好"这条路。
    """
    p = _state.pending.pop(fp, None)
    if p is None:
        return
    # LOCK_FOREVER = 到验证通过为止
    _locked[fp] = LOCK_FOREVER


def lock_remaining(task: str) -> int:
    """这个任务还要锁多少秒。0 表示没锁。

    ⚠️ 要同时看两种锁。红队报告 F-09 指出的就是这个。

        原来只查 _locked（"验证码过期后还要锁 10 分钟"那种）。
        但还有 _force_locked —— "被拦满三次、任务端锁死"的那种。

        后果：is_fenced() 调的是这个函数，于是"被拦满三次锁死之后，
        is_fenced 却说没被关着"。

        派活那条路本身安全（cmd_dispatch 里单独查了 _force_locked），
        所以不是直接可利用的漏洞。但一个叫"还剩多少秒"的函数
        不知道最重的那种锁，迟早有人在别处用它做判断。

        现在两种锁取大的那个。
    """
    # 先把别的进程可能改过的状态读进来
    _sync_from_disk()

    fp = _fingerprint(task)

    # 先看看有没有过期的码要处理
    p = _state.pending.get(fp)
    if p is not None and p.expires < time.time():
        _expire_pending(fp)

    # 任务端级强制锁（被拦满三次那种）
    force_until = _force_locked.get(fp)
    if force_until is None:
        force_until = _force_locked.get("_all")
    force_left = int((force_until or 0) - time.time())
    if force_left > 0:
        return force_left

    until = _locked.get(fp)
    if until is None:
        return 0
    left = int(until - time.time())
    if left <= 0:
        # 锁满了，自动开
        _locked.pop(fp, None)
        return 0
    return left


def is_fenced(task: str) -> bool:
    """这个任务现在被安全锁关着吗。

    什么时候会这样：
        · 验证码还活着（等用户填）
        · 验证码过期了，但锁还没满 10 分钟
    """
    fp = _fingerprint(task)

    # 码还没过期 → 等验证，算被关着
    p = _state.pending.get(fp)
    if p is not None and p.expires >= time.time():
        return True

    return lock_remaining(task) > 0


def check(task: str) -> dict:
    """派活前的检查。

    返回：
        {
            "fenced": bool,      被安全锁关着吗（关着就什么都派不出去）
            "verified": bool,    这个任务验过了吗
            "dangerous": bool,   看着像危险操作吗（仅供参考）
            "lock_left": int,    还要锁多少秒
            "waiting": bool,     有没有一个还没过期的码在等用户填
            "reason": str,
        }

    ⚠️ 关键设计：**围栏是全局的，不是按任务分的。**

        用户的原话：
            「只要出了验证码，整个任务端都锁死，什么活都排不出去，
              这已经触发了安全围栏」

        所以这里不判断"这个任务危不危险"再决定拦不拦——
        一旦有任务触发过围栏（发了码），在这个任务解锁之前，
        **所有派活请求都被拒绝**。

        那个"别的任务"怎么判断它属于这个围栏？
            简单做法：只要有任何任务处于围栏状态，就全站锁。
            因为围栏的语义是"系统可能被入侵了，先停下来"。
    """

    # 先把别的进程改过的状态读进来（前台和桥是两个进程）
    _sync_from_disk()
    # 先扫一遍，把过期的码转成锁
    for fp in list(_state.pending.keys()):
        p = _state.pending.get(fp)
        if p is not None and p.expires < time.time():
            _expire_pending(fp)

    # 任意一个任务在围栏里，全站锁。
    now = time.time()
    any_pending = any(p.expires >= now for p in _state.pending.values())

    # 清理到期自动解开的锁
    for fp in list(_locked.keys()):
        if _locked[fp] <= now:
            _locked.pop(fp, None)

    # 清理到期的强制锁
    for fp in list(_force_locked.keys()):
        if _force_locked[fp] <= now:
            _force_locked.pop(fp, None)

    # 强制锁期间绝对不能派活，也不能重发
    forced = bool(_force_locked)

    global_lock = any_pending or bool(_locked) or forced

    # 这个任务自己的状态
    mine_verified = is_verified(task)
    mine_pending = has_pending(task)

    # ⚠️ lock_left 是**全站**的解锁剩余时间，不是这个任务自己的。
    #
    #    原来写的是 lock_remaining(task)——查的是"这个任务有没有锁"。
    #    但围栏是全局的：A 任务触发的锁，B 任务也要等。
    #    查 B 自己的锁当然查到 0，于是"还要锁多久"显示成 0，
    #    用户以为马上就好，实际上要等十分钟。
    #
    #    有多个锁时取**最晚**的那个——最后一个解开才算真的开。
    if _locked:
        lefts = [int(v - now) for v in _locked.values() if v > now]
        global_left = max(lefts) if lefts else 0
    else:
        global_left = 0

    # 强制锁的剩余时间也算进"还要锁多久"
    if _force_locked:
        fl = max(int(v - now) for v in _force_locked.values())
        global_left = max(global_left, fl)

    # 有码在等的时候，显示的是"等用户填"，不算倒计时
    mine_left = global_left if not any_pending else 0

    # 能不能重新发码：强制锁期间不行
    can_resend = not forced

    reason = ""
    if global_lock and not mine_verified:
        if forced:
            reason = (
                f"验证码连错三次，强制锁 "
                f"{global_left // 60} 分 {global_left % 60} 秒。"
                f"这段时间里不能重新获取验证码。"
            )
        elif any_pending:
            reason = "有验证码等着填，任务端现在锁着"
        elif global_left > 0:
            reason = (
                f"验证码超时了，任务端还要锁 "
                f"{global_left // 60} 分 {global_left % 60} 秒"
            )
        else:
            reason = "任务端被别的验证请求锁着"

    # ⚠️ "计数锁死"也要算进 fenced。
    #
    #    原来 fenced 只看 global_lock（时间锁 / 强制锁），
    #    不看"今天被拦满三次"。
    #
    #    后果实测：
    #        计数文件里 count=3（真的锁死了）
    #        /api/dispatch 回 blocked
    #        但前台问 /api/guard/check → fenced=False → 它说"能派"
    #    两边说的是两件事，用户被搞糊涂。
    #
    #    用户的原话：
    #        「锁死我们要怎么解锁呢？这个肯定就要找前台了是吧？」
    #    前台连"现在锁着"都看不到，当然没法带他走出来。
    tripped = False
    trip_count = 0
    try:
        from guard import guard_gate as _gg

        _c = _gg.peek_count()
        tripped = bool(_c.get("tripped"))
        trip_count = int(_c.get("count") or 0)
    except Exception:  # noqa: BLE001
        pass

    if tripped and not reason:
        reason = (
            f"今天已经被拦了 {trip_count} 次，任务端锁死。\n"
            "要重新用的话，得走验证码解锁。"
        )

    fenced = bool((global_lock or tripped) and not mine_verified)

    return {
        # fenced：派不出去（这是插件真正要看的）
        "fenced": fenced,
        # tripped：是不是因为"今天被拦满三次"锁死的
        # （跟其他锁区分开，方便前台说清楚该怎么解）
        "tripped": tripped,
        "trip_count": trip_count,
        # verified：能不能派。跟 fenced 相反，但语义更直白——
        # 没被围栏关着就是"可以派"，不管这个任务本身危不危险
        "verified": not fenced,
        "dangerous": looks_dangerous(task),
        "lock_left": mine_left,
        "waiting": mine_pending,
        "can_resend": can_resend,
        "forced": forced,
        "reason": reason,
    }


def _cfg_path() -> str:
    return _userdirs.config_file("guard.json")


def load_config() -> dict:
    """读安全配置（邮箱、SMTP）。

    ⚠️ 这里存的是 SMTP 授权码。不进代码、不进仓库、
        不写日志、不回传给界面。
    """
    p = _cfg_path()
    if not os.path.exists(p):
        return {
            "email": "",
            "smtp_host": "",
            "smtp_port": 465,
            "smtp_user": "",
            "smtp_pass": "",
            "enabled": False,
        }
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("enabled", bool(d.get("email") and d.get("smtp_host")))
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {"email": "", "smtp_host": "", "smtp_port": 465,
            "smtp_user": "", "smtp_pass": "", "enabled": False}


def save_config(cfg: dict) -> None:
    p = _cfg_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    # 这个文件里有授权码，收紧权限
    #
    # ⚠️ 原来直接调 icacls，在 Linux 上会 FileNotFoundError 被吞掉。
    #    交给 platform_utils 统一处理。
    try:
        from tools import platform_utils as _pu

        _pu.tighten_file(p)
    except ImportError:
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def is_guard_ready() -> bool:
    """验证码通道配好了没有。"""
    c = load_config()
    return bool(c.get("enabled") and c.get("email") and c.get("smtp_host"))


# 常见邮箱服务商的 SMTP 参数。
#
# 填个邮箱地址就该认出服务器——不该让用户去查文档。
# 查不到的一律让用户自己填，不瞎猜。
_SMTP_PRESETS = {
    "qq.com": ("smtp.qq.com", 465),
    "foxmail.com": ("smtp.qq.com", 465),
    "163.com": ("smtp.163.com", 465),
    "126.com": ("smtp.126.com", 465),
    "yeah.net": ("smtp.yeah.net", 465),
    "sina.com": ("smtp.sina.com", 465),
    "sina.cn": ("smtp.sina.com", 465),
    "sohu.com": ("smtp.sohu.com", 465),
    "aliyun.com": ("smtp.aliyun.com", 465),
    "139.com": ("smtp.139.com", 465),
    "189.cn": ("smtp.189.cn", 465),
    "gmail.com": ("smtp.gmail.com", 465),
    "outlook.com": ("smtp-mail.outlook.com", 587),
    "hotmail.com": ("smtp-mail.outlook.com", 587),
    "live.com": ("smtp-mail.outlook.com", 587),
    "office365.com": ("smtp.office365.com", 587),
    "yahoo.com": ("smtp.mail.yahoo.com", 465),
    "icloud.com": ("smtp.mail.me.com", 587),
}


def detect_smtp(email: str) -> tuple[str, int] | tuple[None, None]:
    """从邮箱地址猜出 SMTP 服务器。

    猜得到返回 (host, port)，猜不到返回 (None, None)——
    猜不到就让用户填，不瞎猜（猜错了会发不出邮件，
    而用户会以为是自己的授权码有问题，白折腾半天）。
    """
    if not email or "@" not in email:
        return None, None
    domain = email.rsplit("@", 1)[1].strip().lower()
    return _SMTP_PRESETS.get(domain, (None, None))


def looks_dangerous(task: str) -> bool:
    """这段任务描述看着危险吗。

    只做初筛——说了「删」「回滚」这类词的算危险。
    判断很保守：宁可多要一次验证码，也别漏放过删库的。
    """
    low = (task or "").lower()
    return any(p in low for p in DANGEROUS_PATTERNS)


def _fingerprint(task: str) -> str:
    """给任务算个指纹，让验证码跟具体任务绑定。

    为什么要绑定：
        给「删 A 库」发的码，不能拿去「删 B 库」。
        不绑的话，贼只要让你为一件无害的事过个码，
        那个码就能拿去干别的。
    """
    return hashlib.sha256((task or "").encode("utf-8")).hexdigest()[:16]


def _new_code() -> str:
    """6 位数字。用 secrets 生成，不用 random。"""
    return f"{secrets.randbelow(1_000_000):06d}"


def send_code_email(task: str, purpose: str = "",
                    config: dict | None = None) -> tuple[bool, str]:
    """发一个验证码到用户的邮箱。返回 (成功, 提示)。

    ⚠️ 这个函数**不把验证码返回给调用方**。
        返回给调用方就等于写进了对话上下文，
        那这个码就废了。

        验证码只走邮件这一条路。

    config：传了就用它，不传就读已保存的。

        ⚠️ 为什么要这个参数：
            "填 → 测 → 存" 是正常顺序。用户填完就想先测一发。
            但原来这里一进来就 load_config()，
            读的是**上一次保存的**值。

            结果：用户填了 163 邮箱，点「发封测试邮件」，
            实际还在连 test@example.com——必然失败，
            而他以为是自己填错了。

            （踩过：用户填了半天，一直"发送失败"，
              查了半小时才发现测试邮件压根没用他填的值。）
    """
    c = config if config is not None else load_config()

    # 服务器没填的话，从邮箱地址自动认一个
    if not c.get("smtp_host") and c.get("email"):
        h, p_ = detect_smtp(c["email"])
        if h:
            c["smtp_host"] = h
            c["smtp_port"] = p_
            save_config(c)

    if config is not None:
        # 用传进来的值判断，不看已保存的那份
        if not (c.get("email") and c.get("smtp_host") and c.get("smtp_pass")):
            return False, "邮箱 / 发信服务器 / 授权码 三样都得填"
    elif not is_guard_ready():
        return False, "还没配安全邮箱"

    fp = _fingerprint(task)

    # 强制锁期间不许重新发码
    fl = _force_locked.get(fp)
    if fl is not None:
        left = int(fl - time.time())
        if left > 0:
            return False, (
                f"验证码连错三次，锁着 {left // 60} 分 {left % 60} 秒。\n"
                f"这段时间里不能重新获取验证码——只能等它自己满。"
            )
        # 锁满了，清掉
        _force_locked.pop(fp, None)

    code = _new_code()
    _sync_from_disk()
    _state.pending[fp] = PendingCode(
        code=code,
        task=task,
        purpose=purpose,
        expires=time.time() + CODE_TTL,
    )
    _flush_to_disk()

    subject = "【危险操作验证码】"
    body = (
        f"有人要求执行一个危险操作，需要你的确认。\n\n"
        f"操作内容：\n{task[:500]}\n\n"
        + (f"原因：{purpose}\n\n" if purpose else "")
        + f"验证码：{code}\n\n"
        f"这个码 {CODE_TTL // 60} 分钟内有效，只能用一次。\n\n"
        f"⚠️ 如果这不是你要求的，**什么都不要做**，\n"
        f"  并且立刻去检查你的账号有没有被别人登录。\n"
    )

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(("电脑端", c.get("smtp_user") or c.get("email")))
        msg["To"] = c["email"]

        host = c.get("smtp_host")
        port = int(c.get("smtp_port") or 465)

        # ⚠️ 端口决定用不用 SSL，不能靠那个从没被写过的标记。
        #
        #    原来写的是：
        #        use_ssl = port == 465 or bool(c.get("smtp_ssl", True))
        #
        #    而 smtp_ssl 这个键**全仓没有一处写它** —— 永远是默认值 True。
        #    于是后半句恒真：不管端口多少都走 SMTP_SSL。
        #
        #    而预置表里这几家是 587，必须 STARTTLS：
        #        outlook / hotmail / live       smtp-mail.outlook.com:587
        #        office365                      smtp.office365.com:587
        #        icloud                         smtp.mail.me.com:587
        #    走 SMTP_SSL 连 587 一定失败 → 这几家用户永远收不到验证码。
        #
        #    现在按端口判：
        #        465        → SSL（SMTPS，最老也最常见）
        #        587 / 25   → 明文连接 + STARTTLS
        #        其他       → 有显式 smtp_ssl 就听它，没有就按端口猜
        _explicit = c.get("smtp_ssl")
        if port == 465:
            use_ssl = True
        elif port in (587, 25, 2525):
            use_ssl = False if _explicit is None else bool(_explicit)
        else:
            use_ssl = bool(_explicit) if _explicit is not None else False

        if use_ssl:
            server = smtplib.SMTP_SSL(
                host, port, timeout=20,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()

        try:
            if c.get("smtp_user") and c.get("smtp_pass"):
                server.login(c["smtp_user"], c["smtp_pass"])
            server.sendmail(
                msg["From"], [c["email"]], msg.as_string()
            )
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass

        return True, f"验证码已发到 {_mask_email(c['email'])}"
    except Exception as e:  # noqa: BLE001
        # 发送失败要把 pending 撤掉，别留个永远验不过的码
        _state.pending.pop(fp, None)
        return False, f"发邮件失败：{type(e).__name__}"


def _mask_email(addr: str) -> str:
    """把邮箱遮一半，界面上显示用。"""
    if not addr or "@" not in addr:
        return "***"
    name, domain = addr.split("@", 1)
    if len(name) <= 2:
        return f"{name[0]}***@{domain}"
    return f"{name[:2]}***@{domain}"


# 「解锁任务端」这件事的固定目标名。
#
# 为什么需要：
#     锁死时发的码，pending 是按 task 的指纹存的。
#     而用户验证时传的 task 跟他当时说的那句话有关，
#     两边对不上 → 码永远验不过 → 环境真的死了。
#
#     实测确认：锁死发的码记在 "任务端锁死后的解锁" 的指纹上，
#     而前台验的时候传的是具体任务，指纹不同。
#
# 所以「解锁任务端」用这个固定的目标，不跟任何具体任务绑定。
UNLOCK_TARGET = "__unlock__"


def unlock_with_code(code: str) -> tuple[bool, str]:
    """用验证码解锁整个任务端。

    跟 verify(task, code) 的区别：
        verify        绑定某一件具体的活
        unlock_with_code  专门解锁"任务端锁死"这个状态

    成功之后：所有锁清空 + 拦截计数清零。
    """
    ok, msg = verify(UNLOCK_TARGET, code)
    if not ok:
        return False, msg

    # 验过了：把所有锁都清掉
    _locked.clear()
    _force_locked.clear()
    _state.locked_until.clear()
    _state.pending.pop(_fingerprint(UNLOCK_TARGET), None)

    # 拦截计数也清零——"验证码拿到，数字清零"
    try:
        from guard import guard_gate as _gg

        _gg.reset_after_pass()
    except Exception:  # noqa: BLE001
        pass

    # 清掉"这次锁死发过码"的标记，下次锁死能重新发
    try:
        d = _load_state()
        d.pop("_trip_code_at", None)
        _save_state(d)
    except Exception:  # noqa: BLE001
        pass

    _flush_to_disk()
    return True, "解锁了，任务端可以正常派活了。"

def reset_all() -> None:
    """把所有安全状态清干净：内存 + 磁盘 + 拦截计数。

    ⚠️ 为什么要有这个函数：

        状态现在分三处放：
            内存里的几个字典
            %APPDATA%/dsh-bridge/lock_state.json   （跨进程共享）
            拦截计数（count_store 自己管一个文件）

        每加一个新的状态文件，所有"清理点"都得跟着改一遍。
        实测漏了一次：
            test_stress.py 清了内存，没清 lock_state.json，
            于是 check() 一同步又读回来 → 测试从第 100 轮开始失败。

        测试之间互相污染只是表象，根子是**没有单一入口**。

        现在一条线清干净，谁要清就调这个。
    """
    # 一、内存
    _state.pending.clear()
    _state.locked_until.clear()
    _state.fails.clear()
    _locked.clear()
    _force_locked.clear()
    _verified.clear()

    # 二、磁盘上的共享状态
    #
    #    不删文件，改成写一个新令牌——
    #    别的进程 sync 时看到令牌变了就会自己清空。
    #    直接删文件的话，别的进程根本不知道发生过什么。
    global _seen_reset_token
    import secrets as _secrets

    tok = _secrets.token_urlsafe(8)
    _seen_reset_token = tok
    try:
        _save_state({
            "locked": {},
            "force_locked": {},
            "verified": {},
            "pending": {},
            "_reset_token": tok,
            "_saved_at": int(time.time()),
        })
    except Exception:  # noqa: BLE001
        pass

    # 三、拦截计数
    try:
        from guard import guard_gate as _gg

        _gg.reset_after_pass()
    except Exception:  # noqa: BLE001
        pass


def verify(task: str, code: str, who: str = "") -> tuple[bool, str]:
    """验码。返回 (通过, 提示)。

    who 用来记录失败次数，防止暴力猜。
    """

    _sync_from_disk()
    now = time.time()

    # 锁了吗
    until = _state.locked_until.get(who or "_", 0)
    if until > now:
        left = int(until - now)
        return False, f"试错太多次，{left} 秒后再试"

    fp = _fingerprint(task)
    p = _state.pending.get(fp)
    if p is None:
        return False, "没有待验证的操作（可能过期了，重新发起一次）"

    if p.expires < now:
        # 码过期了：删掉它，然后**锁住**。
        #
        # 这里踩过两次坑，都是"两处逻辑不一致"：
        #
        #   坑一（红队 G 组）：
        #       这里原来只删 pending、没上锁，而 _expire_pending()
        #       同一步是"删 + 锁"。于是走 verify 这条路过期不锁，
        #       等于码一过期马上就能要新的——攻击者可以让码不停过期、
        #       不停重发，每次重发都是一次猜的机会。
        #
        #   坑二（锁多久）：
        #       修坑一的时候照着 _expire_pending 锁了 10 分钟。
        #       后来规则改成"锁到验证通过为止"，两处又不一样了。
        #       现在两边都是 LOCK_FOREVER。
        #
        # 为什么是 LOCK_FOREVER 而不是 10 分钟：
        #   10 分钟对攻击者是"稍等继续"，对普通用户毫无意义
        #   （他本来就不会把码拖到过期）。用户的原话：
        #       「锁死十分钟后解锁，或者这个锁死直接锁死，
        #         必须要验证码，不给验证码，那就什么都干不了。」
        _state.pending.pop(fp, None)
        _locked[fp] = LOCK_FOREVER
        _flush_to_disk()
        return False, "验证码过期了"

    if p.tries >= MAX_TRIES:
        _state.pending.pop(fp, None)
        _state.locked_until[who or "_"] = now + LOCK_SECONDS
        return False, f"这个码试错太多次，锁 {LOCK_SECONDS // 60} 分钟"

    p.tries += 1

    if not secrets.compare_digest(str(code).strip(), p.code):
        left = MAX_TRIES - p.tries
        if left <= 0:
            # 错满三次：**强制锁**
            #
            # 跟普通过期不一样：
            #     过期 → 可以重新发码
            #     错三次 → 十分钟内**不许重发**
            #
            # 因为错三次可能是有人在猜。再给他重发的权利，
            # 等于让他在十分钟里试 10 万种组合。
            _state.pending.pop(fp, None)
            _state.locked_until[who or "_"] = now + LOCK_SECONDS
            # ⚠️ 连错三次 = 有人在猜的可能性很高。
            #    原来锁 10 分钟，等于给他十分钟后再猜的机会。
            #    现在锁到"重新验证通过"为止。
            _force_locked[fp] = LOCK_FOREVER
            _flush_to_disk()
            return False, (
                f"验证码连错 {MAX_TRIES} 次，锁死了。\n"
                f"要重新获取验证码，得先在桌面端「危险操作验证」里"
                f"走一次重新验证。\n"
                f"这条锁不会自己过期——就是为了防有人猜。"
            )
        return False, f"验证码不对（还能试 {left} 次）"

    # 通过：一次性，立刻作废；同时记下"这个任务允许执行了"
    _state.pending.pop(fp, None)

    # ⚠️ 把所有的锁都清掉，包括共享文件里的。
    #
    #    用户的规则：「验证码拿到，数字清零」。
    #
    #    原来只 pop 了 pending。而 _locked / _force_locked 还在，
    #    于是"验证通过"之后任务端还是锁着——
    #    用户会觉得"我明明验了怎么还不行"。
    #
    #    锁死有三条来源：
    #        _locked[fp]        码过期了
    #        _force_locked[fp]  连错三次
    #        _force_locked["_all"]  全局锁死
    #    验证通过要把这三条都清掉。
    _locked.pop(fp, None)
    _force_locked.pop(fp, None)
    _force_locked.pop("_all", None)
    _state.locked_until.clear()
    _state.fails.clear()

    mark_verified(task)

    # 顺手把计数也清零——"验证码拿到，数字清零"
    try:
        from guard import guard_gate as _gg

        _gg.reset_after_pass()
    except Exception:  # noqa: BLE001
        pass

    _flush_to_disk()
    return True, "验证通过"


def has_pending(task: str) -> bool:
    """这个任务有没有正在等验证的码。"""
    _sync_from_disk()
    p = _state.pending.get(_fingerprint(task))
    if p is None:
        return False
    if p.expires < time.time():
        _state.pending.pop(_fingerprint(task), None)
        return False
    return True
