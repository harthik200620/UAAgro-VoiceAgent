"""The client's own database as a source for the catalogue (§15.1).

Three modules, one direction of travel: :mod:`mysql` reads the client's
server and nothing else, :mod:`mapping` says what their columns mean, and
:mod:`sync` writes the catalogue and records the run. The panel's router and
the worker's scheduled job both build on :mod:`sync`; only :mod:`sync` and
the router's connection test touch :mod:`mysql`.

Import the submodules rather than names from here: the tests replace
``mysql.connect`` on its module, and a name copied at import time would keep
pointing at the real driver.
"""

from __future__ import annotations

from . import mapping, mysql, sync

__all__ = ("mapping", "mysql", "sync")
