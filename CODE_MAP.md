# 代码在哪

**找东西先看这份。**

---

## 一句话导航

| 我要改… | 去这里 |
|---|---|
| 派活、执行、任务状态 | `core/bridge.py` |
| 引擎怎么调模型、怎么循环 | `engine/agent.py` |
| 引擎的工具（读写文件、跑命令） | `engine/tools.py` |
| 什么东西不许做 | `guard/action_guard.py` |
| 验证码、三次锁死 | `guard/guard.py`、`guard/guard_gate.py` |
| 窗口权限级别 | `guard/task_perms.py` |
| 桌面界面 | `desktop/app.py` |
| HTTP 接口 | `server/api_server.py` |
| 平台差异（Windows/Linux） | `tools/platform_utils.py` |

---

## 目录说明

### `core/` — 桥的本体

| 文件 | 管什么 |
|---|---|
| `bridge.py` | 派活、执行、查状态。**命令行入口。** |
| `engine_bridge.py` | 把自研引擎接到桥上 |
| `recover.py` | 启动时把"进程没了但还显示运行中"的任务纠正 |
| `outputs.py` | 收集任务产出的文件 |

**数据文件（`tasks.jsonl`、`results.jsonl`、`state/`、`workspaces/`）在**仓库根目录**，不在 `core/` 里。**

### `guard/` — 安全

**八个文件都在回答同一个问题：什么不许做、做了会怎样、能不能拦回来。**

| 文件 | 管什么 |
|---|---|
| `action_guard.py` | 动作守卫。判执行口，不判命令长什么样 |
| `guard.py` | 验证码门。生成、校验、过期、锁 |
| `guard_gate.py` | 把"拦截计数 → 三次锁死"接进 guard |
| `count_store.py` | 防篡改的计数（主文件 + 影子副本 + 签名） |
| `task_perms.py` | 任务端权限级别（workspace / home / full） |
| `safe_path.py` | 任务名、目录名的安全校验 |
| `security.py` | 本机 HTTP 接口的防护（token、Host、Origin） |
| `guard_git.py` | 别让 AI 的临时文件混进 git |

### `server/` — 对外接口

| 文件 | 管什么 |
|---|---|
| `api_server.py` | HTTP 接口，给桌面面板用。只监听 127.0.0.1 |

**`_app_dir()` 返回的是**仓库根**，不是 `server/`——运行数据一直在根目录。**

### `tools/` — 杂项

| 文件 | 管什么 |
|---|---|
| `deps.py` | 运行环境检查、依赖安装 |
| `tune_model.py` | 切换任务端用的模型 |
| `watchdog.py` | 服务守护 |
| `platform_utils.py` | 平台差异（文件权限、查进程、无窗口启动） |

### `desktop/` — 桌面端

| 文件 | 管什么 |
|---|---|
| `app.py` | 主界面（原来的 `desktop_qt.py`） |
| `paths.py` | 程序装在哪、配置存在哪 |
| `channels.py` | QQ / 飞书通道启停 |
| `launcher.py` | 起停服务 |
| `svc_flow.py` | 启动流程 |
| `hide_console.py` | 隐藏黑窗口 |

### `engine/` — 自研引擎

**任务端不用装 DeepSeek Harness，就用这个。**

| 文件 | 管什么 |
|---|---|
| `agent.py` | 工具循环：调模型 → 执行工具 → 结果回灌 → 再调 |
| `llm.py` | 任何 OpenAI 兼容接口 |
| `tools.py` | 六个工具：读、写、改、列、搜、跑命令 |
| `session.py` | 会话存盘、上下文压缩 |
| `profiles.py` | 多套模型配置 |
| `scheduler.py` | 并发调度 |
| `ratelimit.py` | 按配置限流 |

### `tests/` — 测试

| 文件 | 条数 |
|---|---|
| `test_safe_path.py` | 92 |
| `test_action_guard.py` | 50 |
| `test_guard.py` | 41 |
| `test_security.py` | 43 |
| `test_engine.py` | 41 |
| `test_outputs.py` | 36 |
| `test_concurrency.py` | 15 |
| `test_stress.py` | 10 |
| `test_field_bugs.py` | 14 |
| `brute_force.py` | 142 |
| `redteam_ext.py` | 316 |
| `smoke.py` | 11 |
| `run_all.py` | 跑全部 |

---

## 常见改动去哪

**改派活流程** → `core/bridge.py` 的 `cmd_dispatch` / `cmd_run`

**改工具能力** → `engine/tools.py`

**加一条安全规则** → `guard/action_guard.py` 的 `_ARBITRARY_EXEC` / `_IRREVERSIBLE` / `_SYSTEM_DAMAGE`

**改权限档位** → `guard/task_perms.py` 的 `LEVELS`

**改界面** → `desktop/app.py`，每个页面一个 `_build_xxx_page`

**改接口** → `server/api_server.py`，路由在 `do_GET` / `do_POST`

---

## 三个容易踩的地方

**一、`guard` 同时是包名和模块名**

```
✅  from guard import task_perms
❌  from guard.guard import task_perms
```

**批量改 import 时要小心这个。**

**二、脚本模式下 `sys.path` 只有自己那一层**

`python core/bridge.py` 跑的时候，`sys.path[0]` 是 `core/`。

所以 `bridge.py` 和 `api_server.py` 开头都主动补了仓库根目录。

**三、运行数据不跟代码走**

`tasks.jsonl` 这些在**仓库根**。分成包之后按 `__file__` 算会跑偏——
等于升级一次把历史任务全丢了。
