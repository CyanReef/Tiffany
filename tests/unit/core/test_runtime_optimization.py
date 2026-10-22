"""Snapshot lifetime and awaitable detection must survive the fast paths."""
import asyncio
import gc
import subprocess
import sys
import threading
import unittest
import weakref
from unittest.mock import patch

from core import Bot, Context, Envelope, Field, ProviderRegistry, ServiceKey
from core.Service import ServiceRegistry


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoke_releases_cached_resources_without_another_event(self):
        class Resource: pass
        bot = Bot()
        scope = bot.scope("resource")
        key = ServiceKey("resource")
        resource = Resource()
        reference = weakref.ref(resource)
        scope.service(key, resource)
        del resource
        bot.runtime._capture_event(Envelope("test", {}))
        await scope.unload()
        gc.collect()
        self.assertIsNone(reference())

    async def test_unchanged_snapshots_reuse_and_old_views_remain_valid(self):
        bot = Bot()
        event = Envelope("test", {})
        before, _ = bot.runtime._capture_event(event)
        again, _ = bot.runtime._capture_event(event)
        self.assertIs(before, again)
        field = Field("new")
        provider = bot.provide(field, lambda ctx: 1)
        after, _ = bot.runtime._capture_event(event)
        self.assertNotIn(field, before.providers.providers)
        self.assertIn(field, after.providers.providers)
        provider.revoke()
        self.assertIsNone(bot.runtime._snapshot_cache)
        self.assertIn(field, after.providers.providers)

    async def test_cross_thread_revocation_cannot_republish_a_cached_resource(self):
        bot = Bot()
        key = ServiceKey("thread-resource")
        handle = bot.service(key, object())
        event = Envelope("test", {})
        old, _ = bot.runtime._capture_event(event)
        # Force construction while the registry changes. Reusing an already
        # published immutable snapshot does not republish it into the cache.
        bot.provide(Field("invalidate"), lambda ctx: 1)
        start, published = threading.Event(), threading.Event()
        original = ServiceRegistry.snapshot
        errors = []

        def changed():
            published.set()
            bot._registrations_changed()
        bot.services._on_change = changed

        def revoke():
            try:
                if not start.wait(2):
                    raise AssertionError("capture did not start")
                handle.revoke()
            except BaseException as error:
                errors.append(error)

        def capture(registry):
            if registry is not bot.services:
                return original(registry)
            snapshot = original(registry)
            start.set()
            if not published.wait(2):
                raise AssertionError("registration did not publish")
            return snapshot

        worker = threading.Thread(target=revoke)
        worker.start()
        try:
            with patch.object(ServiceRegistry, "snapshot", capture):
                held, _ = bot.runtime._capture_event(event)
        finally:
            start.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIs(held.services, old.services)
        self.assertIsNone(bot.runtime._snapshot_cache)
        latest, _ = bot.runtime._capture_event(event)
        self.assertNotIn(key, latest.services.services)
        self.assertIn(key, held.services.services)


class AwaitableTests(unittest.TestCase):
    def test_ordinary_event_data_is_released_without_waiting_for_gc(self):
        class Marker: pass
        field = Field("text")
        providers = ProviderRegistry()
        providers.add(field, lambda view: view.raw["text"])
        marker = Marker()
        reference = weakref.ref(marker)
        ctx = Context(Envelope("test", {"text": "value", "marker": marker}), providers)
        self.assertEqual(ctx.resolve(field), "value")
        enabled = gc.isenabled()
        gc.disable()
        try:
            del marker, ctx
            self.assertIsNone(reference())
        finally:
            gc.collect()
            if enabled:
                gc.enable()

    def test_retained_provider_view_keeps_shared_cache_and_captured_snapshot(self):
        first, second, published = Field("first"), Field("second"), Field("published")
        providers = ProviderRegistry()
        seen = []
        def capture(view):
            seen.append(view)
            return view.raw["text"]
        providers.add(first, capture)
        handle = providers.add(second, lambda view: view.resolve(first) + " original", requires=(first,))
        ctx = Context(Envelope("test", {"text": "value"}), providers)
        ctx.resolve(first)
        ctx.put(published, 42)
        del ctx
        handle.revoke()
        providers.add(second, lambda view: "replacement")
        view = seen[0]
        self.assertEqual(view.resolve(published), 42)
        self.assertEqual(view.resolve(second), "value original")
        self.assertEqual(len(seen), 1)
        self.assertFalse(hasattr(view, "reply"))
        self.assertFalse(hasattr(view, "service"))

    def test_builtin_subclass_awaitables_and_generator_coroutines_are_rejected(self):
        import types
        class AwaitableString(str):
            def __await__(self):
                yield
                return self
        @types.coroutine
        def generator():
            yield
        for value in (AwaitableString("value"), generator()):
            field = Field("value")
            providers = ProviderRegistry()
            providers.add(field, lambda ctx, value=value: value)
            with self.assertRaisesRegex(TypeError, "synchronous"):
                Context(Envelope("test", {}), providers).resolve(field)
            if hasattr(value, "close"):
                value.close()

    def test_virtual_awaitable_registration_invalidates_builtin_classification(self):
        code = """
from collections.abc import Awaitable
from core import Context, Envelope, Field, ProviderRegistry
def view():
    field=Field('text')
    providers=ProviderRegistry()
    providers.add(field,lambda ctx:'value')
    return Context(Envelope('test',{}),providers),field
ctx,field=view()
assert ctx.resolve(field)=='value'
Awaitable.register(str)
ctx,field=view()
try:
    ctx.resolve(field)
except TypeError:
    pass
else:
    raise AssertionError('virtual registration was ignored')
"""
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_providers_share_one_lazy_capability_view(self):
        fields = Field("a"), Field("b")
        providers = ProviderRegistry()
        seen = []
        def value(view):
            seen.append(view)
            return view.raw["text"]
        for field in fields:
            providers.add(field, value)
        ctx = Context(Envelope("test", {"text": "value"}), providers)
        self.assertIsNone(ctx._provider_view)
        self.assertEqual(ctx.resolve(fields[0]), "value")
        self.assertEqual(ctx.resolve(fields[1]), "value")
        self.assertIs(seen[0], seen[1])
