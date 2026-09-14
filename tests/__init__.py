"""Test suite for opsx-controller.

The suite is hermetic with respect to the ambient ``OPSX_*`` dispatch
environment. The controller exports ``OPSX_<ROLE>_MODEL`` and
``OPSX_<ROLE>_VARIANT`` into its own process (``apply_model_env`` in
``orchestrator/opsx-plan.py``), and any worker it spawns inherits them. Tests
that assert a clean environment (default variants, no model patch) would then
fail purely from being run inside a supervised worker.

Because the suite already passes with no ``OPSX_*`` variable set, forcing a
clean start is safe and makes every run independent of the caller's dispatch
environment. Each test that needs a model pin or variant sets it explicitly
(e.g. via ``mock.patch.dict``), so clearing ambient values changes nothing for
those tests.
"""

from __future__ import annotations

import atexit
import os

_OPSX_PREFIX = "OPSX_"

# Snapshot the ambient dispatch environment, then clear it for the whole test
# process. Restore it on exit so a caller that inspects ``os.environ`` after
# the run sees the values it started with.
_SAVED_OPSX_ENV = {
    key: value for key, value in os.environ.items() if key.startswith(_OPSX_PREFIX)
}


def _restore_opsx_env() -> None:
    for key in [k for k in os.environ if k.startswith(_OPSX_PREFIX)]:
        del os.environ[key]
    os.environ.update(_SAVED_OPSX_ENV)


for _key in list(_SAVED_OPSX_ENV):
    del os.environ[_key]

atexit.register(_restore_opsx_env)
