"""Record what a map session actually DID, so it can be replayed and checked.

WHY OPERATIONS AND NOT KEYSTROKES. Three long investigations in one evening
each ended at the same place: a question about what the deterministic core
was asked and what it answered. Not what was clicked -- what was RUN. So
this records the calls: the spatial lookup and its arguments, the WHERE the
filter built, the query that fetched the wells, and the size and digest of
each answer.

That makes the file two things at once. Read forwards it is a workflow --
"drew a box, asked for wells near lineA.sgy, filtered on depth". Re-executed
it is a REGRESSION TEST: the same arguments against today's code must give
the same digests. An op whose answer changed is exactly the thing worth
being told about, and it is the class of bug that hurts most here, because
a changed well set is silent -- it plots, exports and gets quoted.

STATE IS RECORDED TOO, but as context rather than as the test. Seeding
session_state reproduces the controls; it does not prove anything by
itself.

THE KEY SET IS THIS MODULE'S OWN, DELIBERATELY. The obvious move is to
reuse _map_option_sig(), which already walks session_state and skips frames
and engines. It answers a DIFFERENT question -- "should this change hold
the map?" -- and so it deliberately drops wm_ai_question, wm_ai_scope and
wm_near_*, which are precisely what a workflow is about. It also cannot see
the drawn box, which turned out to be the single most important piece of
state in the session that prompted this. Borrowing it would produce a
capture that looks complete and reproduces a different question: the same
shape as the invariant that grouped by FILE_NAME when the identity was
FILE_PATH.

NOTHING HERE MAY RAISE INTO THE PAGE. A recorder that breaks the thing it
is recording is worse than no recorder. But a failure is not swallowed
either -- it goes to stderr with its reason, because a capture that quietly
records nothing is how you find out tomorrow that there is no file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

# The toggle's widget key. Under wm_ so the sub-page persist loop carries it
# across a trip to Documents and back, like every other map control.
ON_KEY = "wm_record_workflow"

_PATH_KEY = "_wf_capture_path"

# Where reports live. C:\Bulk is staging workspace, never input.
_REPORT_DIR = os.environ.get("DW_REPORT_DIR", r"C:\Bulk\reports")

# What a workflow is made of. Wider than _OPT_PREFIXES on purpose: the AI
# question and the near-feature controls ARE the workflow, and map_mode and
# the lease strip's own keys decide what is on screen.
_STATE_PREFIXES = ("wm_", "h3_", "wells_", "seis_", "lv_", "ai_filter_",
                   "map_")

# Keys that carry no prefix but decide what the map is answering. The drawn
# box is here because its absence is what would make a replay silently ask a
# different question.
_STATE_EXPLICIT = (
    "_active_drill_bbox", "_drawn_bounds", "viewport_uwis",
    "tray_selected_uwis", "_seis_pick", "_lease_gj_sig",
)

# Same rule _map_option_sig uses, and for the same reason: a frame or an
# engine differs between two renders that changed nothing.
_SCALARS = (str, int, float, bool, type(None))

_MAX_LIST = 200          # a captured list longer than this is digested, not stored


def _warn(msg):
    """Say why capture failed. Never raises; never reaches the page."""
    try:
        sys.stderr.write("[workflow_capture] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def digest(values) -> str:
    """A stable fingerprint of a result set, order-independent.

    Stored INSTEAD of the rows. 1,372 uwis is 20 KB a line and the question
    a regression check asks is not "which wells" but "the same wells as
    before" -- and a digest answers that in 16 characters. When it differs,
    the recorded count and arguments are enough to go and look.
    """
    try:
        _items = sorted(str(v) for v in (values or []))
    except Exception:
        return ""
    _h = hashlib.sha1("\x1f".join(_items).encode("utf-8", "replace"))
    return _h.hexdigest()[:16]


def _jsonable(v, _depth=0):
    """Scalars through, short flat lists through, everything else described.

    A frame or an engine becomes "<DataFrame 1372x22>" rather than being
    dropped: knowing something was there and was not captured beats a key
    silently missing from the file.
    """
    if isinstance(v, _SCALARS):
        return v
    if isinstance(v, (list, tuple, set, frozenset)):
        _l = list(v)
        if all(isinstance(x, _SCALARS) for x in _l):
            if len(_l) <= _MAX_LIST:
                return _l
            return {"__n": len(_l), "__digest": digest(_l)}
        return {"__n": len(_l), "__type": type(v).__name__}
    if isinstance(v, dict) and _depth < 3:
        return {str(k): _jsonable(x, _depth + 1) for k, x in list(v.items())[:60]}
    _shape = ""
    try:
        _shape = " %dx%d" % v.shape
    except Exception:
        pass
    return "<%s%s>" % (type(v).__name__, _shape)


def is_on() -> bool:
    """True when the operator has switched recording on for this session."""
    try:
        import streamlit as st
        return bool(st.session_state.get(ON_KEY))
    except Exception:
        return False


def current_path():
    """The file this session is recording into, or None."""
    try:
        import streamlit as st
        return st.session_state.get(_PATH_KEY)
    except Exception:
        return None


def start(label: str = "") -> str:
    """Open a file for this session. Idempotent -- returns the open one.

    NAMED FOR WHEN, not for a session id: the id means nothing to the person
    who has to find the file afterwards and say "this is the one where it
    went wrong".
    """
    try:
        import streamlit as st
        _p = st.session_state.get(_PATH_KEY)
        if _p:
            return _p
        os.makedirs(_REPORT_DIR, exist_ok=True)
        _name = "workflow_%s%s.jsonl" % (
            time.strftime("%Y%m%d_%H%M%S"),
            ("_" + "".join(c if c.isalnum() else "_" for c in label)[:24])
            if label else "")
        _p = os.path.join(_REPORT_DIR, _name)
        st.session_state[_PATH_KEY] = _p
        _write(_p, {"kind": "start", "label": label,
                    "python": sys.version.split()[0]})
        return _p
    except Exception as _e:
        _warn("could not start: %s" % _e)
        return ""


def stop():
    """Close the file. The next start() opens a new one."""
    try:
        import streamlit as st
        _p = st.session_state.pop(_PATH_KEY, None)
        if _p:
            _write(_p, {"kind": "stop"})
        return _p
    except Exception as _e:
        _warn("could not stop: %s" % _e)
        return None


def _write(path, obj):
    """One JSON object per line, appended. Never raises."""
    if not path:
        return
    try:
        obj = dict(obj)
        obj.setdefault("t", time.strftime("%H:%M:%S"))
        with open(path, "a", encoding="utf-8") as _fh:
            _fh.write(json.dumps(obj, default=str) + "\n")
    except Exception as _e:
        _warn("write failed (%s): %s" % (path, _e))


def snapshot() -> dict:
    """The session state a workflow is made of. Context, not the test."""
    _out = {}
    try:
        import streamlit as st
        for _k, _v in list(st.session_state.items()):
            if not isinstance(_k, str):
                continue
            if not (_k.startswith(_STATE_PREFIXES) or _k in _STATE_EXPLICIT):
                continue
            if _k.startswith("FormSubmitter:"):
                continue
            _out[_k] = _jsonable(_v)
    except Exception as _e:
        _warn("snapshot failed: %s" % _e)
    return _out


def record_op(name: str, args: dict, result=None, n=None, replayable=False):
    """One call the deterministic core made, and what it answered.

    `replayable` says whether tools/replay_workflow.py can re-execute this
    from the recorded arguments alone. A call whose arguments include the
    loaded well rows cannot be, and claiming otherwise would make the
    regression check quietly skip it -- so it is recorded as an observation
    and labelled as one.
    """
    if not is_on():
        return
    _p = current_path()
    if not _p:
        return
    _rec = {"kind": "op", "name": name,
            "args": {str(k): _jsonable(v) for k, v in (args or {}).items()},
            "replayable": bool(replayable)}
    if n is not None:
        _rec["n"] = int(n)
    if result is not None:
        _rec["n"] = len(result) if n is None else _rec["n"]
        _rec["digest"] = digest(result)
    _write(_p, _rec)


def record_render(tag: str = "", **extra):
    """A render boundary, with the state that produced it."""
    if not is_on():
        return
    _p = current_path()
    if not _p:
        return
    _write(_p, {"kind": "render", "tag": tag,
                "state": snapshot(),
                **{k: _jsonable(v) for k, v in extra.items()}})


def record_note(text: str, **extra):
    """Anything worth reading in sequence that is not an op or a render."""
    if not is_on():
        return
    _p = current_path()
    if not _p:
        return
    _write(_p, {"kind": "note", "text": str(text)[:400],
                **{k: _jsonable(v) for k, v in extra.items()}})
