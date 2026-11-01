"""把「拦截计数 → 三次锁死」接进 guard。

老爸的设计
    锁不是状态，是**关卡**。每次派活都要过。

        派活   → 锁检测 → 正常 → 放行
        干活   → 锁检测 → 正常 → 过
        交割   → 锁检测 → 正常 → 推回

    检测到不安全：

        第 1 次 → 拦截，告知用户
        第 2 次 → 拦截，告知用户
        第 3 次 → **彻底锁死任务端**，走原来那套验证码机制

    计数的规矩（这是关键）：

        不按"同一件事"计数 —— 攻击者会不停换花样试，
        每一种只试一次就永远到不了三次。

        按**当天总次数**计数 —— 换什么花样都算一次。

        验证码通过 / 锁满 10 分钟 → 计数重置。

        换来的是：攻击者要突破就得反复等 10 分钟、反复拿邮箱验证码 ——
        时间成本。

计数的存储
    走 count_store：主文件 + 影子 + 签名 + 标记文件。
    任务端改哪个都会被回滚，全改了按拦满处理。
"""

from __future__ import annotations

import time


def _store():
    """延迟导入 count_store。

    为什么不在模块顶层 import：
        count_store 会读密钥、碰 DPAPI，在 import 阶段做这些
        会让 guard 的加载变慢，而且一旦出错连 guard 都用不了。
    """
    try:
        from guard import count_store as c

        return c
    except Exception:  # noqa: BLE001
        return None


# ══════════════════════════════════════════════════════
# 拦截计数
# ══════════════════════════════════════════════════════

# 当天被拦多少次就彻底锁死。
#
# 为什么是 3：
#   第 1 次可能是用户口误、用词夸张（"删了算了"其实是想重写）。
#   第 2 次给他确认的机会。
#   第 3 次就说明确实在反复尝试了 —— 要么是攻击，要么是真要干危险的事，
#   两种都该走验证码。
TRIP_COUNT = 3


def intercept(task: str, reason: str = "") -> dict:
    """记一次拦截。

    返回：
        {
            "count": 今天第几次被拦
            "tripped": 是不是已经到三次了（该锁死了）
            "message": 给用户看的话
        }
    """
    store = _store()

    if store is None:
        # 存储不可用：保守处理，直接当成已触发。
        #
        # ⚠️ 这里故意"从严"：
        #    存储坏了要是不可能计数，那"三次锁死"就永远不会触发。
        #    宁可让用户多走一次验证码，也不能让攻击者因为"存储坏了"得手。
        return {
            "count": TRIP_COUNT,
            "tripped": True,
            "message": "拦截计数不可用，按最严处理。这是第 3 次，任务端锁死。",
        }

    try:
        # ⚠️ 先看是不是已经锁死了。
        #
        #    用户的规矩：「到 3 锁死……永远不可能超过 3」。
        #
        #    原来这里一进来就 bump()，不看自己。
        #    结果跑到 3 之后还能变成 4、5、6、9、12——
        #    数字一直涨，而用户看到的提示还是老样子。
        #
        #    实测撞到过：计数涨到 12，用户问"为什么数字能到 9"。
        #
        #    锁死之后不再累加：数字停在那，意义就是"到顶了"。
        _before = store.peek()
        _n = int((_before or {}).get("count", 0))
        if _n >= TRIP_COUNT:
            _hint = _ensure_trip_code()
            return {
                "count": _n,
                "tripped": True,
                "tamper": int((_before or {}).get("tamper", 0)),
                "message": (
                    f"今天这是第 {_n} 次被拦下了，任务端锁死。\n"
                    f"{_hint}"
                ),
            }

        d = store.bump()
        n = int(d.get("count", 0))
        tamper = int(d.get("tamper", 0))

        # 锁死之后数字就停在 TRIP_COUNT，不再往上爬
        if n > TRIP_COUNT:
            n = TRIP_COUNT

        tripped = n >= TRIP_COUNT

        if tripped:
            # ⚠️ 锁死的那一刻必须**真的发码**，不能只说"验证码发到邮箱"。
            #
            #    原来只写了这句话，用户去邮箱找——没有。
            #    说了做不到，比不说更糟：用户会以为邮箱配错了、
            #    或者以为软件坏了。
            #
            #    现在调 _ensure_trip_code()，它会真发，并且防抖
            #    （60 秒内只发一次，不会连发三封）。
            _hint = _ensure_trip_code()
            msg = (
                f"今天这是第 {n} 次被拦下了，任务端锁死。\n"
                f"{_hint}"
            )
        elif n == 1:
            msg = (
                f"这个活看着不太安全：{reason or '涉及不可逆操作'}\n"
                f"这是今天第 1 次被拦。真要做的话跟我说清楚，我再确认一次。"
            )
        else:
            left = TRIP_COUNT - n
            msg = (
                f"又拦下一件不太安全的活：{reason or '涉及不可逆操作'}\n"
                f"今天已经拦了 {n} 次，还剩 {left} 次机会——再拦一次任务端就锁死了。"
            )

        if tamper > 0:
            # 篡改痕迹要暴露出来，别藏着。
            msg += f"\n（注意：拦截计数文件被改过 {tamper} 次，已经自动恢复了。）"

        return {"count": n, "tripped": tripped, "message": msg, "tamper": tamper}

    except Exception as e:  # noqa: BLE001
        return {
            "count": TRIP_COUNT,
            "tripped": True,
            "message": f"计数出错（{type(e).__name__}），按最严处理。",
        }


