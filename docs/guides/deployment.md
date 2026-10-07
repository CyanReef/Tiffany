# 服务器部署与覆盖更新

首版面向 Linux、Python 3.11～3.14。服务器需要预装 Python、venv 和 screen；第一次准备依赖需要访问 PyPI，已有环境可离线复用。应用连接 QQ 时仍需访问平台。

## 启动

在项目中执行：

```bash
bash start.sh
```

从任意工作目录启动：

```bash
screen -S tiffany bash /opt/Tiffany/start.sh
```

第一次在终端选择 OneBot/NapCat 或 QQ 官 Bot，之后自动读取同一份配置。选择 QQ 可在控制台扫码，也可手动输入 AppID、AppSecret。配置向导被取消或失败时不提交新凭据。非交互启动缺少配置会退出，先在交互终端运行 `configure`。

```bash
bash /opt/Tiffany/start.sh configure
bash /opt/Tiffany/start.sh check-config
bash /opt/Tiffany/start.sh login
bash /opt/Tiffany/start.sh configure --import-config /path/to/old/Tiffany.toml
```

`login` 仅用于已有 QQ 配置；重新扫码可能得到另一个 AppID，成功后配置随之切换。环境变量指定的密钥仍有最高优先级，使用本地新凭据时应撤销旧环境变量。导入保留原 TOML 内容，不复制环境变量中的密钥；导入后执行 `check-config`，缺少 QQ 密钥时运行 `login`。

指定 Python：`TIFFANY_PYTHON=/usr/bin/python3.12 bash start.sh`。指定运行目录：`bash start.sh --home /srv/tiffany/data`。优先级是 `--home`、`TIFFANY_HOME`、启动脚本所在项目的 `data/`；修改 home 后需要使用对应配置和凭据。

直接运行 `python main.py` 会读取 data 中的配置并运行应用，但不准备环境或自动重启。

## Windows 本机测试

安装 Python 3.11+，在项目根目录打开 PowerShell 或 CMD。一行启动，第一次进入同一套配置向导：

```powershell
.\start.bat
```

`start.bat` 调用 Windows 自带的 PowerShell，再运行共用启动器；执行策略放行仅作用于该次 PowerShell 进程。也可直接使用 `.\start.ps1`，适合已经允许执行本地脚本的 PowerShell。首次联网准备依赖，后续复用环境。

启动器、配置命令和应用使用 UTF-8 输出，并为子进程设置 UTF-8 环境；直接执行 `python launcher.py` 或 `python -m deployment.configure` 也能在旧 Windows 编码环境中输出中文结果。诊断中的路径可能显示为完整路径，例如 `RUNNER~1` 展开为 `runneradmin`，它们可以指向同一个文件。

```powershell
.\start.bat configure
.\start.bat check-config
.\start.bat login
.\start.bat configure --import-config "D:\Old Tiffany\Tiffany.toml"
.\start.bat --home "D:\Tiffany Data"
```

脚本按自身位置定位项目，可从其他工作目录启动：

```powershell
& "D:\Projects\Python\Tiffany\start.bat"
```

优先检查当前 PATH 中的 `python`，再尝试 `py -3`、`python3`；每个命令会检查 PATH 中的候选文件，以实际执行结果验证 Python 3.11+ 和 venv。支持可正常执行的 Windows 应用别名与命令包装脚本，不按 WindowsApps 目录直接排除。检测失败时显示候选路径和具体错误。也可指定单个 Python 可执行文件路径：

```powershell
# PowerShell
$env:TIFFANY_PYTHON = "D:\SDK\Python\MiniConda3\python.exe"
.\start.bat
```

```bat
rem CMD
set "TIFFANY_PYTHON=D:\SDK\Python\MiniConda3\python.exe"
start.bat
```

测试 OneBot 时将 NapCat 反向 WebSocket 地址设置为 `ws://127.0.0.1:6199`，Token 与向导填写的一致；发送 `ping` 验证回复。健康状态可用 `curl.exe http://127.0.0.1:9464/readyz` 检查。QQ 在向导中选择扫码或手动凭据，后续启动复用。停止时在启动终端按 Ctrl+C，并等待应用与启动器退出，再覆盖更新。

