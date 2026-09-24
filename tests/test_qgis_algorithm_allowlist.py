import inspect
import re

import pytest

from src.agents.tools import run_qgis_algorithm as run_qgis_algorithm_module
from src.agents.tools.run_qgis_algorithm import RunQGISAlgorithmArgs, run_qgis_algorithm
from src.core.qgis_session import (
    ALLOWED_ALGORITHM_IDS,
    AlgorithmNotAllowed,
    QgisSession,
)
from tool_sweep.arguments import SWEEP_ARGUMENTS

ALGORITHM_ID = re.compile(r"'((?:native|qgis):\w+)'")

NETWORK_OR_CODE_ALGORITHMS = [
    "native:filedownloader",
    "native:httprequest",
    "native:openurl",
    "native:batchnominatimgeocoder",
    "native:downloadvectortiles",
    "native:postgisexecutesql",
    "native:spatialiteexecutesql",
    "native:fieldcalculator",
    "qgis:advancedpythonfieldcalculator",
    "qgis:executesql",
    "gdal:warpreproject",
    "script:anything",
    "model:anything",
]


class FakeProcessing:
    def __init__(self):
        self.runs = []

    def run(self, algorithm_id, parameters):
        self.runs.append(algorithm_id)
        return {"OUTPUT": "out.gpkg"}


@pytest.mark.parametrize("algorithm_id", NETWORK_OR_CODE_ALGORITHMS)
def test_the_tool_refuses_an_algorithm_off_the_list_before_qgis_starts(
    monkeypatch, algorithm_id
):
    started = []
    monkeypatch.setattr(
        run_qgis_algorithm_module, "qgis_session", lambda: started.append(True)
    )

    result = run_qgis_algorithm(algorithm_id, '{"URL": "http://169.254.169.254/"}')

    assert result.startswith("❌")
    assert f"'{algorithm_id}' is not an allowed QGIS algorithm" in result
    assert started == []


def test_the_session_runs_only_allowed_algorithms():
    processing = FakeProcessing()
    session = QgisSession(None, processing, None)

    with pytest.raises(AlgorithmNotAllowed, match="native:filedownloader"):
        session.run("native:filedownloader", {"URL": "http://169.254.169.254/"})
    session.run("native:buffer", {"INPUT": "roads.gpkg"})

    assert processing.runs == ["native:buffer"]


def test_every_algorithm_the_tool_description_names_is_allowed():
    description = inspect.getdoc(run_qgis_algorithm) + str(
        RunQGISAlgorithmArgs.model_json_schema()
    )
    named = set(ALGORITHM_ID.findall(description))

    assert named
    assert named <= ALLOWED_ALGORITHM_IDS


def test_the_nightly_sweeps_algorithm_is_allowed():
    sample = SWEEP_ARGUMENTS["run_qgis_algorithm"].args["algorithm_id"]

    assert sample in ALLOWED_ALGORITHM_IDS
