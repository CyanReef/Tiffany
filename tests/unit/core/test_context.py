"""Lazy field resolution and provider contracts."""

import inspect
import unittest

from core import Context, Envelope, Field, ProviderRegistry


class ContextTests(unittest.TestCase):
    def test_provider_is_lazy_and_cached_even_for_none(self):
        field = Field[object | None]("test.value")
        registry = ProviderRegistry()
        calls = 0

        def provide(ctx):
            nonlocal calls
            calls += 1
            return None

        registry.add(field, provide)
        raw = {"untouched": True}
        ctx = Context(Envelope("test", raw), registry)

        self.assertEqual(calls, 0)
        self.assertIs(ctx.raw, raw)
        self.assertIsNone(ctx.resolve(field))
        self.assertIsNone(ctx.resolve(field))
        self.assertEqual(calls, 1)

    def test_platform_provider_overrides_fallback(self):
        field = Field[str]("test.value")
        registry = ProviderRegistry()
        registry.add(field, lambda ctx: "fallback")
        registry.add(field, lambda ctx: "onebot", platform="napcat")

        ctx = Context(Envelope("napcat", {}), registry)

        self.assertEqual(ctx.resolve(field), "onebot")

    def test_missing_provider_error_names_field_and_platform(self):
        field = Field[str]("missing")
        ctx = Context(Envelope("napcat", {}), ProviderRegistry())

        with self.assertRaisesRegex(LookupError, "missing"):
            ctx.resolve(field)

    def test_fields_with_the_same_name_are_not_aliased(self):
        first = Field[str]("same.name")
        second = Field[int]("same.name")

        self.assertIsNot(first, second)
        self.assertNotEqual(first, second)

    def test_async_provider_is_rejected(self):
        field = Field[str]("async")
        registry = ProviderRegistry()

        async def provide(ctx):
            return "value"

        self.assertTrue(inspect.iscoroutinefunction(provide))
        with self.assertRaisesRegex(TypeError, "synchronous"):
            registry.add(field, provide)

    def test_provider_returning_an_awaitable_is_rejected(self):
        field = Field[str]("hidden-async")
        registry = ProviderRegistry()

        async def value():
            return "value"

        registry.add(field, lambda ctx: value())
        ctx = Context(Envelope("test", {}), registry)

        with self.assertRaisesRegex(TypeError, "synchronous"):
            ctx.resolve(field)

    def test_transitive_dependency_is_validated_and_cycles_are_rejected(self):
        root = Field[str]("root")
        dependency = Field[str]("dependency")
        registry = ProviderRegistry()
        registry.add(root, lambda ctx: "root", requires=(dependency,))

        with self.assertRaisesRegex(LookupError, "dependency"):
            registry.validate((root,), "test")

        cyclic = ProviderRegistry()
        cyclic.add(root, lambda ctx: "root", requires=(dependency,))
        cyclic.add(dependency, lambda ctx: "dependency", requires=(root,))

        with self.assertRaisesRegex(ValueError, "cycle"):
            cyclic.validate((root,), "test")

    def test_declared_dependencies_are_lazy_until_provider_uses_them(self):
        root = Field[str]("root")
        dependency = Field[str]("dependency")
        registry = ProviderRegistry()
        dependency_calls = 0

        def provide_dependency(ctx):
            nonlocal dependency_calls
            dependency_calls += 1
            return "dependency"

        registry.add(dependency, provide_dependency)
        registry.add(root, lambda ctx: "root", requires=(dependency,))
        registry.validate((root,), "test")
        ctx = Context(Envelope("test", {}), registry)

        self.assertEqual(ctx.resolve(root), "root")
        self.assertEqual(dependency_calls, 0)


if __name__ == "__main__":
    unittest.main()