## 从 Windows 迁移到 Linux

先停止 Windows 实例，再将程序文件和 `data/config/`、`data/secrets/`、`data/storage/` 复制到 Linux 项目内的同名位置；要保留历史日志和备份，再复制 `data/logs/`、`data/backups/`。在 Linux 安装 Python、venv、screen 后执行 `bash start.sh`，已有配置和本地凭据直接复用。

`data/envs/`、`data/cache/`、`data/run/` 可留在 Windows，它们在 Linux 重新生成。环境目录按操作系统、架构、Python 版本和锁文件哈希隔离，避免复用另一平台的环境。使用环境变量提供凭据时，需在 Linux 重新设置对应变量；配置中的监听地址也应按服务器实际环境检查。Linux 启动时会设置凭据及备份的文件权限。生产环境的信号、平台收发与持续运行仍按实机验收清单验证。

## 保留的数据

```text
data/
  config/Tiffany.toml       # 不含密钥，普通启动不重写
  secrets/credentials.json # 按 AppID/平台索引的本地凭据
  logs/tiffany.log          # 应用日志，追加并轮转
  logs/launcher.log         # 环境准备与监督日志
  storage/                 # 业务持久化数据
  cache/                   # 可重建缓存、pip 缓存
  backups/                 # 每个主动修改文件最近 10 份备份
  envs/                    # 按操作系统、架构、Python 版本和锁文件哈希隔离
  run/                     # launcher.lock / application.lock
```

Linux secrets 和 backups 目录为 0700；凭据及其备份为 0600。备份可能包含旧密钥，备份整个 data 时需要保留权限。配置损坏会报错，恢复备份或修复后再启动；配置向导也不会自动覆盖损坏文件。每个文件的替换是原子的，多文件提交在普通写入错误时回滚；凭据保留旧 AppID 条目，以便两个文件替换之间发生掉电时旧配置仍能查到原凭据。业务数据迁移由业务服务负责。

同一 home 只允许一个启动器和一个应用。锁由系统持有，崩溃后会自动释放；留下的锁文件不代表仍有进程，不要通过删除锁文件绕开正在运行的实例。

扩展从 `APP_PATHS` 获取路径，持久内容放 storage，可重建内容放 cache：

```python
from core import APP_PATHS

@bot.hook(name="example", uses=(APP_PATHS,))
async def example(ctx):
    paths = ctx.service(APP_PATHS)
    destination = paths.storage / "example.json"
    # 由业务自行选择数据库或原子文件写入。
```

## 平台接入与扫码

OneBot 默认监听 `127.0.0.1:6199`，NapCat 配置反向 WebSocket 地址 `ws://127.0.0.1:6199/ws`。Token 使用 `Authorization: Bearer <token>`；向导通过隐藏输入保存 Token，或用配置指定的 `TIFFANY_ONEBOT_TOKEN` 环境变量。非本机绑定必须有 Token。接入支持一个活动连接。

