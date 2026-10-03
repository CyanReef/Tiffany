# 学习与继续开发路线

推荐按“看懂现有功能 → 跟踪一次事件 → 理解数据与资源 → 修改框架机制”的顺序学习。全量文件分类见 [源码地图](CODE_MAP.md)，测试位置和运行方式见 [测试导航](../tests/README.md)。

## 1. 按六个阶段阅读

| 阶段 | 学习目标 | 阅读顺序 | 对照测试 |
| --- | --- | --- | --- |
| 1. 应用如何组成 | 知道配置、适配器和业务在哪里装配 | [main](../main.py) → [application](../application.py) → [settings](../settings.py) → [业务注册](../hooks/__init__.py) | [适配器生命周期](../tests/integration/onebot/test_adapter_lifecycle.py) |
| 2. 一个 Hook 如何工作 | 理解路由、字段需求、回复和 stop | [ping](../hooks/ping.py) → [共享字段](../fields.py) → [Context](../core/Context.py) | [Ping](../tests/integration/hooks/test_ping.py) |
| 3. 字段为什么懒计算 | 理解 Field 身份、Provider 依赖和事件缓存 | [Field](../core/Field.py) → [协议字段](../adapters/onebot_fields.py) → `Context.resolve` → [Provider](../core/Provider.py) | [Context](../tests/unit/core/test_context.py)、[Provider 注册表](../tests/unit/core/test_provider_registry.py) |
| 4. 一次事件如何执行 | 理解快照、优先级、谓词、超时和取消 | `Bot.emit` → `Runtime.emit` → `Runtime._dispatch` → [Dispatcher](../core/Dispatcher.py) → [路由](../core/dispatch/routing.py) / [执行](../core/dispatch/execution.py)；注册变化看 [注册表](../core/dispatch/registry.py) 和 [声明/句柄](../core/dispatch/models.py) | [分发](../tests/integration/core/test_dispatcher.py)、[Hook 执行](../tests/integration/core/test_hook_execution.py) |
| 5. 扩展资源如何管理 | 理解 Service、Scope、owner 与启动/关闭顺序 | [Service](../core/Service.py) → [Scope](../core/Scope.py) → `Bot.unload` → [owner 清理](../core/runtime/owners.py)；[启动](../core/runtime/startup.py) / [关闭](../core/runtime/shutdown.py) 共用 [组件调用](../core/runtime/components.py) | [Service 注册表](../tests/unit/core/test_service_registry.py)、[Scope](../tests/integration/core/test_scope.py)、[生命周期](../tests/integration/core/test_lifecycle.py) |
| 6. 传输与并发如何配合 | 理解 echo、pending、过载接纳、会话队列和观测 | [Adapter](../adapters/OneBotWebSocketAdapter.py) → [Client](../clients/OneBotWebSocketClient.py) → [Scheduler](../core/EventScheduler.py) → [Trace](../core/Trace.py) | [客户端](../tests/unit/onebot/test_client.py)、[调度](../tests/integration/core/test_scheduler.py)、[追踪](../tests/integration/core/test_trace.py) |
| 7. 服务器如何启动 | 理解环境复用、配置保护、进程监督和健康检查 | 启动脚本 → [启动器](../deployment/launcher.py) → [环境准备](../deployment/environment.py) → [监督](../deployment/supervisor.py) → [应用](../application.py) | [环境](../tests/unit/deployment/test_environment.py)、[真实进程](../tests/integration/deployment/test_processes.py)、[Windows 入口](../tests/integration/deployment/test_windows_entrypoints.py) |
| 8. 扩展如何持久保存数据 | 理解 `APP_PATHS` 与 owner，区分关键故障和清理结果 | [路径服务键](../core/Paths.py) → [共享布局](../shared/paths.py) → `ctx.service(APP_PATHS)`；`Runtime.wait_closed/failure_cause` | [分层和路径服务](../tests/unit/core/test_layering.py)、[运行边界](../tests/integration/core/test_server_regressions.py) |

每个阶段先运行对应测试，再看测试里如何构造输入、等待结果和断言行为。读到与当前目标无关的底层实现时，可以沿着源码地图返回上层入口。

