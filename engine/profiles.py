"""任务端的模型配置：支持多套，随时切换。

为什么改成多套
    原来只有一套配置，想换模型就得把地址、Key、模型名全重填一遍。
    但实际使用中会想要：
        · 便宜的任务用便宜的模型
        · 难的活换成强的
        · 手上同时有几个厂商的 Key

    所以改成"配置列表"：
        每个配置有名字、地址、Key、模型
        有一个"当前用哪个"
        界面上能加、能删、能切换

存储格式（在用户目录下的 dsh-bridge/engine.json）
    {
      "active": "kimi",                 ← 当前用哪个（按 id）
      "profiles": [
        {"id": "kimi", "name": "Kimi", "base_url": "...", "api_key": "...",
         "model": "kimi-k2.6", "max_steps": 40},
        ...
      ]
    }

    兼容旧格式：如果读到的是老的单套配置（顶层就有 base_url），
    自动转成列表里的一项，不丢用户已经填好的东西。
"""

from __future__ import annotations

import json
import os
import time

APP_NAME = "dsh-bridge"

DEFAULT_MAX_STEPS = 40


def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "engine.json")


def _new_id() -> str:
    return f"p{int(time.time() * 1000)}"


DEFAULT_CONCURRENCY = 1


def _blank_profile(name: str = "") -> dict:
    return {
        "id": _new_id(),
        "name": name or "新配置",
        "base_url": "",
        "api_key": "",
        "model": "",
        "max_steps": DEFAULT_MAX_STEPS,
        # 这套配置有没有启用。多套可以同时启用——它们不是"切换"关系，
        # 是"同时都在干活"，各自有各自的并发额度。
        "enabled": True,
        # 这套最多同时跑几个任务。满了就排队。
        # 默认 1：保守，用户自己按服务商的限额往上加。
        "concurrency": DEFAULT_CONCURRENCY,
    }


