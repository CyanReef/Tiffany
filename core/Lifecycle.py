from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Protocol, runtime_checkable


StopMode = Literal["drain", "abort"]


class RuntimeState(str, Enum):
    NEW = "new"
    SETTING_UP = "setting_up"
    SETUP = "setup"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING_DRAIN = "stopping_drain"
    STOPPING_ABORT = "stopping_abort"
    STOPPED = "stopped"
    TEARING_DOWN = "tearing_down"
    TERMINATED = "terminated"
    STOP_FAILED = "stop_failed"


class LifecycleError(RuntimeError):
    pass


class RuntimeNotRunningError(LifecycleError):
    def __init__(self, state: RuntimeState):
        super().__init__(f"runtime is not accepting events in state {state.value!r}")
        self.state = state


class RuntimeOverloadedError(RuntimeError):
    pass


class ComponentStartupTimeoutError(LifecycleError):
    def __init__(self, component: str, phase: str, timeout: float):
        super().__init__(
            f"component {component!r} {phase} exceeded {timeout:g} seconds"
        )
        self.component = component
        self.phase = phase
        self.timeout = timeout


class DuplicateAdapterIdError(LifecycleError):
    def __init__(self, adapter_id: str):
        super().__init__(f"adapter id {adapter_id!r} is already installed")
        self.adapter_id = adapter_id


class ShutdownIncompleteError(LifecycleError):
    def __init__(self, report: "ShutdownReport"):
        super().__init__("runtime shutdown did not complete cleanly")
        self.report = report


@dataclass(frozen=True, slots=True)
class CleanupResult:
    component: str
    phase: str
    outcome: Literal["completed", "error", "timeout", "abandoned"]
    duration: float
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class StartupResult:
    component: str
    phase: str
    outcome: Literal["completed", "error", "timeout"]
    duration: float
    error_type: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class ShutdownReport:
    forced: bool = False
    drain_duration: float = 0.0
    results: list[CleanupResult] = field(default_factory=list)
    failures: list[BaseException] = field(default_factory=list, repr=False)
    abandoned_tasks: tuple[str, ...] = ()
    abandoned_components: tuple[str, ...] = ()
    startup_results: tuple[StartupResult, ...] = ()

    @property
    def successful(self) -> bool:
        return (
            not self.failures
            and not self.abandoned_tasks
            and not self.abandoned_components
        )


@runtime_checkable
class Lifecycle(Protocol):
    async def setup(self, runtime: object) -> None: ...

    async def start(self) -> None: ...

    async def stop(self, mode: StopMode) -> None: ...

    async def teardown(self) -> None: ...
