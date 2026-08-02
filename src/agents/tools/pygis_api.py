from pydantic import BaseModel, Field
from typing import Optional


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


def pyqgis_api(function_name: str, **kwargs) -> str:
    """
    Calls a PyQGIS function for QGIS-specific tasks.
    """
    import os

    try:
        from qgis.core import QgsApplication

        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["QGIS_PREFIX_PATH"] = "/usr"
        QgsApplication.setPrefixPath("/usr", True)
        qgs = QgsApplication([], False)
        qgs.initQgis()
    except Exception as e:
        return f"QGIS init failed: {str(e)}"

    from qgis.core import QgsVectorLayer, QgsRasterLayer
    import logging

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    try:
        if function_name.startswith("native:") or function_name.startswith("qgis:"):
            from qgis import processing

            result = processing.run(function_name, kwargs)
            return str(result)
        elif function_name == "QgsVectorLayer":
            layer = QgsVectorLayer(
                kwargs.get("uri"), kwargs.get("layer_name", "layer"), "ogr"
            )
            if layer.isValid():
                return f"Vector layer loaded: {layer.name()}"
            return f"Error: Invalid vector layer at {kwargs.get('uri')}"
        elif function_name == "QgsRasterLayer":
            layer = QgsRasterLayer(kwargs.get("uri"), kwargs.get("layer_name", "layer"))
            if layer.isValid():
                return f"Raster layer loaded: {layer.name()}"
            return f"Error: Invalid raster layer at {kwargs.get('uri')}"
        else:
            return f"Error: PyQGIS function '{function_name}' not supported."
    except Exception as e:
        logger.error(f"PyQGIS error: {str(e)}")
        return f"Error executing '{function_name}': {str(e)}"


# Required for auto-registration
TOOL_FUNCTION = pyqgis_api
TOOL_SCHEMA = PyQGISArgs