## 2. 先运行一个无外部服务的例子

在项目根目录、安装依赖后运行下面的 Python 代码。它展示已有的 Scope、Hook、Provider、字段缓存和 Trace，并使用人工构造的 Envelope。

```python
import asyncio

from core import Bot, Envelope, Field, ProviderContext


async def main():
    bot = Bot()
    scope = bot.scope("learning.demo")
    text_field = Field[str]("learning.text")
    provider_calls = 0
    observed = []

    def read_text(ctx: ProviderContext) -> str:
        nonlocal provider_calls
        provider_calls += 1
        return str(ctx.raw.get("text", ""))

    scope.provide(text_field, read_text)

    @scope.hook(name="first", on="message", priority=10, needs=(text_field,))
    async def first(ctx):
        observed.append(("first", ctx.resolve(text_field)))

    @scope.hook(name="second", on="message", needs=(text_field,))
    async def second(ctx):
        observed.append(("second", ctx.resolve(text_field)))

    bot.dispatcher.trace.enable()
    await bot.start()
    try:
        await bot.emit(Envelope("learning", {"text": "hello"}, kind="message"))
        await bot.emit(Envelope("learning", {"text": "ignored"}, kind="notice"))
        assert observed == [("first", "hello"), ("second", "hello")]
        assert provider_calls == 1
        print(observed, "provider calls:", provider_calls)
        print("trace:", [record.result for record in bot.dispatcher.trace.snapshot()])

        await scope.unload()
        await bot.emit(Envelope("learning", {"text": "after unload"}, kind="message"))
        assert provider_calls == 1
    finally:
        report = await bot.stop()
        assert report.successful


asyncio.run(main())
```

观察三个地方：优先级较大的 Hook 先运行；两个 Hook 读取同一个 Field 时，Provider 在这个事件中只计算一次；`notice` 路由和卸载后的事件不会执行这两个 Hook。

在自己的实验副本中，可以逐个尝试这些变化：

1. 在 `first` 末尾调用 `ctx.stop()`，观察 `second` 是否执行。
2. 给 `first` 加 `when=lambda ctx: False`，观察后续 Hook 和 Provider 次数。
3. 连续发两条 `message`，观察缓存是每个事件独立存在的。
4. 为某个 Hook 增加短 `timeout`，在 handler 中等待，结合 `on_timeout="continue"` 观察后续处理。

这些是学习现有机制的实验；实际项目改动仍放到对应功能模块和测试中。

## 3. 写一个功能时怎么组织

### 定义、注册、运行分开

功能文件放数据类型、Field/ServiceKey 和 `register(...)`。应用组合入口决定安装哪些功能。网络监听在组件 `start()` 中建立，释放在 `stop()`/`teardown()` 中完成。

现有例子：`hooks/ping.py` 直接读取 `TEXT` 匹配 `ping`；`hooks/command.py` 演示命令数据及派生字段 Provider，供其他命令功能使用；`hooks/__init__.py` 选择注册两者。新的可独立管理功能可以接收 `Scope`，由组合入口传入 `bot.scope("extension.<名称>")`。

### 按计算方式选能力

| 场景 | 做法 | 可以参考 |
| --- | --- | --- |
| 从原始消息取 ID、文本或命令 | 同步 Provider，声明依赖的 Field | `adapters/onebot_fields.py`、`hooks/command.py` |
| 请求外部 API 或访问数据库 | 在 Hook 中 await 一个 Service 的方法，Hook 声明 `uses` | Service 注册表和生命周期测试 |
| 让后续 Hook 使用已经算好的结果 | `ctx.put(field, value)`，后续通过同一个 Field 读取 | `Context.put/resolve` |
| 把一批注册和资源一起卸载 | 使用一个 Scope 和统一 owner | Scope 集成测试 |
| 建立长期运行的后台任务 | `bot.runtime.tasks.spawn(..., owner=scope.owner, name=...)`，按任务的重要性选择失败策略 | `TaskRegistry.spawn` 与 `Runtime._task_failed` |

