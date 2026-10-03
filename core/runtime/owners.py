"""In-flight owner accounting and resource cleanup for Scope unload."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..Lifecycle import RuntimeState, ShutdownReport, StopMode
from ..Ownership import OwnerKey, same_owner
from .components import _remove_identity

if TYPE_CHECKING:
    from ..Runtime import Runtime


def _retain_event_owners(runtime: Runtime, owners: tuple[object, ...]) -> None:
    for owner in owners:
        key = OwnerKey(owner)
        runtime._owner_events[key] = runtime._owner_events.get(key, 0) + 1


def _release_event_owners(runtime: Runtime, owners: tuple[object, ...]) -> None:
    changed = False
    for owner in owners:
        key = OwnerKey(owner)
        count = runtime._owner_events.get(key, 0) - 1
        if count > 0:
            runtime._owner_events[key] = count
        else:
            runtime._owner_events.pop(key, None)
        changed = True
    if changed:
        runtime._owner_changed.set()


async def wait_owner_events(runtime: Runtime, owner: object, timeout: float) -> bool:
    key = OwnerKey(owner)
    try:
        async with asyncio.timeout(timeout):
            while runtime._owner_events.get(key, 0):
                runtime._owner_changed.clear()
                if not runtime._owner_events.get(key, 0):
                    return True
                await runtime._owner_changed.wait()
        return True
    except TimeoutError:
        return False


async def _unload_owner_resources(
    runtime: Runtime, owner: object, mode: StopMode
) -> ShutdownReport:
    """Settle in-flight work and release resources owned by one scope."""

    report = ShutdownReport(forced=mode == "abort")
    if runtime.state == RuntimeState.RUNNING:
        deadline = asyncio.get_running_loop().time() + runtime.DRAIN_TIMEOUT
        drained = mode == "drain" and await runtime.wait_owner_events(
            owner, timeout=max(0, deadline - asyncio.get_running_loop().time())
        )
        if drained:
            drained = await runtime.tasks.wait_owner(
                owner, timeout=max(0, deadline - asyncio.get_running_loop().time())
            )
        if not drained:
            report.forced = True
            events, tasks = await asyncio.gather(
                runtime.scheduler.cancel_owner(owner, grace=runtime.CANCEL_GRACE),
                runtime.tasks.cancel_owner(owner, grace=runtime.CANCEL_GRACE),
            )
            report.abandoned_tasks = tuple(dict.fromkeys((*events, *tasks)))
            if report.abandoned_tasks:
                # These tasks may still access captured services/providers.
                # Keep their resources until a later unload can settle them.
                return report
            mode = "abort"

    deadline = asyncio.get_running_loop().time() + 30.0

    adapters = runtime.adapters_for_owner(owner)
    for adapter in reversed(adapters):
        if id(adapter) in runtime._started_adapter_ids:
            if await runtime._cleanup(adapter, "stop", mode, report, deadline):
                _remove_identity(runtime._started_adapters, adapter)
                runtime._started_adapter_ids.discard(id(adapter))
            else:
                continue
        if id(adapter) in runtime._setup_ids:
            if await runtime._cleanup(adapter, "teardown", None, report, deadline):
                _remove_identity(runtime._setup_components, adapter)
                runtime._setup_ids.discard(id(adapter))

    lifespans = runtime.lifespans_for_owner(owner)
    for registration in reversed(lifespans):
        if any(item is registration for item in runtime._entered_lifespans):
            if await runtime._cleanup(
                registration.manager, "__aexit__", None, report, deadline
            ):
                _remove_identity(runtime._entered_lifespans, registration)

    owned_handles = runtime.bot.services.handles_for_owner(owner)
    owned_ids = {id(handle) for handle in owned_handles}
    ordered = tuple(
        handle
        for handle in runtime.bot.services.lifecycle_order()
        if id(handle) in owned_ids
    )
    for handle in reversed(ordered):
        component = handle.component
        stopped = id(component) not in runtime._started_service_ids
        if not stopped:
            stopped = await runtime._cleanup(
                component, "stop", mode, report, deadline
            )
            if stopped:
                _remove_identity(runtime._started_services, component)
                runtime._started_service_ids.discard(id(component))
        torn_down = id(component) not in runtime._setup_ids
        if stopped and not torn_down:
            torn_down = await runtime._cleanup(
                component, "teardown", None, report, deadline
            )
            if torn_down:
                _remove_identity(runtime._setup_components, component)
                runtime._setup_ids.discard(id(component))
        if stopped and torn_down:
            _remove_identity(runtime._service_order, component)

    if not report.failures and not report.abandoned_tasks:
        adapter_ids = {id(adapter) for adapter in adapters}
        runtime._adapters[:] = [
            adapter for adapter in runtime._adapters
            if id(adapter) not in adapter_ids
        ]
        runtime._adapter_owners[:] = [
            item for item in runtime._adapter_owners
            if not same_owner(item[1], owner)
        ]
        runtime._lifespans[:] = [
            registration for registration in runtime._lifespans
            if not same_owner(registration.owner, owner)
        ]
    return report


def _has_owner_resources(runtime: Runtime, owner: object) -> bool:
    return bool(
        runtime.tasks.tasks_for(owner)
        or runtime.adapters_for_owner(owner)
        or runtime.lifespans_for_owner(owner)
        or runtime._owner_events.get(OwnerKey(owner), 0)
    )
