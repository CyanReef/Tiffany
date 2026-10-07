# 共享预算与按需调度

Tiffany 按事件到达分配缓冲，按可运行会话创建业务 Task。同会话最多一个事件执行，保持 FIFO；跨会话在全局和 Adapter 并发额度内执行。没有常驻 worker、定时扩容、磁盘队列或新增运行依赖。原始事件、同步惰性字段、Hook 顺序、独立 ContextVar 和 Runtime/Scope 资源归属保持原有契约。

## 配置

核心与部署共用标准库层的 `SchedulerPolicy`，默认值一致：

```python
from core import Bot, SchedulerPolicy

bot = Bot(scheduler=SchedulerPolicy(
    max_events=8192,
    buffer_budget_bytes=64 * 1024 * 1024,
    max_concurrency=64,
))
```

```toml
[scheduler]
max_events = 8192
buffer_budget_bytes = 67108864
max_concurrency = 64

[adapter.websocket]
# 不设置 workers 时继承全局并发；显式设置可限制此 Adapter。
# workers = 8
pending_limit = 256
```

QQ 使用 `[adapter.qqofficial]` 中的可选 `workers` 和独立的 `max_inflight = 16`。事件扩容不提高 QQ HTTP 或 OneBot `pending_limit` 的 API 额度。固定 `session_backlog` 和 OneBot 独立提交队列的 `queue_size` 已移除。

运行时调整 `scheduler.global_limit` 或 `configure_adapter(id, limit)` 只约束后续启动，不取消当前活动事件。继承全局的 Adapter 会跟随全局上限；核心测试可暂设全局并发 0 暂停启动，公开初始策略必须为正整数。

## 接纳与保留额度

在途数包括排队与执行中的事件，估算占用在统一结束路径归还一次。总事件数、字节预算各保留 1/8；共享区有余量时，任一会话可使用。超出任一共享区水位后，只接纳符合下列条件的有会话事件：接纳后本会话在途不超过 32，估算占用不超过 `min(256 KiB, 保留字节预算)`。所有事件均不能突破总预算。

**32 是使用保留额度的资格线，不是会话积压上限。** 默认预算内，单会话可缓冲 128、256、512、1024、4096 条普通事件。热门会话达到共享区水位后停止占用，低占用会话仍可利用保留区。无 `session_id` 的事件独立执行，只使用共享区。

`Envelope.admission_bytes` 可指定正整数计费值；缺省为 4096 字节。内置 Adapter 按 `2048 + 12 * sys.getsizeof(frame)` 计费，不遍历或重新编码 `raw`。这是**估算占用预算**，不是 RSS 硬限制；Hook 分配的对象、下游连接资源和调用方保留的结果不包含在预算中。应用自己持有原始事件或 Future 时，应自行管理其生命周期。

## 提交与取消

```python
from core import Envelope

future = bot.runtime.submit(Envelope('demo', raw, session_id='room:1'))
if future is None:
    # 未接纳；调用方决定拒绝后的业务策略。
    ...
else:
    context = await future
```

`submit()` 同步完成接纳，不等待容量腾空或业务完成；必须在运行时所属事件循环调用。预算通过后才创建 Future、捕获注册快照并保留 owner。默认拒绝返回 `None`；`reject=True` 抛出 `RuntimeOverloadedError`。

异步 `emit(wait=True/False)` 共用同一接纳路径。`wait=True` 的等待者取消会撤销排队事件或取消其活动 Task；`wait=False` 和同步 `submit()` 返回的 Future 被取消只停止结果等待，已接纳事件继续执行。拒绝取消的任务继续占用预算和资源。降低额度不会丢弃已接纳事件，业务异常、主动取消和停机超时遵循现有语义。

## 公平调度与接入

可运行 Adapter 做轮转，Adapter 内就绪会话做轮转。达到 Adapter 并发上限时暂停进入可运行集合，提交不扫描大量被阻塞会话。单会话突发通过缓冲吸收；严格 FIFO 下，该会话的处理速度仍由单条业务耗时决定。

有执行余量且没有已就绪的前序会话时，接纳和完成后的下一条事件直接进入同一个任务启动分支，减少就绪集合的创建和删除。Adapter 曾达到并发上限时，其等待会话仍享有先前的轮转位置，不会被刚完成事件的会话越过。

OneBot/QQ 读取循环直接同步接纳，连续处理 64 帧后主动让出事件循环。API echo、心跳与控制消息继续由协议读取循环处理，接纳失败不等待容量。OneBot 记录拒绝并限频输出日志；QQ 拒绝后保留旧恢复检查点并重连，不新增框架重试。

Adapter drain 按需等待 Scheduler 中自己的排队和活动计数，保留连接和客户端供已接纳 Hook 完成 API 调用；排空后再关闭。没有逐事件提交 Task、签名反射或 drain 回调。超时升级 abort、Scope 卸载和资源保留沿用 Runtime 的管理。

## 观测

业务指标仍立即写入原指标注册表。预算附加值由 `scheduler.snapshot()` 在抓取 OpenMetrics 时读取，不增加逐事件指标写锁：

| Gauge | 含义 |
| --- | --- |
| `event_buffer_estimated_bytes` | 当前估算占用 |
| `event_buffer_budget_bytes` / `event_buffer_available_bytes` | 总字节预算 / 剩余额度 |
| `event_slots_available` | 剩余在途条数 |
| `event_reserved_slots` / `event_reserved_bytes` | 保留额度 |
| `event_concurrency_limit` | 全局执行上限 |

`events_rejected_total` 的原因分别为 `capacity`、`buffer_budget`、`reserved_capacity`，不带会话 ID 标签。readiness 综合运行状态与总预算余量，表达整体接纳能力；消息大小、会话资格及保留区规则仍可能导致具体事件被拒绝。

正确性验证见 [弹性调度测试](../../tests/integration/core/test_elastic_scheduler.py)、[调度回归](../../tests/integration/core/test_scheduler_regressions.py)、[真实 WebSocket 满载测试](../../tests/integration/onebot/test_elastic_transport.py)。性能复测方法见 [基准指南](../development/benchmarks.md)。
