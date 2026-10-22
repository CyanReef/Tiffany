# 配置化业务扩展

业务组合发生在应用启动时，核心仍只处理 Envelope、Hook、同步 Provider、Service 和 Scope。配置修改后重启生效；普通启动不会改写配置。没有 `[extensions]` 时保持原来的裸文本 `ping` 和命令 Provider。

## 启用功能

在现有 OneBot 或 QQ 配置末尾添加：

```toml
[extensions]
enabled = ["commands", "ping", "help", "permissions", "ratelimit", "notes"]

[extensions.commands]
prefix = "/"

[extensions.permissions]
# 身份按 Adapter、消息场景和原生用户 ID 隔离。下面是 OneBot 示例。
# QQ 的 adapter_id 通常是 qqofficial_websocket:<AppID>，user_id 必须是 OpenID 字符串。
admins = [{ adapter_id = "onebot_websocket:napcat", scene = "group", user_id = 123456 }]

[extensions.ratelimit]
rate = 1.0
burst = 5
max_buckets = 4096
idle_ttl = 600.0

[extensions.notes]
max_entries = 10000
max_bytes = 4096
max_inflight = 64
```

仅需要命令和健康检查时使用 `enabled = ["commands", "ping", "help"]`。空列表显式停用所有业务；未选中的业务模块不导入、不注册服务或创建数据库。其余五项都依赖 `commands`，必须在列表中显式选择。ID 未知、重复、缺少依赖、依赖循环、参数错误或命令重名会在接入平台前失败。

没有平台账号时，可在项目根目录运行 `python -B -m examples.extensions`，用临时目录体验 ping、help 和笔记保存/读取/删除。示例退出后清理演示数据；源码见 [extensions.py](../../examples/extensions.py)。

| 输入 | 行为 |
| --- | --- |
| `ping` / `/ping` | 回复 `pong`；保留大小写和首尾空白兼容，不消耗业务限流额度 |
| `/help` | 列出当前注册的命令 |
| `/status` | 配置中的管理员读取 Runtime 状态、排队数和活动数；不显示凭据或消息 |
| `/note set hello world` | 保存当前用户在当前会话中的笔记，提交成功后回复“保存成功” |
| `/note get` | 读取笔记，缺失时回复“暂无笔记” |
| `/note delete` | 删除笔记 |

参数沿用空白分隔语义，不支持 shell 引号；笔记中的连续空白合并成单个空格。未命中命令不解析身份、不访问限流或存储；未知命令、权限拒绝和限流拒绝静默结束。已匹配业务拒绝来自机器人自身的消息。

管理员身份保留整数和字符串类型，不将 QQ OpenID 转为 QQ 号。业务限流按 Adapter、消息场景及原生用户身份计费；桶满时拒绝新身份，每次最多清理八个过期桶。TTL 必须大于等于令牌桶完整补满所需的时间。

## 数据与停止

笔记写入 `data/storage/notes.sqlite3`，键包含 Adapter、场景、用户类型、用户 ID 和稳定会话 ID。不同应用、账号、群聊、私聊不会复用同一笔记；用户或会话身份缺失时拒绝操作。

SQLite 使用 WAL 与 `synchronous=FULL`，所有数据库操作在服务自有的单线程执行器运行。容量同时覆盖等待与执行中的操作；满容量立即失败，调用方取消不释放正在执行操作的额度。停止先结算事务再关闭连接。保存失败明确回复失败，取消或连接丢失可能导致数据已提交但未成功发送确认，不承诺消息与数据库的分布式事务。

4096 个限流桶、10000 条笔记、单条 4096 字节及 64 个在途存储操作是首版硬上限；配置可以降低这些上限，超过上限会在连接平台前报错。

数据库格式从版本 1 开始。未知版本、损坏或不匹配的结构在启动时失败，保留文件供诊断，不自动重建。容量约束不删除历史数据；降低记录上限后仍可读取、更新或删除已有记录。

## 开发本地业务

`hooks.extensions` 提供 `ExtensionSpec`、`install_extensions(bot, config)` 与安装结果中的只读 Scope 视图。注册表使用显式模块路径，不扫描目录。首版增加 ID 时需同步 `shared.extensions.REQUIRES` 与配置选项；配置校验只依赖标准库。

每个模块实现同步的 `install(scope, config)`，注册资源后返回命令贡献的 ServiceKey。贡献中的 `CommandSpec` 声明名称、处理函数、Field、Service、管理员要求和健康命令属性；处理函数签名为 `async handler(ctx, command)`。数据库或网络 I/O 通过 Service 执行，不放进 Provider 或安装函数。

命令贡献与服务各自属于模块的 Scope，命令总入口属于 `business.commands`。总入口声明所有贡献和资源依赖，启动时编译不可变查找表，每条消息只进入一个命令 Hook。模块数量增加不会引入逐消息的命令列表扫描。

首版通过配置重启启停扩展。运行中卸载贡献会被总入口的依赖保护阻止；若手动清理，应先卸载 `business.commands`，再卸载贡献模块。安装失败先撤销总入口，再逆序清理模块；清理失败会携带残留 Scope 报告，不将失败当作成功卸载。
