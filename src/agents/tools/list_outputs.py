from pydantic import BaseModel
from src.core.utils import caller_outputs_dir


class ListOutputsArgs(BaseModel):
    pass  # no arguments needed


def list_outputs() -> str:
    """
    List all files in the outputs directory. Call this when the user refers to
    a previous result, asks to 'use the file from before', or when you need to
    find a file path from an earlier step in the workflow.
    """
    import os

    outputs_dir = caller_outputs_dir()

    if not os.path.exists(outputs_dir):
        return "No outputs directory found. No files have been created yet."

    files = []
    for fname in sorted(os.listdir(outputs_dir)):
        fpath = os.path.join(outputs_dir, fname)
        if os.path.isfile(fpath):
            size_kb = os.path.getsize(fpath) / 1024
            files.append(f"  outputs/{fname} ({size_kb:.0f} KB)")

    if not files:
        return "No output files found."

    return f"Output files ({len(files)}):\n" + "\n".join(files)


TOOL_FUNCTION = list_outputs
TOOL_SCHEMA = ListOutputsArgs
