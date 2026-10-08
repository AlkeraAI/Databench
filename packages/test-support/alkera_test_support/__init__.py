"""Test fakes for the shared packages.

The root workspace installs this package through its dev dependency group only,
so it never reaches an app image or a release binary, and no module under a
shipped package may import it (``packages/api-core/tests/test_test_support_not_shipped.py``).
"""
