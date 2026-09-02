"""The system prompt for a run started from the viewer.

The viewer sends its own state and the catalogue of actions it can run in the
AG-UI `state` field, so what the model may reach is whatever that viewer offered
on this run rather than a list kept here. No catalogue means the plain persona.
"""

import json

from src.agents.agent_manager import PERSONA, load_external_tools, superseded_by

STATE_HEADING = "Viewer state:"
ACTIONS_HEADING = "Viewer actions:"
READS_MARKER = "[reads]"
DESTRUCTIVE_MARKER = "[asks to confirm]"
# a parameter that declares no type in the catalogue
DEFAULT_PARAMETER_TYPE = "string"
# parameter descriptions sit under their action line
PARAMETER_INDENT = "  "

INSTRUCTIONS = (
    "To change the viewer, call viewer_control with action='run', name set to one "
    "of the names above, and that action's parameters as further fields of the "
    "same call, spelled as listed, for example viewer_control(action='run', "
    "name='layers.set_visible', layer='Parcels', visible=false). "
    "For layers, projects, documents and feeds use the ids or names from the "
    f"viewer state. A {READS_MARKER} action answers a question: its answer arrives "
    "as the next user message beginning 'Result of <name>:', so carry on from "
    "there. When the person names a feature on the map that the viewer state does "
    "not locate, run find_feature first and only then the action that needs its "
    "coordinates. For 'where is X' and for any named thing that is not a town, "
    "a city or a street address, find_feature comes first and geocode_place only "
    "when it finds nothing. A listed action that does what the person asked is "
    "called before any other tool, whatever a rule above says about that kind "
    "of request: those rules are for what no listed action covers."
)


def hidden_tools(state) -> list:
    """The agent tools a run with this catalogue does without.

    A tool names the viewer actions that do its job on the map, and a model
    given both takes the tool, whatever the prompt says. With the action
    offered, the tool is not.
    """
    offered = {entry["name"] for entry in catalogue_of(state)}
    return [
        func.__name__
        for func, _ in load_external_tools()
        if offered.intersection(superseded_by(func))
    ]


def catalogue_of(state) -> list:
    """The actions the viewer offered, or [] when it offered none."""
    if not isinstance(state, dict):
        return []
    actions = state.get("actions")
    if not isinstance(actions, list):
        return []
    return [entry for entry in actions if isinstance(entry, dict) and entry.get("name")]


def parameter_text(name: str, schema, required: bool) -> str:
    schema = schema if isinstance(schema, dict) else {}
    values = schema.get("enum")
    if isinstance(values, list) and values:
        kind = "|".join(str(v) for v in values)
    else:
        kind = str(schema.get("type") or DEFAULT_PARAMETER_TYPE)
    return f"{name}{'' if required else '?'}: {kind}"


def action_line(entry: dict) -> str:
    parameters = entry.get("parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    properties = parameters.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = set(parameters.get("required") or [])
    signature = ", ".join(
        parameter_text(name, schema, name in required)
        for name, schema in properties.items()
    )
    line = f"{entry['name']}({signature})"
    description = str(entry.get("description") or "").strip()
    if description:
        line += f": {description}"
    if entry.get("reads"):
        line += f" {READS_MARKER}"
    if entry.get("destructive"):
        line += f" {DESTRUCTIVE_MARKER}"
    described = [
        f"{PARAMETER_INDENT}{name}: {str(schema.get('description')).strip()}"
        for name, schema in properties.items()
        if isinstance(schema, dict) and str(schema.get("description") or "").strip()
    ]
    return "\n".join([line, *described])


def system_prompt_for(state) -> str:
    """PERSONA, plus what the viewer looks like and what it can be told to do."""
    catalogue = catalogue_of(state)
    if not catalogue:
        return PERSONA

    viewer = state.get("viewer")
    viewer = viewer if isinstance(viewer, dict) else {}
    snapshot = json.dumps(viewer, separators=(",", ":"))
    lines = "\n".join(action_line(entry) for entry in catalogue)
    return (
        f"{PERSONA}\n\n"
        f"{STATE_HEADING}\n{snapshot}\n\n"
        f"{ACTIONS_HEADING}\n{lines}\n\n"
        f"{INSTRUCTIONS}"
    )