def load_all() -> dict:
    """读全部配置。老格式会自动迁移。"""
    p = config_path()
    data: dict = {}

    def _read(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    if os.path.exists(p):
        data = _read(p)

    if not isinstance(data, dict):
        data = {}

    # ⚠️ 主配置空着，但备份有东西 → 用备份。
    #
    #    什么情况下会这样：
    #        · 写的时候断电/被杀，留下一个空文件
    #        · 某个 bug 把配置清空了
    #
    #    备份的内容如果比现在多（有 profiles，现在没有），
    #    说明主文件出了问题，拿备份顶上。
    # 只有"看着像损坏"的时候才从备份恢复。
    #
    # 判据：主文件里没有 _saved_at 标记，而且 profiles 是空的。
    #
    #   有标记（正常写过一次）→ 哪怕是空列表也照收，
    #                            那是用户真的把配置删光了。
    #   没标记 → 很可能是被截断的老文件/坏文件，才去看备份。
    looks_broken = not data.get("_saved_at") and not data.get("profiles")
    if looks_broken:
        bak = backup_path()
        if os.path.exists(bak):
            b = _read(bak)
            if isinstance(b.get("profiles"), list) and b["profiles"]:
                data = b
                # 顺手写回主文件，别每次都从备份读
                try:
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except OSError:
                    pass

    # 业务层不需要看到 _saved_at，读到后面统一去掉。
    # 放在最后处理，见函数结尾。

    # 已经是新格式
    if isinstance(data.get("profiles"), list):
        data.setdefault("active", "")
        # 补齐缺字段
        for prof in data["profiles"]:
            if not isinstance(prof, dict):
                continue
            prof.setdefault("id", _new_id())
            prof.setdefault("name", "未命名")
            prof.setdefault("base_url", "")
            prof.setdefault("api_key", "")
            prof.setdefault("model", "")
            prof.setdefault("max_steps", DEFAULT_MAX_STEPS)
            prof.setdefault("enabled", True)
            prof.setdefault("concurrency", DEFAULT_CONCURRENCY)
        # _saved_at 只是文件层面的标记，不往业务层传。
        # 传出去的话，调用方遍历 keys 时会看到一个莫名其妙的字段。
        data.pop("_saved_at", None)
        return data

    # 老格式：顶层就是一套配置
    if data.get("base_url") or data.get("api_key") or data.get("model"):
        prof = {
            "id": "default",
            "name": "默认",
            "base_url": data.get("base_url") or "",
            "api_key": data.get("api_key") or "",
            "model": data.get("model") or "",
            "max_steps": int(data.get("max_steps") or DEFAULT_MAX_STEPS),
            "enabled": True,
            "concurrency": DEFAULT_CONCURRENCY,
        }
        return {"active": "default", "profiles": [prof]}

    return {"active": "", "profiles": []}


def backup_path() -> str:
    """配置的备份文件。"""
    return config_path() + ".bak"


def save_all(data: dict) -> None:
    """存配置。

    ⚠️ 覆盖之前先留一份备份。
    """
    p = config_path()
    tmp = p + ".tmp"

    # 覆盖之前留旧的一份。
    #
    # 为什么：engine.json 里是用户填的模型地址、Key、模型名。
    # 一旦被写坏或者清空，用户得重新填一遍 Key——
    # 而 Key 往往记不住。
    #
    # 留一份 .bak，出事了还能捞回来。
    if os.path.exists(p):
        try:
            import shutil

            shutil.copy2(p, backup_path())
        except OSError:
            pass

    # ⚠️ 写一个"_saved_at"标记。
    #
    #    读的时候靠它区分两件事：
    #        有标记 + profiles 为空  → 用户真的删光了，正常
    #        没标记 + profiles 为空  → 文件可能被截断/写坏了，考虑从备份恢复
    #
    #    没有这个标记的话，"删到一条不剩"会被当成"文件坏了"，
    #    然后被备份原样救回来——删不掉。
    data = dict(data)
    data["_saved_at"] = int(time.time())

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def active_profile() -> dict:
    """当前用哪套。没有就返回空 dict。"""
    data = load_all()
    active = data.get("active") or ""
    for prof in data.get("profiles") or []:
        if prof.get("id") == active:
            return prof
    # active 指向不存在的东西时，退回第一个
    profiles = data.get("profiles") or []
    return profiles[0] if profiles else {}


def _norm_base(url: str) -> str:
    """把地址规范化，好比较。

    用户可能写成：
        https://api.deepseek.com
        https://api.deepseek.com/
        HTTPS://API.DEEPSEEK.COM/
    这三种是同一个东西。
    """
    u = (url or "").strip().lower().rstrip("/")
    # 去掉末尾的 /v1（有的写有的不写，其实指向同一个服务）
    if u.endswith("/v1"):
        u = u[:-3]
    return u


def find_duplicate(base_url: str, model: str = "",
                   exclude_id: str = "") -> dict | None:
    """找有没有"同一个地址 + 同一个模型"的另一套。

    ⚠️ 只按地址查重是错的——会误伤正常场景：
        同一个 https://api.deepseek.com 上，
        想同时配 deepseek-flash（跑简单活）
        和 deepseek-v4-pro（跑难活），这是合理的。

    所以判据是「地址 + 模型」两个都一样才算重复。
    空的地址或空的模型（还没填完）不参与判断。

    exclude_id：改的时候要排除自己。
    """
    nb = _norm_base(base_url)
    nm = (model or "").strip().lower()
    if not nb or not nm:
        return None

    for prof in load_all().get("profiles") or []:
        if prof.get("id") == exclude_id:
            continue
        if _norm_base(prof.get("base_url") or "") != nb:
            continue
        if (prof.get("model") or "").strip().lower() == nm:
            return prof
    return None


def find_name_taken(name: str, exclude_id: str = "") -> dict | None:
    """找有没有同名的另一套。

    为什么名字也得查：
        列表里两个都叫 Kimi，用户分不清哪个是哪个。
        派活的时候也说不清"给 Kimi 派"是给哪一个。
    """
    nn = (name or "").strip().lower()
    if not nn:
        return None

    for prof in load_all().get("profiles") or []:
        if prof.get("id") == exclude_id:
            continue
        if (prof.get("name") or "").strip().lower() == nn:
            return prof
    return None


def add_profile(prof: dict) -> dict:
    """加一套配置。返回新加的那套（带生成的 id）。"""
    data = load_all()
    newp = {
        "id": prof.get("id") or _new_id(),
        "name": (prof.get("name") or "新配置").strip(),
        "base_url": (prof.get("base_url") or "").strip(),
        "api_key": (prof.get("api_key") or "").strip(),
        "model": (prof.get("model") or "").strip(),
        "max_steps": int(prof.get("max_steps") or DEFAULT_MAX_STEPS),
        "enabled": bool(prof.get("enabled", True)),
        "concurrency": max(1, min(20, int(prof.get("concurrency") or DEFAULT_CONCURRENCY))),
    }
    # ── 两条检查 ──
    #
    # 用户定的规则：
    #     同一把 Key 建两遍  → 没意义，拦
    #     名字重名          → 分不清，拦
    #
    # 拦在引擎层而不只拦界面：别的调用方（命令行、脚本）
    # 也会走这里，界面上的检查挡不住它们。
    dup = find_duplicate(newp["base_url"], newp["model"])
    if dup is not None:
        newp["_error"] = (
            f"这个地址 + 模型已经有一份了（叫「{dup.get('name')}」）。"
            "想换 Key 就直接改那一套，不用新建。"
        )
        newp["_duplicate_of"] = dup.get("id") or ""
        return newp

    same = find_name_taken(newp["name"])
    if same is not None:
        newp["_error"] = f"名字「{newp['name']}」已经被另一套用了，换个名字"
        newp["_duplicate_of"] = same.get("id") or ""
        return newp

    data.setdefault("profiles", []).append(newp)
    # 第一套自动设为当前
    if not data.get("active"):
        data["active"] = newp["id"]
    save_all(data)
    return newp


def update_profile(pid: str, patch: dict) -> bool:
    """改一套配置。key 传空表示不改。

    改地址的时候也要查重——不能把 A 改成 B 的地址，
    那样列表里还是两个同地址的。
    """
    # 先查新地址会不会撞到别人
    newbase = patch.get("base_url")
    newmodel = patch.get("model")
    if isinstance(newbase, str) and newbase.strip():
        dup = find_duplicate(
            newbase,
            newmodel if isinstance(newmodel, str) else "",
            exclude_id=pid,
        )
        if dup is not None:
            return False
    # 名字也不能撞
    newname = patch.get("name")
    if isinstance(newname, str) and newname.strip():
        if find_name_taken(newname, exclude_id=pid) is not None:
            return False
    data = load_all()
    for prof in data.get("profiles") or []:
        if prof.get("id") != pid:
            continue
        for k in ("name", "base_url", "model"):
            if k in patch and isinstance(patch[k], str):
                prof[k] = patch[k].strip()
        # key 单独处理：空字符串表示"不改"，不是"清空"
        k = patch.get("api_key")
        if isinstance(k, str) and k.strip():
            prof["api_key"] = k.strip()
        if "max_steps" in patch:
            try:
                prof["max_steps"] = int(patch["max_steps"])
            except (TypeError, ValueError):
                pass
        if "enabled" in patch:
            prof["enabled"] = bool(patch["enabled"])
        if "concurrency" in patch:
            try:
                n = int(patch["concurrency"])
                prof["concurrency"] = max(1, min(20, n))
            except (TypeError, ValueError):
                pass
        save_all(data)
        return True
    return False


def delete_profile(pid: str) -> bool:
    """删一套。删的如果是当前用的，切到剩下的第一个。"""
    data = load_all()
    before = len(data.get("profiles") or [])
    data["profiles"] = [p for p in (data.get("profiles") or [])
                        if p.get("id") != pid]
    if len(data["profiles"]) == before:
        return False
    if data.get("active") == pid:
        data["active"] = data["profiles"][0]["id"] if data["profiles"] else ""
    save_all(data)
    return True


def set_active(pid: str) -> bool:
    data = load_all()
    if not any(p.get("id") == pid for p in (data.get("profiles") or [])):
        return False
    data["active"] = pid
    save_all(data)
    return True


def is_configured(prof: dict | None = None) -> bool:
    """这套配置能不能用（三样都填了）。"""
    p = prof if prof is not None else active_profile()
    return bool(p.get("base_url") and p.get("api_key") and p.get("model"))

# ----------------------------------------------------------------------
# 按 id 取、按启用状态筛
# ----------------------------------------------------------------------

def get_profile(pid: str) -> dict:
    """按 id 取一套。没有就返回空 dict。"""
    for prof in (load_all().get("profiles") or []):
        if prof.get("id") == pid:
            return prof
    return {}


def enabled_profiles() -> list[dict]:
    """所有启用的、而且配好了的配置。派活时从这里面挑。"""
    out = []
    for prof in (load_all().get("profiles") or []):
        if not prof.get("enabled"):
            continue
        if not (prof.get("base_url") and prof.get("api_key") and prof.get("model")):
            continue
        out.append(prof)
    return out


def default_profile() -> dict:
    """没指定用哪套时的默认选择。

    启用的里面挑第一个。全都没启用就退回第一套。
    """
    eps = enabled_profiles()
    if eps:
        return eps[0]
    profiles = load_all().get("profiles") or []
    return profiles[0] if profiles else {}
