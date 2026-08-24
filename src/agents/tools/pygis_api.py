from pydantic import BaseModel, Field
from typing import Optional

from src.core.errors import PathRefused
from src.core.qgis_session import QgisUnavailable, qgis_session
from src.core.utils import tool_input_path


class PyQGISArgs(BaseModel):
    function_name: str = Field(
        ...,
        description="Name of the PyQGIS function or algorithm (e.g., 'QgsVectorLayer', 'native:buffer').",
    )
    uri: Optional[str] = Field(
        None, description="Path or URI for layer loading (e.g., 'data.shp')."
    )
    layer_name: Optional[str] = Field(
        "layer", description="Name for the layer (default: 'layer')."
    )


def confined_uri(uri: str) -> str:
    name, separator, suffix = uri.partition("|")
    return tool_input_path("uri", name) + separator + suffix


def pyqgis_api(function_name: str, **kwargs) -> str:
    """
    Calls a PyQGIS function for QGIS-specific tasks.
    """
    if function_name in ("QgsVectorLayer", "QgsRasterLayer") and kwargs.get("uri"):
        try:
            kwargs = {**kwargs, "uri": confined_uri(kwargs["uri"])}
        except PathRefused as e:
            return f"❌ '{function_name}' refused a parameter: {e}"

    try:
        session = qgis_session()
    except QgisUnavailable as e:
        return f"❌ QGIS init failed: {e}"

    from qgis.core import QgsVectorLayer, QgsRasterLayer
    import logging

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    try:
        if function_name.startswith("native:") or function_name.startswith("qgis:"):
            if session.processing is None:
                return (
                    f"❌ '{function_name}' needs the QGIS processing module, which is "
                    f"not available ({session.processing_error})."
                )
            result = session.processing.run(function_name, kwargs)
            return str(result)
        elif function_name == "QgsVectorLayer":
            layer = QgsVectorLayer(
                kwargs.get("uri"), kwargs.get("layer_name", "layer"), "ogr"
            )
            if layer.isValid():
                return f"Vector layer loaded: {layer.name()}"
            return f"❌ Invalid vector layer at {kwargs.get('uri')}"
        elif function_name == "QgsRasterLayer":
            layer = QgsRasterLayer(kwargs.get("uri"), kwargs.get("layer_name", "layer"))
            if layer.isValid():
                return f"Raster layer loaded: {layer.name()}"
            return f"❌ Invalid raster layer at {kwargs.get('uri')}"
        else:
            return f"❌ PyQGIS function '{function_name}' not supported."
    except Exception as e:
        logger.error(f"PyQGIS error: {str(e)}")
        return f"❌ Error executing '{function_name}': {str(e)}"


# Required for auto-registration
TOOL_FUNCTION = pyqgis_api
TOOL_SCHEMA = PyQGISArgs
