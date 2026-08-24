"""The one QGIS session this process gets, because a second one segfaults it.

A `QgsApplication` can be built once per process. Building another after
`exitQgis()` dies with SIGSEGV, and every QGIS tool used to build and tear down
its own, so whichever QGIS tool ran second took the executor down with it and
every tool called after that reported an unreachable executor. The tools ask
here instead, and nothing calls `exitQgis()`: the session lives until the
process does.

A start that fails is remembered too. Retrying it would run
`QgsApplication([], False)` a second time, which is the crash.

This sits in core rather than beside the tools because the tool loader imports
tool modules as the top-level `tools` package and reloads them: a session held
next to them would exist once per import name, and the second copy would build
the second QgsApplication.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

# the tool venv is isolated; the qgis bindings and the processing plugin live in
# the system paths, so bridge them onto sys.path
QGIS_SYSTEM_PATHS = (
    "/usr/lib/python3/dist-packages",
    "/usr/share/qgis/python",
    "/usr/share/qgis/python/plugins",
)
QGIS_PREFIX_PATH = "/usr"

_start_lock = threading.Lock()
_session: QgisSession | None = None
_start_failure: QgisUnavailable | None = None


class QgisUnavailable(RuntimeError):
    """QGIS cannot run in this process, with the reason a tool can print."""


class QgisSession:
    """The process's QgsApplication, and the processing plugin loaded onto it."""

    def __init__(self, application, processing, processing_error: str | None):
        # kept referenced for the life of the process: QGIS reads the
        # application back out of its own static state on every call
        self.application = application
        self.processing = processing
        self.processing_error = processing_error

    def registry(self):
        from qgis.core import QgsApplication

        return QgsApplication.processingRegistry()

    def algorithms(self):
        return self.registry().algorithms()

    def algorithm_by_id(self, algorithm_id: str):
        return self.registry().algorithmById(algorithm_id)


def _bridge_system_paths() -> None:
    for path in QGIS_SYSTEM_PATHS:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.append(path)


def _start_session() -> QgisSession:
    _bridge_system_paths()
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QGIS_PREFIX_PATH"] = QGIS_PREFIX_PATH

    try:
        from qgis.analysis import QgsNativeAlgorithms
        from qgis.core import QgsApplication
    except ImportError as e:
        raise QgisUnavailable(f"the QGIS python bindings are not importable: {e}") from e

    QgsApplication.setPrefixPath(QGIS_PREFIX_PATH, True)
    application = QgsApplication([], False)
    application.initQgis()
    QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())

    processing = None
    processing_error = None
    try:
        # a real import, not find_spec: the plugin can be on the path and still
        # fail to load without its qgis dependencies
        import processing as processing_module
        from processing.core.Processing import Processing

        Processing.initialize()
        processing = processing_module
    except Exception as e:
        processing_error = str(e)
        logger.warning(f"QGIS processing plugin unavailable: {e}")

    logger.info(
        f"QGIS session started: {len(QgsApplication.processingRegistry().algorithms())} "
        f"algorithms, processing {'ready' if processing else 'unavailable'}"
    )
    return QgisSession(application, processing, processing_error)


def qgis_session() -> QgisSession:
    """The process's QGIS session, started on first use.

    Raises `QgisUnavailable` when QGIS cannot start here, and raises the same
    reason on every later call rather than trying again.
    """
    global _session, _start_failure

    with _start_lock:
        if _start_failure is not None:
            raise _start_failure
        if _session is not None:
            return _session
        try:
            _session = _start_session()
        except QgisUnavailable as e:
            _start_failure = e
            raise
        except Exception as e:
            _start_failure = QgisUnavailable(f"QGIS could not start: {e}")
            raise _start_failure from e
        return _session
