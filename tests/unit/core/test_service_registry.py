"""Service identity, dependencies, snapshots and ownership."""

import unittest

from core.Service import ServiceConflictError, ServiceKey, ServiceRegistry


class ServiceRegistryTests(unittest.TestCase):
    def test_keys_are_identity_based_and_lookup_is_explicit(self):
        registry = ServiceRegistry()
        first = ServiceKey[object]("http")
        same_name = ServiceKey[object]("http")
        service = object()
        registry.register(first, service)

        self.assertIs(registry.get(first), service)
        with self.assertRaises(LookupError):
            registry.get(same_name)

    def test_handle_snapshot_conflict_and_revoke(self):
        registry = ServiceRegistry()
        key = ServiceKey[object]("database")
        owner = object()
        service = object()
        handle = registry.register(
            key,
            service,
            owner=owner,
            namespace="extension.data",
            source="data:setup",
        )
        snapshot = registry.snapshot()
        duplicate = registry.register(
            key,
            service,
            owner=owner,
            namespace="extension.data",
            source="data:setup",
        )

        self.assertIs(handle, duplicate)
        self.assertIs(handle.component, service)
        with self.assertRaises(ServiceConflictError) as raised:
            registry.register(
                key,
                object(),
                owner=object(),
                namespace="other",
            )
        self.assertEqual(raised.exception.existing.namespace, "extension.data")

        self.assertTrue(handle.revoke())
        self.assertIs(registry.get(key, snapshot), service)
        self.assertEqual(registry.lifecycle_order(snapshot)[0].service, service)
        self.assertNotIn(handle.id, registry._handles)
        with self.assertRaises(LookupError):
            registry.get(key)

    def test_dependencies_are_validated_and_ordered(self):
        registry = ServiceRegistry()
        database = ServiceKey[object]("database")
        repository = ServiceKey[object]("repository")
        database_handle = registry.register(database, object())
        repository_handle = registry.register(
            repository,
            object(),
            dependencies=(database,),
        )

        self.assertEqual(
            registry.lifecycle_order(),
            (database_handle, repository_handle),
        )

        missing_registry = ServiceRegistry()
        missing_registry.register(repository, object(), dependencies=(database,))
        with self.assertRaisesRegex(LookupError, "database"):
            missing_registry.validate()

        cyclic_registry = ServiceRegistry()
        cyclic_registry.register(database, object(), dependencies=(repository,))
        cyclic_registry.register(repository, object(), dependencies=(database,))
        with self.assertRaisesRegex(ValueError, "cycle"):
            cyclic_registry.validate()

    def test_owner_revoke_is_atomic_and_idempotent(self):
        registry = ServiceRegistry()
        owner = "extension"
        first = registry.register(ServiceKey[object]("first"), object(), owner=owner)
        second = registry.register(ServiceKey[object]("second"), object(), owner=owner)
        revision = registry.revision

        self.assertEqual(registry.handles_for_owner(owner), (first, second))
        self.assertEqual(registry.revoke_owner(owner), 2)
        self.assertEqual(registry.revision, revision + 1)
        self.assertFalse(first.active)
        self.assertFalse(second.active)
        self.assertEqual(registry.revoke_owner(owner), 0)


if __name__ == "__main__":
    unittest.main()
