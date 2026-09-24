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
    "To change the viewer, call the viewer_control tool with action set to 'run', "
    "name set to one of the names above, and that action's parameters as further "
    "fields of the same call, spelled as listed. A name with a dot in it, like "
    "layers.set_visible, is a viewer action and never a tool of its own: it only "
    "runs through viewer_control. Call viewer_control and every other tool as a "
    "tool call, never written out in the reply as text or JSON. "
    "When the viewer state says mode is chat, the person sees the map and this "
    "chat and no toolbar, buttons or menus, so never tell them to click or open "
    "anything: run the action yourself. "
    "Unique values, categorized, choropleth and graduated colouring of a layer on "
    "the map are all layers.shade_by with the column to colour by. "
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


def superseding_actions(state) -> dict:
    """The agent tools a run with this catalogue does without, each with the actions replacing it.

    A tool names the viewer actions that do its job on the map, and a model
    given both takes the tool, whatever the prompt says. With the action
    offered, the tool is not.
    """
    offered = {entry["name"] for entry in catalogue_of(state)}
    superseding = {}
    for func, _ in load_external_tools():
        actions = [action for action in superseded_by(func) if action in offered]
        if actions:
            superseding[func.__name__] = actions
    return superseding


def run_fields(state) -> dict:
    superseding = superseding_actions(state)
    fields = {"system_prompt": system_prompt_for(state, superseding)}
    if superseding:
        fields["without_tools"] = list(superseding)
    return fields


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


def hidden_tool_line(tool: str, actions: list) -> str:
    return (
        f"{tool} is not offered here: where a rule above names it, run "
        f"{' or '.join(actions)} with viewer_control instead."
    )


def system_prompt_for(state, superseding: dict | None = None) -> str:
    """PERSONA, plus what the viewer looks like and what it can be told to do."""
    catalogue = catalogue_of(state)
    if not catalogue:
        return PERSONA

    viewer = state.get("viewer")
    viewer = viewer if isinstance(viewer, dict) else {}
    snapshot = json.dumps(viewer, separators=(",", ":"))
    lines = "\n".join(action_line(entry) for entry in catalogue)
    prompt = (
        f"{PERSONA}\n\n"
        f"{STATE_HEADING}\n{snapshot}\n\n"
        f"{ACTIONS_HEADING}\n{lines}\n\n"
        f"{INSTRUCTIONS}"
    )
    hidden = [hidden_tool_line(tool, actions) for tool, actions in (superseding or {}).items()]
    if not hidden:
        return prompt
    return prompt + "\n\n" + "\n".join(hidden)
