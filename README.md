# SCNet Desktop Widget / 超算桌面挂件

[English](README.en.md)

在 Windows 桌面查看 SSH/Slurm 超算作业。挂件独立运行，无需保持 Codex 开启；超算端不需要安装 Codex、MCP 或 Python 软件包。读取部分沿用 [mcp-scnet-hpc](https://github.com/OVER4041/mcp-scnet-hpc) 的 SSH 配置与路径检查，独立包只保留查询能力。

## 显示内容

- 当前运行、排队和活跃作业数量；按超算日期统计今日完成与异常数量。
- 关注作业状态、已运行或等待时间、申请核数、申请内存与节点。
- 可选 EPW q 点与迭代阶段进度，以及现有资源账本累计用时和核时。
- 默认紧凑卡片，可展开列表；支持拖动、置顶、手动同步及 30/60/120/300 秒刷新间隔。
- 前台应用全屏时自动暂停置顶并退到后面，退出全屏后恢复原来的置顶偏好；普通最大化不触发。每秒检测一次，支持无边框全屏和多显示器坐标。
- 已知作业转为终态时显示本机提醒和提示音。断网保留旧数据、冻结推算计时并标记未同步。

## 启动

1. 本机安装 Python 3.11+（含 Tkinter）和 OpenSSH。标准 Windows Python 通常带 Tkinter，运行挂件不依赖第三方 Python 包。
2. 先用本机 SSH 配置完成超算登录和主机指纹确认。密钥有口令时，先通过本机 ssh-agent 解锁。
3. 将 `config.example.toml` 复制为 `config.local.toml`，把 `ssh_host = "your-hpc"` 改成自己的 SSH 别名。
4. 将 `widget.example.json` 复制为 `widget.local.json`。默认每 60 秒同步本人作业。
5. 双击 `启动超算挂件.vbs`。也可以使用 `pythonw launch-widget.pyw`；不安装任何开机自启动。

安装源码包后可运行 `scnet-desktop-widget --widget-config widget.local.json`。加 `--once` 可打印单次真实状态而不打开窗口。

## 可选进度与累计时间

默认自动跟踪运行中的作业，无需填写作业号或日志路径。挂件读取 Slurm 的工作目录、标准输出及提交脚本，识别简单的输出重定向；也会在作业目录及其 `logs` 子目录中查找文件名包含当前作业号的日志，因此能发现由 Python 驱动程序创建的 EPW 输出。每次最多读取 6 个日志的末尾 160 行且每个不超过 8 KiB，优先显示最近更新的 EPW 进度。只检查配置的 `remote_root` 范围，不执行脚本，也不遍历整个主目录。

`widget.local.json` 的 `watched_jobs` 可保留需要查询的历史作业；`job_logs` 是手动路径覆盖，例如 `{"123456": "calculations/epw.out"}`，适用于日志没有作业号或位于其他目录的情况。自动关注的作业结束后会转向活跃作业；手动点击列表中的作业后，本次运行保持该选择。日志尚未创建、没有可识别进度或暂不可读时会显示相应状态，不影响作业列表刷新。日志反映当前计算阶段，不代表整体完成百分比或科学收敛。

`project_ledger` 可指向已有资源账本 JSON，内容为 `{"jobs": [{"id": "123456", "mpi_tasks": 32, "actual_elapsed_s": 600}]}`。尚未完成的条目将 `actual_elapsed_s` 设为 null；挂件优先使用实时记账。累计时间包括所选账本中的失败与续算，核时按核数乘以作业用时累计。

## 状态含义

作业离开队列后等待 Slurm 记账确认，不自动判成功；`.batch`、`.extern` 不重复计数，数组按任务计数。今日异常不包含用户取消。调度完成与科学结果收敛分开判断。EPW 进度只对应当前阶段，不是整体剩余时间预测；申请内存不等于实时内存占用。

保持本机唤醒、联网，挂件才能轮询。退出挂件不会影响超算作业。挂件本身不唤醒 AI；自动后处理可另配聊天的定时监视。

个人配置、密钥、缓存、界面位置和运行记录不进入公开仓库。SSH 复用既有密钥与主机校验，不自动接受未知主机，也不提供提交或取消作业按钮。

## 开发与验证

在 PowerShell 设置 `$env:PYTHONPATH = "$PWD/src"`，然后运行 `python -m unittest discover -s tests -v`。界面使用 Python 标准库 Tkinter。可选视觉验证工具 `scripts/verify_widget.py` 还需要 Pillow、个人配置和 `.state/widget-live-verification.json` 的本机测试快照；这些文件不会随源码发布。

首次实现已验证真实 Slurm 与 EPW 数据、高清屏、折叠、断网保留数据和冻结计时；通用解析与计账测试可离线运行。见 [更新日志](CHANGELOG.md)。

全屏避让的本机实测可运行 `python scripts/verify_fullscreen.py`，需要交互式 Windows 桌面。脚本会短暂打开独立测试窗口，验证全屏、无边框、最大化、最小化以及手动关闭置顶后的行为，并自动关闭测试窗口；不连接超算。检测通过 Windows 窗口与显示器 API 比较前台窗口客户区和完整显示器区域，参见 [GetClientRect](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getclientrect) 与 [GetMonitorInfoW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getmonitorinfow)。
