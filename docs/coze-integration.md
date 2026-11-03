# 接到扣子（Coze）

本文说明怎么让扣子当门面，任务打到**你自己的电脑**上跑。

---

## 原理

```
扣子网页 / 桌面端
    ↓  WebSocket（字节私有的 Frontier 协议）
coze-bridge（官方 npm 包，跑在你电脑上）
    ↓  ACP 协议（JSON-RPC 2.0，stdin/stdout）
engine/acp_server.py（我们的安全执行端）
    ↓
engine/agent.py → 模型 + 工具 + 全部安全层
```

**我们只做最后一层。**

前面两层（聊天界面、长连接）都是扣子自带的，不用我们写。

---

## 为什么这样接

| | 以前 | 现在 |
|---|---|---|
| 聊天入口 | 自己做推送插件、配平台 | 扣子自带 |
| 长连接 | 自己做 HTTP 桥 | coze-bridge 自带 |
| 部署 | 装一堆东西 | 装一个 npm 包 |
| 安全 | **我们的** | **还是我们的** |

**丢掉的是重复造轮子的部分，保住的是真正值钱的部分。**

---

## 接入步骤

### 一、装 coze-bridge

```bash
npm install -g coze-bridge
```

### 二、在扣子拿连接命令

1. 打开 [扣子](https://www.coze.cn)
2. Agent 列表 → ➕ → **新建 Agent**
3. 点 **接入本地 Agent**
4. 点 **复制命令**

拿到的命令长这样：

```bash
npx -y coze-bridge@latest --pat-token=sat_xxx --pair-code=yyy
```

**⚠️ token 只有 60 分钟有效期。**复制了赶紧跑。

### 三、告诉 coze-bridge 我们的 Agent 在哪

**coze-bridge 默认只找它认识的几个 Agent**（Claude Code、Codex、OpenClaw）。

要接我们自己的，得让它知道怎么起我们的进程。

> **本步骤的确切做法还在确认中。**
> coze-bridge 的配置方式没有公开文档，我们是从它的源码分析里推断的。
> 下面给的是**当前已知最可能的方式**，实测后会更正。

**方式 A：如果它支持 `--agent-command` 之类的参数**

```bash
npx -y coze-bridge@latest \
  --pat-token=sat_xxx \
  --pair-code=yyy \
  --agent-command="python D:/Project/dsh-bridge-oss/engine/acp_server.py"
```

**方式 B：如果是配置文件**

一般会在 `~/.coze/bridge/` 下。找 `config.json` 之类的文件，
把我们的命令写进去。

**方式 C：如果只认那几个固定名字**

那就得包一层 —— 做一个叫 `claude-agent-acp` 的假包，
里面转调我们的服务端。

---

## 我们的服务端怎么跑

**单独手动跑（调试用）**：

```bash
python engine/acp_server.py
```

它会读 stdin，往 stdout 写 JSON 帧，日志全走 stderr。

**你可以手工喂一帧试试**：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}' | python engine/acp_server.py
```

应该看到（stdout）：

```json
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentInfo":{...},"agentCapabilities":{...}}}
```

---

## 协议要点（给改代码的人看）

**规范**：https://agentclientprotocol.com/protocol/v1/overview.md

| 要点 | 说明 |
|---|---|
| 传输 | JSON-RPC 2.0，stdin/stdout，每帧一行 JSON |
| 字段命名 | 协议帧里用 camelCase；枚举值用 snake_case |
| 日志 | **必须走 stderr**。写 stdout 会污染协议帧 |
| 路径 | 协议里的路径**必须是绝对路径** |
| 通知 | `session/cancel` 和 `session/update` 是通知，**不能回响应** |

**我们实现了四个方法**：

| 方法 | 类型 | 做什么 |
|---|---|---|
| `initialize` | 请求 | 握手，报版本和能力 |
| `session/new` | 请求 | 建会话 |
| `session/prompt` | 请求 | 跑一轮任务 |
| `session/cancel` | **通知** | 取消（不回响应） |

**我们没实现**（能力里如实报 `false`，客户端不会调）：

- `fs/read_text_file` / `fs/write_text_file` —— 文件访问由 engine 自己管（走 `guard/task_perms.py`）
- `session/request_permission` —— 守卫在本地判，不依赖客户端
- `session/load` —— 不恢复历史会话
- `terminal/*` —— 命令执行在 engine 里，不走客户端

---

## 安全层在哪里

**扣子只能看到"结果"，看不到执行过程。**

```
扣子发一句"把 report 目录清理一下"
    ↓
我们的 agent 决定跑 rm -rf report
    ↓
**守卫拦住**（或影子执行兜住 / 审计员发现不对）
    ↓
扣子收到的是一句"这个操作我没做，因为……"
```

**扣子不知道、也不需要知道中间发生了什么。**

所有安全机制（影子执行、动作守卫、审计员、出口管控、版本回退）**原样生效**。

---

## 还没解决的问题

1. **coze-bridge 怎么配置自定义 Agent** —— 见上面的方式 A/B/C，要实测
2. **流式输出** —— 现在是一轮跑完才回，长任务会显得卡
   - 改进方向：`session/update` 里已经在发 `tool_call` 进度了，
     可以把模型输出的 chunk 也流式发出去
3. **一次多轮对话** —— 现在每次 `session/prompt` 都当独立任务
   - 规范支持多轮（用同一个 `sessionId` 接着聊）
   - 我们的 `engine/sess.py` 有历史，接上就支持了

---

## 参考

- ACP 官方规范：https://agentclientprotocol.com/protocol/v1/overview.md
- Prompt Turn 流程：https://agentclientprotocol.com/protocol/v1/prompt-turn.md
- 扣子本地 Agent 文档：https://docs.coze.cn/cozespace_access_openclaw
