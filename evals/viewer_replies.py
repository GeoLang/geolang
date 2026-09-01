"""What the viewer sends back to the model after it ran a viewer_control call.

The live viewer answers a run call: one that it refuses arrives as
"<name> failed: <message>", and a [reads] action's result as
"Result of <name>: <text>". Both go back as the next user message, so the model
is scored on a conversation rather than on one shot. Mirrors viewtopia's
`runViewerAction`, down to the message text, and holds one pending reply per
turn the way the chat store's single follow-up slot does.

The result texts are a fixture, evals/viewer/reads_results.json, since nothing
here runs the viewer's own actions.
"""

from evals.viewer_scoring import RUN_ACTION, call_action, read_arguments

# viewtopia's MAXIMUM_FOLLOW_UPS: two keeps a read whose answer needs another
# read from looping
MAXIMUM_FOLLOW_UPS = 2


def _failure(name: str, message: str) -> str:
    return f"{name} failed: {message}"


def _required(entry: dict) -> list:
    parameters = entry.get("parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    return [str(key) for key in parameters.get("required") or []]


def reply_for_call(call: dict, catalogue: list, reads_results: dict):
    """The message one call queues, or None when the viewer says nothing.

    A destructive action says nothing either: it waits for the user to confirm
    it in the chat, and the model is not told about that.
    """
    if call_action(call) != RUN_ACTION:
        return None
    name = str(call.get("name") or "")
    arguments = read_arguments(call.get("args"))
    if arguments is None:
        return _failure(name, f"{name}: its arguments did not read as an object.")
    entry = next((e for e in catalogue if e.get("name") == name), None)
    if entry is None:
        return _failure(name, f"There is no viewer action named {name}.")
    if entry.get("destructive"):
        return None
    missing = [key for key in _required(entry) if arguments.get(key) is None]
    if missing:
        problems = ", ".join(f"{key} is required" for key in missing)
        return _failure(name, f"{name}: {problems}")
    if not entry.get("reads"):
        return None
    text = reads_results.get(name)
    if not text:
        raise ValueError(f"reads_results.json has no result text for {name}")
    return f"Result of {name}: {text}"


def pending_reply(calls: list, catalogue: list, reads_results: dict):
    """The one reply a turn's calls leave waiting, or None when they leave none.

    The viewer keeps a single follow-up slot, so a turn that queued twice sends
    only what it queued last.
    """
    replies = [reply_for_call(call, catalogue, reads_results) for call in calls]
    return next((reply for reply in reversed(replies) if reply is not None), None)
