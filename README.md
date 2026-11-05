# remote-dispatch

**v1.3.5**

> 这个仓库是派活台的**「干活那一半」** —— 在电脑上真跑命令、真改文件。
>
> 另一半（接飞书/钉钉、当那个跟你说话的前台）在
> **[atrium](https://github.com/sxm991104/atrium)**。

---

## 它解决什么问题

**你在外面，想让电脑上干点活。**

手机上只能用聊天软件。而聊天窗口里的 AI 没有终端、没有文件系统 ——
它能跟你聊，但碰不到你的电脑。

```
你在手机上说的话   →   电脑上真去做   →   文档、表格、图片发回来
```

**这个仓库管后半截：怎么把活干完、怎么把成果交回来。**

它不做的事：

- 不接聊天平台（那是 [atrium](https://github.com/sxm991104/atrium) 的事）
- 不当客服聊天（那是 atrium 里那个前台的事）

**两个仓库松耦合，都能单独用。**

---

## 一条命令装好

```bash
npm i -g yatou-paihuo
paihuo install
```

**把两个仓库一起装上，Python 依赖也一并弄好。**
装完双击 `启动派活台.bat` 就能用。

> **只想装这一个？** 见下面「自己跑」。

需要 Node 16+ 和 **Python 3.10 ~ 3.13**（3.14 还不行，PySide6 没有它的包）。

---

## 它给你什么保护

**这是真的在跑命令、真的在改文件。** 不是沙盒演示。

所以它做了六层：

**动作守卫** —— 删库、读密钥、上传数据这类**不可逆**的操作，先拦下来。
判据不是"你想不想"，是"这一步做完能不能还原"。

**影子执行** —— 命令先在沙箱里跑，改动不落地。跑完你看它改了什么，
点头才写回真机。（需要 [Sandboxie-Plus](https://sandboxie-plus.com)）

**验证码** —— 提权、放宽权限、危险操作，发码到你邮箱。
不是问你"确定吗"，是**另开一条你手机能收到的路**。

**安全审核员** —— 另开一个模型，专门看"这次改动跟要求对不对"。
它不能聊天、没有记忆、不读项目 —— 骗不了它。

**版本回退** —— 靠 Windows 系统保护（卷影副本）。出事了能退回去。

**权限档** —— 每个任务窗口能碰多大地方，单独设。

**三种模式，按你机器上真具备什么来选：**

| 模式 | 拦住 | 验证码 | 审核员 | 回退 |
|---|---|---|---|---|
| 严格防护 | ✓ | ✓ | ✓ | |
| 出事可回退 | | | | ✓ |
| 全开 | | | | ✓ |

**没配邮箱就选不了"严格防护"，没开系统保护就选不了"出事可回退"。**
不让你点一个看着有防护、其实在裸奔的选项。

---

## 它到底能干什么

**你说一句话，它在电脑上做完**

```
「把项目里的 API 地址改成新的，然后跑一遍测试」
「把这一周的日报整理成一份 Word」
「这十几个图片压一下，放到桌面」
```

**它用模型思考，用工具动手**：读文件、写文件、改文件、列目录、搜内容、跑命令。

**做完了带成果回来** —— 改动的文件、生成的文档、以及一张 diff。

---

## 三种跑法

**一、桌面端点（最省事）**

双击 `启动派活台.bat`，在「任务」页写一句话点派活。不接聊天软件也能用。

**二、从聊天软件派（远程办公）**

装 [atrium](https://github.com/sxm991104/atrium)，在飞书或钉钉里说话。
一键装会把 atrium 一起装好。

**三、命令行派**

```bash
python core/bridge.py dispatch "在 ~/demo 下建个 README.md，写三行简介"
python core/bridge.py stats <任务名>
```

---

## 自己跑（开发者）

```bash
git clone https://github.com/sxm991104/remote-dispatch.git
cd remote-dispatch
python -m pip install -r requirements-dev.txt
python desktop/app.py
```

**Linux 上没有图形界面**，用命令行派活 —— 见 [docs/LINUX.md](docs/LINUX.md)。

---

## 想读深一点

**这份 README 只讲"是什么、能干什么、怎么装"。**
想读实现细节的，这几份分开放着：

| 想看什么 | 去哪 |
|---|---|
| 为什么这么设计（异步、心跳、交差规则） | [docs/DESIGN.md](docs/DESIGN.md) |
| 那几层安全怎么实现的 | [docs/SECURITY.md](docs/SECURITY.md) |
| 怎么在 Linux 上跑 | [docs/LINUX.md](docs/LINUX.md) |
| 出问题时 watchdog 怎么兜 | [docs/WATCHDOG.md](docs/WATCHDOG.md) |
| 以前用过 AstrBot 那版，怎么迁 | [HISTORY.md](HISTORY.md) |
| 改代码前踩过的坑 | [PITFALLS.md](PITFALLS.md) |
| 怎么跑测试 | [TESTS.md](TESTS.md) |
| 接下来往哪走 | [ROADMAP.md](ROADMAP.md) |
| 任务怎么写 | [examples/task-templates.md](examples/task-templates.md) |

---

## 版本

当前 **v1.3.5**。改动见 [CHANGELOG.md](CHANGELOG.md)。

---

## 许可

MIT —— 见 [LICENSE](LICENSE)。
