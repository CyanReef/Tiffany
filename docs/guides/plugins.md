# 本地源码插件

将标准 Python 插件项目放入 `<home>/plugins/` 的一级子目录，使用原来的 `start.bat`、`start.ps1` 或 `start.sh` 启动。有效插件默认启用；启动器在同一个托管环境安装当前 Tiffany 和启用插件，再启动应用。插件依赖、导入、配置或初始化失败会停止启动，不会静默跳过。

## 快速使用

复制仓库的 `examples/plugins/echo/` 到 `data/plugins/echo/`，运行 `start.bat` 或 `bash start.sh`。首次准备需要访问包索引；发送 `/echo hello` 验证回复。配置、平台和凭据仍使用现有部署向导。

```powershell
.\start.bat plugins list
.\start.bat plugins sync
.\start.bat plugins check
.\start.bat plugins disable echo
.\start.bat plugins enable echo
.\start.bat plugins repair
```

Linux 将 `start.bat` 换成 `bash start.sh`。全部命令支持 `--home`，从其他工作目录启动也使用同一份 home。管理命令无需平台账号；`check` 使用已有环境校验配置、入口和 Service 注册，不调用 setup/start，也不连接平台。`check-config` 仍只校验原有应用配置和凭据。

`sync`、`repair` 和启停修改要求应用已停止。`list` 显示保存的启用状态与环境准备状态，不表示插件正在运行。普通启动和读取命令不创建或改写插件配置。

## 声明与开发

在插件的 `pyproject.toml` 添加一个 entry point 和 API 声明：

```toml
[project.entry-points."tiffany.plugins"]
repeater = "tiffany_plugin_repeater:install"

[tool.tiffany.plugin]
api = 1
requires = []
```

标准 `[project]` 需提供静态 name、version 和 dependencies，宿主版本要求在 dependencies 中声明，例如 `tiffany-hook-runtime>=0.1,<0.2`。构建后端必须支持 editable 安装；模板使用 setuptools。一个项目首版声明一个插件；ID 使用小写字母开头，后接小写字母、数字、下划线或连字符，plugins 与 tiffany 为配置文件保留。重复 ID、重复包名或与宿主依赖及包管理工具同名的插件报错。

`requires` 是其他外部插件 ID，用于确定注册顺序；它不代替 Python 包依赖。缺失、禁用依赖及依赖循环在启动前失败。原有内置扩展继续通过 `[extensions]` 配置并先于外部插件安装。

入口必须实现同步 `install(scope, config) -> None`。`config` 是 `<home>/config/<ID>.toml` 解析的字典；不存在时传空字典。插件负责验证选项和默认值。源码目录名称不决定插件 ID，修改 ID 会改变默认配置文件和 Scope 名称。

安装函数通过 Scope 注册 Hook、Provider、Service 或 lifespan，不执行网络、磁盘初始化；异步初始化放进 Service/lifespan。资源属于 `plugins.<ID>`，后台任务通过 Runtime 的任务注册表并使用对应 owner。注册失败会逆序撤销新插件；初始化失败沿用运行时回滚。Provider 保持同步按需派生字段。

独立源码仓库可以在 `<home>/config/plugins.toml` 中登记，不必复制：

```toml
paths = ["D:/Projects/Python/my_plugin"]
disabled = []
```

相对路径按 home 解析；相同真实目录去重。目录中的普通非插件项目会被忽略；显式登记的路径缺少有效声明会报错。禁用项不会导入入口或安装其依赖，但静态声明仍必须有效。

同步使用 editable 安装，支持 flat/src 布局和包内资源。普通 Python 源码改动重启即可，声明、依赖、源码路径或启用集合变化会触发同步；扩展模块仍需遵循构建后端要求。多个插件需导入同一个公共 Field/ServiceKey 实例，名称相同不代表同一个键。

现有复读机后续需在其 pyproject 中补充上面的声明；它已有匹配的安装函数，默认配置路径仍是 `config/repeater.toml`。本功能不自动修改或迁移复读机仓库，也不再要求第三方插件自行维护应用启动器。

## 环境和失败恢复

独立安装使用 `tiffany run` 和 `tiffany plugins ...`，无需宿主源码。源码部署将 Tiffany 作为 editable 安装；wheel 部署从校验后的已安装内容生成宿主 wheel。两种模式中的第三方源码插件仍使用 editable，具备同样的声明与管理命令。每个 home 的环境独立，插件同步不修改 uv 工具环境。

Python、平台、基础锁或宿主代码内容变化使用不同环境目录；插件变化在当前共享环境中增量同步。先统一解析所有启用插件，并约束基础依赖和宿主版本，再安装、执行 `pip check` 和校验插件 editable 来源、entry point 和宿主内容身份。多插件不兼容的依赖不能在此共享环境中同时满足，需要修改约束或禁用冲突插件。

环境就绪记录保存在环境内的 `.tiffany-plugins.json`，记录声明指纹、解释器和实际版本。正常重复启动不运行 pip、不访问包索引；应用启动也校验记录，监督器不会在声明已变化时使用旧准备结果。直接运行 `main.py` 不准备依赖，不一致时需使用正常启动入口。

解析失败保留原就绪记录；实际安装前将环境标记为 dirty。安装失败或取消后应用不会启动，下一次普通启动会要求执行 `plugins sync` 重试或 `plugins repair` 重建。诊断位于 `<home>/logs/plugins-install.log`。原地安装不承诺自动回滚；repair 只重建当前托管环境，保留源码、配置、凭据和存储。

禁用或移除插件后的下一次同步会卸载该插件自身的发行包，保留可能共享的第三方库。重新启用会重新安装插件。同步与管理修改使用实例锁，不能绕开锁修改正在运行的共享环境。

## 更新与迁移

插件源码和配置都位于运行目录，Tiffany 发布包不覆盖它们。升级前停止服务，按部署方式更新工具或覆盖源码，再使用相同 home 启动；宿主要求不满足的插件会报告冲突。备份应包含 `plugins/`、`config/`、`secrets/` 和 `storage/`，不要把就绪记录当作可移植锁文件。

迁移操作系统或 Python 环境时复制源码、配置、凭据和业务数据，重新准备环境，不复制 `envs/`、`cache/` 或 `run/`。外部路径插件还需复制对应项目并调整 `plugins.toml`。首版配置修改后重启生效，不提供运行时热重载或插件市场。
