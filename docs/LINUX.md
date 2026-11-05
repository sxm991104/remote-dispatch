# Linux 上跑

> 主 README 讲的是 Windows（带桌面端）。
> Linux 上没有图形界面，用命令行派活。

---

## Linux 上跑起来

**先说清楚：Linux 上没有桌面端。**

不是因为懒 —— Linux 桌面在中国只有 2.12%（Statcounter 2026-07），
而愿意装 Linux 的基本都是搞技术的。他们要的是一条命令能跑，
不是一个界面。所以这边给的是三个脚本。

### 你需要

```
Python 3.10+      大多数发行版自带
pip               装依赖才用，跑起来不用
```

没有的话：

```bash
# Ubuntu / Debian
sudo apt install python3 python3-pip

# Fedora
sudo dnf install python3 python3-pip

# Arch
sudo pacman -S python python-pip
```

### 三步

```bash
git clone https://github.com/sxm991104/remote-dispatch.git
cd remote-dispatch

./setup.sh          # 一、检查环境、建目录、生成配置模板
./start.sh          # 二、起服务（Ctrl+C 停，或者用 --daemon 后台跑）
./status.sh         # 三、看它在不在跑、任务在干什么
```

**`setup.sh` 会告诉你还差什么，不会替你做主**（不改系统包管理器、不改 shell 配置）。

### 填模型

```bash
$EDITOR ~/.config/dsh-bridge/engine.json
```

**至少要改 `api_key` 那一行。**

```json
{
  "profiles": [
    {
      "id": "p1",
      "name": "我的模型",
      "base_url": "https://api.deepseek.com",
      "api_key": "把 key 填在这里",
      "model": "deepseek-chat",
      "enabled": true,
      "concurrency": 1
    }
  ]
}
```

**`base_url` 填任何 OpenAI 兼容的接口都行**：DeepSeek / OpenAI / 通义 /
Kimi / 本地 Ollama。见 [任务端](../README.md)。

### 配置放哪

**Linux 遵循 XDG 规范，不是 `~/dsh-bridge/`。**

```
~/.config/dsh-bridge/          ← 默认（$XDG_CONFIG_HOME 没设时）
$XDG_CONFIG_HOME/dsh-bridge/   ← 设了就跟着走
```

**里面这些文件：**

```
api_token.txt     接口的访问令牌（第一次启动自动生成）
engine.json       模型配置
guard.json        危险操作验证码的邮箱配置（可选）
task_perms.json   各窗口的权限（默认工作区）
```

**想换位置就设 `XDG_CONFIG_HOME`，整个目录跟着走。**

### 接聊天软件

**这一段在 [atrium](https://github.com/sxm991104/atrium) 里，不在这个仓库。**

```bash
cd atrium
python -m atrium.cli init      # 生成 ~/.atrium/config.json
python -m atrium.cli test      # 测凭据
python -m atrium.cli run       # 起来
```

**atrium 要能调到这个仓库**，配置里指一下路径：

```json
{
  "engine": {
    "type": "remote-dispatch",
    "path": "/path/to/remote-dispatch"
  }
}
```

**接哪个平台见上面 [atrium](https://github.com/sxm991104/atrium) ——
飞书、钉钉这些官方接口在 Linux 上都能用。**

**这个仓库本身不需要装任何聊天框架。** 它只认命令行：

```bash
python core/bridge.py dispatch "帮我把 X 改了" --id 任务名
python core/bridge.py run --id 任务名        # 后台跑（atrium 会自己起）
python core/bridge.py stats --id 任务名      # 查进度
python core/bridge.py list                   # 列任务
```

### 起在后台

```bash
./start.sh --daemon      # 后台跑，日志写 bridge.log
./status.sh              # 看状态
./start.sh --stop        # 停
```

**端口默认 8790，只接受本机连接。** 要换端口设 `DSH_BRIDGE_PORT`。

### 开机自启

**仓库没带这个** —— 不同发行版的做法不一样，装一个 systemd unit 或者
往 `~/.config/autostart/` 放个 `.desktop` 都行，看你的情况。

**一个最小的 systemd user unit 长这样：**

```ini
# ~/.config/systemd/user/remote-dispatch.service
[Unit]
Description=remote-dispatch bridge
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/remote-dispatch
ExecStart=%h/remote-dispatch/start.sh
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now remote-dispatch
```

### 验证一下它真的能跑

```bash
./start.sh --daemon
./status.sh
#  进程      跑着（pid xxx）
#  端口      8790
#  engine.json   有
```

**然后派一条不靠模型的活试试：**

```bash
TOKEN=$(cat ~/.config/dsh-bridge/api_token.txt)
curl -s -X POST http://127.0.0.1:8790/api/dispatch   -H "X-Token: $TOKEN" -H "Content-Type: application/json"   -d '{"task":"在 /tmp 建一个 hello.txt，写三行字","name":"第一条"}'
```

**或者跑一遍测试，确认这台的 Python 环境没问题：**

```bash
python3 -m pip install pytest
python3 tests/run_all.py
```

---