def peek_count() -> dict:
    """看一眼今天被拦了几次，不改。"""
    store = _store()
    if store is None:
        return {"count": 0, "tripped": False, "tamper": 0, "available": False}
    try:
        d = store.peek()
        n = int(d.get("count", 0))
        return {
            "count": n,
            "tripped": n >= TRIP_COUNT,
            "tamper": int(d.get("tamper", 0)),
            "available": True,
        }
    except Exception:  # noqa: BLE001
        return {"count": 0, "tripped": False, "tamper": 0, "available": False}


def reset_after_pass() -> None:
    """验证码通过、或者锁满之后，把当天计数清零。

    这是设计的一部分：不重置的话，用户一天只能犯规一次，
    之后每一个小口误都会让他锁死。
    """
    store = _store()
    if store is None:
        return
    try:
        store.reset_count()
    except Exception:  # noqa: BLE001
        pass


# ══════════════════════════════════════════════════════
# 关卡：每次派活都要过
# ══════════════════════════════════════════════════════

def _guard():
    """拿 guard 模块。延迟导入，避免循环依赖。"""
    from guard import guard as g

    return g


# 上次给"锁死"这件事发码的时间。
#
# 为什么要有这个：
#     锁死之后每次派活都会走到"该发码"这一步。
#     用户连发三条消息 → 收到三封邮件。
#     挡一下：60 秒内只发一次。
_last_trip_mail = 0.0
_TRIP_MAIL_GAP = 60.0


def _ensure_trip_code() -> str:
    """锁死时确保码发出去了。

    两条路共用这一个入口：
        gate()     派活时发现计数到顶
        intercept() 执行时撞满第三次

    为什么要"ensure"这个说法：
        发码带防抖（60 秒内只发一次）。
        这个函数可以放心重复调，不会刷屏。
    """
    return _send_trip_code()


