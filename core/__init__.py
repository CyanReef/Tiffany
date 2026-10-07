"""Tiffany 的公共 API，按入口、事件、注册、运行和观测职责组织。

源码分类见 docs/development/code-map.md，建议从 Bot、Envelope 和 Context 开始阅读。
"""

# 1. 开发入口：注册功能、划定资源归属。
from .Bot import Bot
from .Scope import OwnerInUseError, Scope
from .Paths import APP_PATHS, RuntimePaths

# 2. 事件模型与分发：描述消息、读取字段、执行 Hook。
from .Envelope import Envelope
from .Context import Context
from .Field import Field
from .Hook import Hook, HookErrorPolicy
from .Dispatcher import (
    Dispatcher,
    HookExecutionError,
    HookHandle,
    HookSnapshot,
    HookTimeoutError,
)

# 3. 注册与依赖：声明数据来源、共享服务和可撤销注册。
from .Provider import (
    Provider,
    ProviderConflictError,
    ProviderContext,
    ProviderHandle,
    ProviderRegistry,
    ProviderSnapshot,
)
from .Service import (
    ServiceConflictError,
    ServiceOwnershipError,
    ServiceHandle,
    ServiceKey,
    ServiceRegistry,
    ServiceSnapshot,
)

# 4. 运行与生命周期：启动、任务监督、关闭及清理报告。
from .Runtime import Lifespan, Runtime
from .SchedulerPolicy import SchedulerPolicy
from .TaskRegistry import TaskInfo, TaskRegistry
from .Lifecycle import (
    CleanupResult,
    ComponentStartupTimeoutError,
    DuplicateAdapterIdError,
    LifecycleError,
    RuntimeNotRunningError,
    RuntimeOverloadedError,
    RuntimeState,
    ShutdownIncompleteError,
    ShutdownReport,
    StartupResult,
    StopMode,
)

# 5. 观测：指标快照、HTTP 导出和事件执行追踪。
from .Metrics import MetricRegistry, MetricSample, MetricSnapshot
from .OpenMetrics import MissingOptionalDependencyError, OpenMetricsExporter
from .Trace import DispatchRecord, TraceRecorder


__all__ = [
    # 开发入口
    "Bot",
    "Scope",
    "OwnerInUseError",
    "RuntimePaths",
    "APP_PATHS",
    # 事件模型与分发
    "Envelope",
    "Context",
    "Field",
    "Hook",
    "HookErrorPolicy",
    "Dispatcher",
    "HookHandle",
    "HookSnapshot",
    "HookExecutionError",
    "HookTimeoutError",
    # 注册与依赖
    "Provider",
    "ProviderContext",
    "ProviderRegistry",
    "ProviderHandle",
    "ProviderSnapshot",
    "ProviderConflictError",
    "ServiceKey",
    "ServiceRegistry",
    "ServiceHandle",
    "ServiceSnapshot",
    "ServiceConflictError",
    "ServiceOwnershipError",
    # 运行与生命周期
    "Runtime",
    "SchedulerPolicy",
    "Lifespan",
    "TaskRegistry",
    "TaskInfo",
    "RuntimeState",
    "StopMode",
    "LifecycleError",
    "RuntimeNotRunningError",
    "RuntimeOverloadedError",
    "ComponentStartupTimeoutError",
    "DuplicateAdapterIdError",
    "ShutdownIncompleteError",
    "StartupResult",
    "CleanupResult",
    "ShutdownReport",
    # 观测
    "MetricRegistry",
    "MetricSample",
    "MetricSnapshot",
    "OpenMetricsExporter",
    "MissingOptionalDependencyError",
    "DispatchRecord",
    "TraceRecorder",
]