QQ 使用[腾讯官方 qqbot-agent-sdk](https://github.com/tencent-connect/qqbot-agent-sdk) 的[固定 1.2.2 版本](https://pypi.org/project/qqbot-agent-sdk/1.2.2/)进行绑定。每个二维码最多等待 300 秒，过期最多自动刷新 3 次。二维码和备用 HTTPS 链接只在终端显示，应用日志不记录它们；成功返回 AppID、密钥、扫码者 OpenID 后保存。后续运行直接使用保存的密钥，访问令牌由现有 Client 获取和刷新。

SDK 只用于绑定，群与 C2C 文本收发继续使用 Tiffany 的 Client/Adapter。账号权限、绑定接口可用性及 `/gateway` 能力需要真实账号验证。永久鉴权或权限错误退出码为 3，保留文件并提示重新登录或检查权限。

## 停止、恢复和日志

进入 screen 后按 Ctrl+C，第一次请求 drain，第二次请求 abort。SIGTERM 使用相同逻辑。等待关闭结果和命令退出，再覆盖文件。超过默认 45 秒未退出，启动器会终止应用进程组。用户主动停止后不会自动重启。

退出码：0 正常、2 配置/环境错误、3 永久鉴权/权限错误、4 可重试故障、5 清理失败。4、5 和未处理异常触发重启；10 分钟内最多 5 次，延迟 2、5、15、30、60 秒，稳定运行 10 分钟后清空计数。暂时断线由 Adapter 重连，应用重启不重新安装依赖。取消后仍有任务不结束时，应用会刷新日志并直接结束子进程，避免卡在 asyncio 的退出阶段；启动器负责恢复，用户主动停止时不会重启。

应用和启动器各自输出到控制台及日志文件。默认 INFO，每个文件最多 10 MiB，保留 10 个轮转备份。已知密钥与 Authorization Token 脱敏，Hook 错误包含 Hook 名、事件关联标识和原因。普通 Hook 错误结束当前事件，关键监督任务失败结束运行时并交给启动器恢复。

监控默认只绑定 `127.0.0.1:9464`：

```bash
curl http://127.0.0.1:9464/livez
curl http://127.0.0.1:9464/readyz
curl http://127.0.0.1:9464/metrics
```

就绪必须同时满足 Runtime RUNNING、Adapter 完成连接鉴权、Scheduler 可以接纳事件。OneBot 等待 NapCat 连接、QQ 等待 READY/RESUMED、断线或队列满时 `/readyz` 返回 503。可用时返回 200。应用监听异常会退出；`allow_remote=true` 才能将监控绑定到非本机地址。

## 发布、更新与回滚

生成用于覆盖更新的程序包：

```bash
python -B tools/build_release.py
```

发布工具按明确的程序文件列表打包，并验证 ZIP 不含根目录 data。旧配置参考保存在 `examples/legacy/Tiffany.toml`，普通启动仍只读取运行目录下的配置；更新包只带根目录的单份英文 `LICENSE`。sdist 使用 MANIFEST.in 排除 data，wheel 使用明确的包列表。普通源码归档也应检查不含 data，勿手工将运行目录加入更新包。

服务器更新：

1. 在 screen 中 Ctrl+C，等待清理结束和进程退出。
2. 保留上一份程序包以及 data 备份。
3. 用 ZIP 中 `Tiffany/` 下的文件覆盖现有项目文件，保留原项目 `data/`。
4. 执行 `bash start.sh`。锁文件变化时自动准备新环境，旧环境保留。

若安装路径正好为 `/opt/Tiffany`，可在停止后使用：

```bash
unzip -o /tmp/Tiffany-server-0.1.0.zip -d /opt
screen -S tiffany bash /opt/Tiffany/start.sh
```

回滚同样先停止，用上一版本包覆盖后再启动。锁文件恢复时会复用对应旧环境。**不要删除整个项目目录，不要使用会删除额外文件的镜像同步方式更新。** 该机制保证覆盖更新保留数据；业务服务若变更存储格式，需提供独立兼容迁移。

## 实机验收

自动测试覆盖配置和凭据保护、二维码模拟、环境复用和回滚、锁、核心关闭与卸载、真实 HTTP/OpenMetrics/WebSocket 鉴权及覆盖更新。Linux CI 配置 Python 3.11～3.14，执行真实子进程的信号转发、第二次停止、故障限次重启和停止截止时间测试。截止时间测试使用缩短预算验证同一机制，默认设置仍为 45 秒。

发布到实际服务器前还需记录以下实机结果：

- NapCat `ping`、临时断线及重连；drain 期间已接纳消息能完成回复。
- QQ 真实扫码、群 @消息和 C2C 私聊 `ping`、Token 更新、断线恢复。
- screen 内 Ctrl+C / SIGTERM、两次停止与 45 秒上限。
- 覆盖更新及旧包回滚后，配置、凭据、业务文件和历史日志保持。
- 至少 24 小时持续运行，观察内存、队列、错误、重连和日志轮转。

本地模拟和 CI 不能替代账号与 24 小时实机验收。事件队列和去重缓存仍在内存，重启重新连接，不保证跨进程补发或恰好一次处理。