新增 Field 或 ServiceKey 时，定义一次并让提供者和消费者导入同一个实例。重新创建一个同名键，会得到不同的身份。

`needs` 会在验证阶段检查 Provider 是否存在，即使计划先通过 `ctx.put` 写入值，也要满足这个声明的注册要求。`ctx.put` 写入后，同一事件里的 `resolve` 会直接返回缓存值。

### 修改前先确定行为和边界

开始制作一个功能时，写清楚输入事件、依赖字段/服务、处理结果和资源清理方式；然后按 [源码地图的修改落点](CODE_MAP.md#6-继续开发时改哪里) 找到模块和测试。

业务场景在 `tests/integration/hooks` 覆盖；纯字段转换或命令解析在 `tests/unit` 覆盖。测试应检查用户能观察到的结果，以及超时、取消、卸载等资源边界。

## 4. 必须理解的现有约定

| 约定 | 对继续开发的影响 |
| --- | --- |
| `Envelope.kind` 默认是 `event` | 人工构造消息并测试 `on="message"` 的 Hook 时，需要显式传入 `kind="message"` |
| Provider 是同步计算函数，声明 `needs` 不会主动解析 | 只调用 `ctx.resolve` 才产生解析成本；Provider 不返回 awaitable |
| `ctx.has(field)` 查询的是当前事件缓存 | 它表示该字段已经有值，不能据此判断注册表是否有 Provider |
| Field/ServiceKey 按实例身份识别 | 跨模块使用共享实例；字符串名称是诊断信息 |
| 同一服务实例只能属于一个 owner | 否则抛出 `ServiceOwnershipError`；共享时只注册一次，消费者使用同一个 ServiceKey 和 `uses`/`depends` |
| Service、Adapter 和 lifespan 在 NEW 阶段注册 | 在 `bot.start()` 或 `bot.setup()` 之前完成资源组合；声明 `uses` 的 Hook 注册时服务就应存在 |
| 每次接纳事件时捕获注册快照 | 已排队或执行中的事件继续使用旧视图；句柄变更影响后续接纳的事件 |
| Hook 的 `abort` 错误策略终止当前事件 | 它与 `bot.stop("abort")` 的整体运行时关闭是不同的调用边界 |
| `ctx.stop()` 影响后续 Hook | 想结束当前 handler，还需要自己的 `return` 或自然返回 |
| 成功卸载的 Scope 变为 inactive | 再取得同名 Scope 仍是原实例；不能用它继续注册资源 |
| Scope 卸载先检查外部消费者 | 其他 owner 依赖本 owner 的字段或服务时，会抛出 `OwnerInUseError` |
| `scope.unload(mode="abort")` 直接取消对应工作 | 取消相关 Scheduler 事件和 owner 任务；拒绝取消的工作会导致清理失败，资源保留以便稍后重试 |
| Scope drain 共用 30 秒工作等待预算 | 相关事件和后台任务不会各等 30 秒；超时后取消工作，组件释放另有有界预算 |
| Runtime 是一次执行的生命周期 | 已终止的 Runtime 不能直接重新 start；新运行创建新的 Bot |
| `wait_closed()` 等待真实清理完成 | 正常关闭和启动回滚都会通知；`failure_cause` 保存首个关键后台故障，`startup_failed` 单独表示启动事务失败 |
| 单并发与会话 FIFO 是不同约定 | 同一 `(adapter_id, session_id)` 内有序；`workers = 1` 仍会跨会话轮流调度，不保证连接全局 FIFO |
| 队列容量同时统计排队和执行中的事件 | 满载时拒绝或丢弃新事件；Adapter 提交不等待业务处理完成，以保持响应帧可读 |
| 任务需要明确归属 | 后台任务通过 TaskRegistry 注册；关闭报告只依据框架拥有的任务 |
| `TaskRegistry.spawn` 的 `critical` 默认是 True | 关键任务失败会触发运行时关闭；非关键任务显式设为 False。`failure_policy="disable_owner"` 会请求卸载任务所属 owner |

## 5. 用测试确认你读懂了代码

在项目根目录运行：

```powershell
# 验证完整项目
python -m unittest discover -s tests -t . -q

# 阅读一项行为的全部断言
python -m unittest tests.unit.core.test_context -v
python -m unittest tests.integration.core.test_scope -v
```

建议先从测试中的一个场景入手：画出输入和结果，找到对应方法，解释断言为什么成立，再修改自己的实验代码验证理解。进入 Scheduler、Runtime 或 Client 的并发逻辑时，要同时看取消和清理路径。

## 6. 用官 Bot 接入练习扩展现有架构

先读 [QQ 官 Bot 接入设计](QQOFFICIAL_DESIGN.md)，按字段 Provider → HTTP Client → WebSocket Adapter → 示例入口的顺序学习：

1. 对照两种平台的字段 Provider，理解相同 Field 为什么能由不同平台提供，OpenID 为什么必须保持字符串。
2. 跟踪同一个 `ping` Hook 在 OneBot 与官 Bot 上的回复路径；业务使用 `ctx.reply()`，协议动作由事件携带的 Client 决定。
3. 阅读官 Bot Adapter 的 `_handle` 和 `_admit`：控制帧留在接入层，业务帧保留原始数据，有界接纳成功后才推进恢复检查点。
4. 在本机网关测试中观察慢 Hook、心跳、同会话顺序、跨会话并发、断线恢复和 drain；这些测试不需要真实账号。

```powershell
python -B -m unittest tests.unit.qqofficial.test_fields -v
python -B -m unittest tests.integration.qqofficial.test_gateway -v
```

高级玩法从自定义 Field/Provider、平台限定的 Hook、Client 直接 API 调用和已有 Service/Scope 能力展开。网络工作在 Hook 或 Service 中 await，协议 Provider 继续保持同步懒计算。

## 7. 从终端启动到服务器运行

按以下顺序阅读，把每一步的输入、文件和退出结果对应起来。完整命令操作见 [部署文档](DEPLOYMENT.md)。

1. [start.sh](../start.sh) 或 [start.bat](../start.bat)/[start.ps1](../start.ps1) 定位脚本目录，找到 Python 3.11+，把参数转交 [launcher.py](../launcher.py)。当前工作目录不会决定默认数据位置。
2. [deployment/launcher.py](../deployment/launcher.py) 使用共享路径约定定位 home，优先级是 `--home`、`TIFFANY_HOME`、项目内 `data/`，创建目录并取得启动器锁。它仅依赖标准库，在依赖尚未安装时也能运行。
3. 缺少配置时由 [configure.py](../deployment/configure.py) 运行首次向导。QQ 扫码在 [onboarding.py](../deployment/onboarding.py) 调用官方 SDK；结果交给凭据存储。已有配置损坏时直接报错，普通启动不覆盖或重写它。
4. [environment.py](../deployment/environment.py) 根据 Python、平台和锁文件哈希选择 `data/envs/` 的环境。新环境安装、导入检查成功后才标记可用；未变化的环境复用，不访问包源。切换版本准备另一环境，保留旧环境供回滚。
5. [supervisor.py](../deployment/supervisor.py) 启动应用子进程，处理停止信号和限次重启；重启循环不会重复安装依赖。退出码 2/3 停止，运行或清理故障按策略重试，用户停止不重试。
6. [application.py](../application.py) 取得应用锁，读取配置和凭据，注册 `APP_PATHS`、健康导出服务、业务及 Adapter，再运行 Bot。第一次停止请求 drain，第二次升级 abort，启动器默认在 45 秒上限后终止子进程。

路径类型定义在 [shared/paths.py](../shared/paths.py)，目录选择与权限操作在 [shared/path_setup.py](../shared/path_setup.py)。[deployment/paths.py](../deployment/paths.py) 保留旧导入出口；[core/Paths.py](../core/Paths.py) 仅提供业务使用的服务键和同一个类型。导入核心不需要部署层，显式调用 `initialize()` 才创建运行目录。

配置和凭据的主动修改由 [storage.py](../deployment/storage.py) 先备份、再原子替换；[credentials.py](../deployment/credentials.py) 只按匹配的 AppID 提供密钥。[logging.py](../deployment/logging.py) 处理轮转与已知密钥脱敏。对照测试观察损坏文件、写入失败和取消扫码时旧数据为何仍能保留。

服务器默认启用 `127.0.0.1:9464` 的 `/metrics`、`/livez`、`/readyz`。就绪要求 Runtime 正常运行、Adapter 连接成功且可接纳事件；断线会降低就绪状态。单独创建 `Bot()` 不安装导出服务，Trace 默认关闭。健康状态来自应用回调，核心不认识 NapCat 或 QQ 登录方式。

更新练习使用临时安装目录：执行 [发布/回滚测试](../tests/integration/deployment/test_release.py)，确认包内没有 `data/`，覆盖程序与回滚后四类保护文件的字节一致。实际更新先停止并等待清理，再覆盖程序文件，保留 `data/`；不要删除整个项目或使用带额外文件删除的同步操作。

## 8. 用路径服务保存业务数据，正确管理归属

服务器组合入口已注册 `APP_PATHS`。业务声明 `uses=(APP_PATHS,)`，通过 `ctx.service(APP_PATHS).storage` 获取目录；缓存使用 `.cache`。不要拼接当前工作目录或把持久文件写进业务源码目录。下面的完整例子使用临时目录，不需要账号：

```python
import asyncio
import tempfile

from core import APP_PATHS, Bot, Envelope, RuntimePaths


async def main():
    with tempfile.TemporaryDirectory() as home:
        paths = RuntimePaths.resolve(home)
        paths.initialize()
        bot = Bot()
        bot.service(APP_PATHS, paths)  # 路径服务归 application owner
        feature = bot.scope("learning.storage")

        @feature.hook(on="message", uses=(APP_PATHS,))
        async def save(ctx):
            target = ctx.service(APP_PATHS).storage / "message.txt"
            await asyncio.to_thread(target.write_text, ctx.raw["text"], encoding="utf-8")

        async with bot:
            await bot.emit(Envelope("learning", {"text": "saved"}, kind="message"))
            assert (paths.storage / "message.txt").read_text(encoding="utf-8") == "saved"
            await feature.unload(mode="abort")
            assert bot.services.get(APP_PATHS) is paths

        report = await bot.runtime.wait_closed()
        assert report.successful
        assert bot.runtime.failure_cause is None
        assert not bot.runtime.startup_failed


asyncio.run(main())
```

路径服务只注册一次。多个 Scope 的 Hook 声明同一个键即可共用它；卸载消费者不会清理 application 拥有的服务。数据库等有生命周期的共享组件也遵循这一规则，依赖它的其他服务声明 `depends=(共享键,)`。把同一实例再注册给另一个 owner 会抛出 `ServiceOwnershipError`；存在外部消费者时，卸载提供者会被 `OwnerInUseError` 阻止。

`Bot.run_async()` 等到运行时真正清理完毕：正常停止返回，关键任务失败传播 `failure_cause`，没有关键故障但清理不成功时抛出 `ShutdownIncompleteError`。启动失败通过启动调用本身传播，`startup_failed` 与启动结果记录可以用于诊断。捕获错误后仍应查看 `wait_closed()` 返回的 `ShutdownReport`；一个故障原因和一个清理报告分别回答“为什么停”和“资源是否释放”。

Scope abort 会立即发出取消请求，不承诺强制结束拒绝取消的协程。卸载进行期间阻止新注册；取消未收敛时保留相应 Provider/Service，已禁用的 Hook 保持禁用，待旧工作结束后可以再次卸载。失败返回后 Scope 仍是 active，只有成功卸载才变为 inactive。对照 [服务器边界回归](../tests/integration/core/test_server_regressions.py) 中的拒绝取消与重试场景理解这一点。

当前实现以这份学习路线、[源码地图](CODE_MAP.md) 和测试为准。根目录 `FRAMEWORK_PLAN.md` 与 `target.md` 保存早期规划，里面的阶段和布局不是当前实现承诺。
