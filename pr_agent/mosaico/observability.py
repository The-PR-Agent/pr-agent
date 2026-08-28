"""MOSAICO observability helpers: parse the observability-extension metadata,
bind correlation IDs into loguru, and open a Langfuse trace span.

Mirrors docstring-agent / mosaico-base-agents conventions, with one deliberate
choice: parse_observability_metadata is TOLERANT (returns a partial dict, never
raises) so partial/absent metadata degrades gracefully rather than failing the
A2A request."""
import contextlib
from collections.abc import Mapping

from pr_agent.log import get_logger

AGENT_NAME = "PR-Agent Solution Agent"

_KEYS = ("mosaico-root-task-id", "mosaico-super-task-id", "mosaico-root-task-name")


def parse_observability_metadata(raw) -> dict:
    """Tolerant partial-dict parse (mirrors docstring-agent/mosaico_utils.py).

    Returns a dict containing ONLY the present, string-valued keys among _KEYS.
    A non-Mapping ``raw`` yields ``{}`` (with a logged warning). Never raises."""
    if not isinstance(raw, Mapping):
        if raw is not None:
            get_logger().warning(
                f"MOSAICO: observability metadata is not a mapping (got {type(raw).__name__}); ignoring.")
        return {}
    return {k: raw[k] for k in _KEYS if k in raw and isinstance(raw[k], str)}


@contextlib.contextmanager
def mosaico_log_context(meta, context_id):
    """Bind the MOSAICO correlation IDs into loguru for the duration of the block.

    Log correlation only - does not feed the litellm Langfuse callback (which traces
    independently from env). ``meta={}`` is a clean pass-through."""
    bindings = {}
    if meta:
        for k in _KEYS:
            if meta.get(k):
                bindings[k] = meta[k]
    if context_id:
        bindings["context_id"] = context_id
    if not bindings:
        yield
        return
    with get_logger().contextualize(**bindings):
        yield


@contextlib.contextmanager
def langfuse_span(meta, context_id):
    """Open a Langfuse span linking this run into the MOSAICO trace.

    Applies the W3C Trace Context transform (spec §observability lines 47-51):
    mosaico-root-task-id with the four UUIDv4 hyphens removed -> trace_id (32 hex),
    last 16 hex digits of the de-hyphenated mosaico-super-task-id -> parent observation
    (parent_span_id, 16 hex), context_id -> session, mosaico-root-task-name -> trace_name.
    All wrapped in try/except -> degrade to untraced. No-op when meta is empty."""
    if not meta:
        yield
        return
    try:
        from langfuse import get_client, propagate_attributes
        lf = get_client()
        trace_ctx = {}
        if meta.get("mosaico-root-task-id"):
            # W3C trace-id: 32 lowercase hex digits, no hyphens (spec §observability lines 47-48).
            trace_ctx["trace_id"] = meta["mosaico-root-task-id"].replace("-", "")
        if meta.get("mosaico-super-task-id"):
            # W3C parent-id: last 16 hex digits (spec §observability lines 49-51).
            trace_ctx["parent_span_id"] = meta["mosaico-super-task-id"].replace("-", "")[-16:]
        with propagate_attributes(session_id=context_id, trace_name=meta.get("mosaico-root-task-name")):
            with lf.start_as_current_observation(as_type="span", name=AGENT_NAME,
                                                 **({"trace_context": trace_ctx} if trace_ctx else {})):
                yield
    except Exception as e:
        get_logger().warning(f"MOSAICO: Langfuse span setup failed: {e}")
        yield