def _send_trip_code() -> str:
    """锁死时发一次验证码。返回给用户看的那句话。

    ⚠️ 同一次锁死只发一封。

        原来是 60 秒防抖：
            锁死之后每被拦一次，只要超过 60 秒就再发一封。
            用户连续操作几分钟 → 收到一串验证码。

            用户的原话：
                「刚才为啥我会收到连串的验证码呀？」

        而且那个防抖变量在进程内存里：
            AstrBot 和桥接口是两个进程，各有各的，
            互相不知道对方发过 → 两边各发一遍。

        现在改成按**状态**判：
            这次锁死发过一次码了，就不再发，
            直到验证通过 / 重置才清掉这个标记。

            标记落盘（lock_state.json），两个进程共享。

    ⚠️ 发不出去也不能让派活流程炸掉——
        守卫本身不可用的时候，gate 那边已经有"从严"的处理了。
        这里失败就返回一个说明，别抛。
    """
    import time as _t

    # 这次锁死还有没有"能用的"码？
    #
    # ⚠️ 判断依据是**码本身还在不在**，不是"发过多久"。
    #
    #    原来写的是"10 分钟内发过就不发"。
    #    止住了连串验证码，但把另一条路堵死了：
    #        用户 120 秒内没来得及给码 → 码过期 → pending 删了 →
    #        锁还在 → 再要码 → "已经发过了" → 
    #        用旧码 → "没有待验证的操作"
    #    三条路全断，环境死在里面。
    #
    #    用户的原话：
    #        「这个验证码不是 120 秒嘛？120 秒内没给，
    #          后面不还是锁死嘛？这个时候要解除，怎么解除呢？」
    #
    #    发过一封 ≠ 这一封还能用。
    try:
        from guard import guard as _g

        fp = _g._fingerprint(_g.UNLOCK_TARGET)
        p = _g._state.pending.get(fp)
        if p is not None and p.expires > _t.time():
            # 码还活着 → 不重复发
            left = int(p.expires - _t.time())
            return (
                f"验证码已经发到你邮箱了，还有 {left} 秒有效。\n"
                "翻一下最近那封；拿到之后在桌面端「危险操作验证」里填进去。"
            )
        # 码没了/过期了 → 往下走，重新发一封
    except Exception:  # noqa: BLE001
        pass

    try:
        g = _guard()
        # 用固定目标 "__unlock__" 发码，不跟任何具体任务绑定。
        #
        # 原来发的是 "任务端锁死后的解锁" 这个字符串，
        # 而用户验证时传的是具体任务的描述——指纹对不上，
        # 码永远验不过。
        ok, msg = g.send_code_email(
            g.UNLOCK_TARGET,
            "解锁任务端（今天被拦满三次）",
        )
        if ok:
            # 记下"这次锁死已经发过码了"，落盘让另一个进程也看到
            try:
                _d = g._load_state()
                _d["_trip_code_at"] = _t.time()
                g._save_state(_d)
            except Exception:  # noqa: BLE001
                pass
            return (
                "验证码已经发到你配置的邮箱了。\n"
                "拿到码之后在桌面端「危险操作验证」里填进去。"
            )
        return f"验证码没发出去：{msg}"
    except Exception as e:  # noqa: BLE001
        return f"验证码没发出去（{type(e).__name__}）。去桌面端看「危险操作验证」那一栏。"


def gate(task: str) -> dict:
    """派活前的关卡。

    这是所有派活入口唯一的必经之路。

    返回：
        {
            "allow": 能不能过
            "reason": 为什么
            "count": 今天第几次
        }

    三种情况：
        一、当前有围栏（验证码等着填 / 锁着）→ 不许过
        二、任务看着不安全 → 记一次拦截，不许过，告诉他第几次了
        三、其他 → 放行
    """
    # 一、先看有没有正在生效的围栏
    g = _guard()
    st = g.check(task)

    # ⚠️ 顺序很重要：**先判计数锁死，再判围栏**。
    #
    #    原来反了（先 fenced 直接 return），而 check() 现在把
    #    "计数锁死"也算进了 fenced —— 于是锁死时走第一个分支
    #    直接返回，**永远到不了下面"发码"那一段**。
    #
    #    用户的原话：
    #        「锁死了，跟前台再次派活，前台派不出去，
    #          那就调用验证码，并告诉用户，验证码输入解锁。」
    #
    #    现在把锁死放在前面：派不出去 → 顺手把码发了 → 告诉他。
    _c = peek_count()
    if _c.get("tripped"):
        hint = _send_trip_code()
        return {
            "allow": False,
            "reason": (
                f"今天已经被拦了 {_c['count']} 次，任务端锁死，派不出去。\n"
                f"{hint}"
            ),
            "count": _c["count"],
            "tripped": True,
            "code_sent": "邮箱" in hint or "已经发到" in hint,
        }

    if st.get("fenced"):
        return {
            "allow": False,
            "reason": st.get("reason") or "任务端锁着",
            "count": peek_count().get("count", 0),
            "fenced": True,
        }

    # 二、计数锁死已经在前面判过了，这里不用再看。

    # 三、**不再靠任务描述判断危险。**
    #
    #    原来这里有一句 looks_dangerous(task)——按关键词猜。
    #    后来发现这条路走不通：
    #
    #        · 危险的说法列不完。"改个权限"就没有对应的词，
    #          而它确实不可逆。
    #        · 任务描述会骗人。有人让你派活，写个无关紧要的描述就行。
    #
    #    所以判断挪到了**动作层**（engine/agent.py 的 _check_action）：
    #    看它实际调了什么工具、传了什么参数。
    #    工具调用做不了假——真要删文件，就必须调删除。
    #
    #    这里只做两件事：
    #        1. 围栏开着吗（上面第一步）
    #        2. 今天拦截次数到顶了吗（上面第二步）
    #
    #    到这一步了就是放行。真正的把关在动作层。
    return {"allow": True, "reason": "", "count": _c.get("count", 0)}
