"""Provider registration, snapshots, conflicts and ownership."""

import unittest

from core.Field import Field
from core.Provider import ProviderConflictError, ProviderRegistry


class ProviderRegistryTests(unittest.TestCase):
    def test_handle_metadata_idempotence_and_revoke(self):
        registry = ProviderRegistry()
        field = Field[str]("value")
        owner = object()

        def provide(ctx):
            return "value"

        first = registry.add(
            field,
            provide,
            platform="test",
            owner=owner,
            namespace="extension.demo",
            source="demo:provide",
        )
        revision = registry.revision
        second = registry.add(
            field,
            provide,
            platform="test",
            owner=owner,
            namespace="extension.demo",
            source="demo:provide",
        )

        self.assertIs(first, second)
        self.assertEqual(registry.revision, revision)
        self.assertEqual(first.namespace, "extension.demo")
        self.assertIs(first.owner, owner)
        self.assertEqual(first.source, "demo:provide")
        self.assertIs(first.field, field)
        self.assertEqual(first.platform, "test")
        self.assertTrue(first.active)
        self.assertTrue(first.revoke())
        self.assertFalse(first.active)
        self.assertFalse(first.revoke())
        self.assertNotIn(first.id, registry._handles)

    def test_snapshot_is_stable_across_copy_on_write_mutations(self):
        registry = ProviderRegistry()
        field = Field[str]("value")
        handle = registry.add(field, lambda ctx: "value")
        snapshot = registry.snapshot()

        handle.revoke()

        with self.assertRaises(LookupError):
            registry.get(field, "test")
        self.assertEqual(registry.get(field, "test", snapshot).provider(None), "value")
        self.assertLess(snapshot.revision, registry.revision)
        with self.assertRaises(TypeError):
            snapshot.providers[field][None] = registry.get(field, "test", snapshot)

    def test_conflict_is_structured_and_namespace_is_not_a_lookup_slot(self):
        registry = ProviderRegistry()
        field = Field[str]("value")
        first_owner = object()
        second_owner = object()

        registry.add(
            field,
            lambda ctx: "first",
            owner=first_owner,
            namespace="first.extension",
            source="first.py:1",
        )
        with self.assertRaises(ProviderConflictError) as raised:
            registry.add(
                field,
                lambda ctx: "second",
                owner=second_owner,
                namespace="second.extension",
                source="second.py:2",
            )

        error = raised.exception
        self.assertIs(error.field, field)
        self.assertEqual(error.existing.namespace, "first.extension")
        self.assertEqual(error.incoming.namespace, "second.extension")
        self.assertIn("first.py:1", str(error))
        self.assertIn("second.py:2", str(error))

    def test_owner_query_and_atomic_revoke(self):
        registry = ProviderRegistry()
        owner = object()
        other = object()
        first = registry.add(Field[str]("first"), lambda ctx: "first", owner=owner)
        second = registry.add(Field[str]("second"), lambda ctx: "second", owner=owner)
        remaining = registry.add(Field[str]("third"), lambda ctx: "third", owner=other)
        revision = registry.revision

        self.assertEqual(registry.handles_for_owner(owner), (first, second))
        self.assertEqual(registry.revoke_owner(owner), 2)
        self.assertEqual(registry.revision, revision + 1)
        self.assertFalse(first.active)
        self.assertFalse(second.active)
        self.assertTrue(remaining.active)
        self.assertEqual(registry.revoke_owner(owner), 0)

    def test_async_callable_objects_are_rejected(self):
        class AsyncProvider:
            async def __call__(self, ctx):
                return "value"

        registry = ProviderRegistry()
        with self.assertRaisesRegex(TypeError, "synchronous"):
            registry.add(Field[str]("value"), AsyncProvider())

    def test_validation_can_use_an_old_snapshot(self):
        registry = ProviderRegistry()
        dependency = Field[str]("dependency")
        root = Field[str]("root")
        registry.add(dependency, lambda ctx: "dependency")
        dependency_handle = registry.add(
            root,
            lambda ctx: "root",
            requires=(dependency,),
        )
        snapshot = registry.snapshot()
        dependency_handle.revoke()

        registry.validate((root,), "test", snapshot)
        with self.assertRaises(LookupError):
            registry.validate((root,), "test")


if __name__ == "__main__":
    unittest.main()
