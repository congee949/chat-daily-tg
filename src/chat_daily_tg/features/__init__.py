"""Feature-level application entrypoints.

Business orchestration is migrated here incrementally.  These modules are the
stable boundary used by the CLI; they intentionally delegate to the proven
legacy implementation until each feature is moved without changing delivery
semantics.
"""
