"""Standalone, fake-only proof that ``tests/test_csp_nonce.py::_make_client`` redirects the three host
persistence paths under a per-invocation temporary directory BEFORE any dependency or app factory runs.

Containment: this proof compiles ONLY the ``_make_client`` function definition (parsed from the test
source) into an isolated namespace whose imports resolve through a fail-closed fake allowlist — a
non-allowlisted import raises, never falling back to the real importer. It never executes the test
module's top level and never mutates ``sys.modules``. Everything the compiled function touches is a fake
(fake security modules, fake dependency constructors, a fake app factory, a fake monkeypatch that records
into a private dict) plus temporary directories. It does NOT import production code, call any production
resolver/predicate, construct the real app, send an auth/application request, load pytest/conftest, unset
an override to inspect a default, reference the host home directory, or run ``test_csp_nonce``. It proves
the SETUP isolation only — not that live authentication or the CSP behave correctly, and not that any
host state was repaired.

Run directly (no pytest):
    python tests/test_csp_nonce_isolation.py
Prints a report; exit status 0 means every check passed.
"""
from __future__ import annotations

import ast
import shutil
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE / "test_csp_nonce.py"

# A synthetic pre-redirect marker. The proof asserts every seam is overwritten to an EXACT temp path;
# it is never resolved or opened on the filesystem, and no real host path is referenced anywhere.
_SENTINEL = Path("__UNREDIRECTED_SEAM__")

# The seams _make_client must redirect, as (fake module attribute owner, attribute) plus the env var,
# and the exact temp path each must hold. Keyed for readable assertions.
_SEAM_ATTRS = {
    "physical_key._CONFIG_DIR": ("physical_key", "_CONFIG_DIR"),
    "web_auth._CONFIG_DIR": ("web_auth", "_CONFIG_DIR"),
    "web_auth._WEB_AUTH_FILE": ("web_auth", "_WEB_AUTH_FILE"),
    "web_auth._SECRET_KEY_FILE": ("web_auth", "_SECRET_KEY_FILE"),
}
# Every dependency/factory construction _make_client performs, in order — all must be snapshotted.
_EXPECTED_LABELS = ["DeviceManager", "FlashEngine", "EventBus", "TargetPool", "create_app"]
# Dotted names _make_client is allowed to import; anything else is rejected fail-closed.
_ALLOW = frozenset([
    "src.security.physical_key", "src.security.web_auth",
    "src.core.cross_comm", "src.core.device_manager", "src.core.flash_engine", "src.ui.web.app",
])


def _expected(tmp):
    """The exact redirected values _make_client must produce for a given temp dir."""
    return {
        "CC_GATE_CONFIG": str(tmp / "access_gate.json"),
        "physical_key._CONFIG_DIR": tmp,
        "web_auth._CONFIG_DIR": tmp,
        "web_auth._WEB_AUTH_FILE": tmp / "web_auth.json",
        "web_auth._SECRET_KEY_FILE": tmp / "web_secret.key",
    }


class _NS:
    """A minimal fake module/namespace; unknown attribute access fails closed (AttributeError)."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class _FakeMonkeyPatch:
    """Records env + attribute redirections into private state; never touches real os.environ."""

    def __init__(self):
        self.env = {}

    def setenv(self, key, value):
        self.env[str(key)] = str(value)

    def setattr(self, target, name, value):
        if not hasattr(target, name):  # mirror pytest raising=True: an unexpected seam fails closed
            raise AssertionError("setattr target has no attribute %r (unexpected seam)" % name)
        setattr(target, name, value)


class _Recorder:
    """Snapshots the seam state at each fake construction so EVERY snapshot can be validated."""

    def __init__(self, mp, modules):
        self.mp = mp
        self.modules = modules  # {"physical_key": _NS, "web_auth": _NS}
        self.snapshots = []  # (label, gate_env, {seam_key: value})

    def snap(self, label):
        attrs = {}
        for seam_key, (mod_name, attr) in _SEAM_ATTRS.items():
            attrs[seam_key] = getattr(self.modules[mod_name], attr)
        self.snapshots.append((label, self.mp.env.get("CC_GATE_CONFIG"), attrs))


def _build_env():
    """Build a fresh fake package tree + fail-closed importer + a recorder. Nothing enters sys.modules."""
    physical_key = _NS(_CONFIG_DIR=_SENTINEL)
    web_auth = _NS(_CONFIG_DIR=_SENTINEL, _WEB_AUTH_FILE=_SENTINEL, _SECRET_KEY_FILE=_SENTINEL)
    mp = _FakeMonkeyPatch()
    recorder = _Recorder(mp, {"physical_key": physical_key, "web_auth": web_auth})

    def dep(label):
        class _Dep:
            def __init__(self, *args, **kwargs):
                recorder.snap(label)
        return _Dep

    class _FakeApp:
        def __init__(self):
            self.config = {}
            self.secret_key = None

        def test_client(self):
            return object()  # inert; never used to send a request

    def create_app(*args, **kwargs):
        recorder.snap("create_app")
        return _FakeApp(), object()

    cross_comm = _NS(EventBus=dep("EventBus"), TargetPool=dep("TargetPool"))
    device_manager = _NS(DeviceManager=dep("DeviceManager"))
    flash_engine = _NS(FlashEngine=dep("FlashEngine"))
    app_mod = _NS(create_app=create_app)
    leaves = {
        "src.security.physical_key": physical_key,
        "src.security.web_auth": web_auth,
        "src.core.cross_comm": cross_comm,
        "src.core.device_manager": device_manager,
        "src.core.flash_engine": flash_engine,
        "src.ui.web.app": app_mod,
    }
    # Walkable package tree for `import src.x.y as z` (fromlist empty -> returns top, attrs are traversed).
    src_top = _NS(
        security=_NS(physical_key=physical_key, web_auth=web_auth),
        core=_NS(cross_comm=cross_comm, device_manager=device_manager, flash_engine=flash_engine),
        ui=_NS(web=_NS(app=app_mod)),
    )

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0 or name not in _ALLOW:
            raise ImportError("blocked non-allowlisted import %r" % name)
        if fromlist:
            return leaves[name]
        return src_top if name.split(".")[0] == "src" else leaves[name]

    return {"physical_key": physical_key, "web_auth": web_auth, "mp": mp,
            "recorder": recorder, "fake_import": fake_import}


def _compile_make_client_only():
    """Parse the test source and compile ONLY the plain _make_client FunctionDef (no module top level)."""
    tree = ast.parse(_TARGET.read_text(encoding="utf-8"))
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_make_client"), None)
    if fn is None:
        raise AssertionError("_make_client FunctionDef not found in test source")
    if fn.decorator_list:
        raise AssertionError("_make_client unexpectedly carries decorators")
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    return compile(module, "<make_client-only>", "exec")


def _define_make_client(code, env):
    """Define _make_client in an isolated namespace with minimal builtins + the fail-closed importer.
    A harmless sentinel is seeded to confirm the module top level is never executed."""
    ns = {"__builtins__": {"__import__": env["fake_import"], "str": str}, "__TOP_LEVEL_MARKER__": "intact"}
    exec(code, ns)  # defines _make_client only; the original module's top level never runs
    return ns


def _validate_run(env, tmp):
    """Every snapshot must equal the exact expected redirected paths + env; the full label set must appear."""
    recorder = env["recorder"]
    want = _expected(tmp)
    problems = []
    labels = [label for label, _g, _a in recorder.snapshots]
    if labels != _EXPECTED_LABELS:
        problems.append("construction labels %r != expected %r" % (labels, _EXPECTED_LABELS))
    for label, gate, attrs in recorder.snapshots:
        if gate != want["CC_GATE_CONFIG"]:
            problems.append("[%s] CC_GATE_CONFIG=%r != %r" % (label, gate, want["CC_GATE_CONFIG"]))
        for seam_key, value in attrs.items():
            if value != want[seam_key]:
                problems.append("[%s] %s=%r != %r" % (label, seam_key, value, want[seam_key]))
    return problems


def _tmpdir():
    return Path(tempfile.mkdtemp(prefix="csp-iso-"))


def _positive(code, report):
    """Two isolated runs: every snapshot exact, and distinct temp dirs give distinct paths."""
    ok = True
    control_a_ok = True
    runs = []
    tmps = []
    try:
        for _ in range(2):
            tmp = _tmpdir()
            tmps.append(tmp)
            env = _build_env()
            ns = _define_make_client(code, env)
            # negative control (a): only the function was compiled — the module top level never ran, so
            # its top-level-only names are absent and the seeded sentinel is untouched.
            if "_script_src" in ns or any(k.startswith("test_") for k in ns):
                control_a_ok = False
                report.append("control-a top-level-not-executed: FAIL (module top level leaked into ns)")
            elif ns.get("__TOP_LEVEL_MARKER__") != "intact":
                control_a_ok = False
                report.append("control-a top-level-not-executed: FAIL (sentinel disturbed)")
            ns["_make_client"](env["mp"], tmp)
            problems = _validate_run(env, tmp)
            if problems:
                ok = False
                report.append("positive run FAIL: " + "; ".join(problems))
            else:
                report.append("positive run OK: all %d snapshots (%s) exact under %s"
                              % (len(env["recorder"].snapshots),
                                 ",".join(_EXPECTED_LABELS), tmp.name))
            runs.append(env)
        if control_a_ok:
            report.append("control-a top-level-not-executed: OK "
                          "(only _make_client compiled; top-level names absent, sentinel intact)")
        ok = ok and control_a_ok
        # distinct per-invocation: the two runs' final snapshot values must differ
        a = runs[0]["recorder"].snapshots[-1][2]
        b = runs[1]["recorder"].snapshots[-1][2]
        distinct = all(a[k] != b[k] for k in a) and (
            runs[0]["mp"].env["CC_GATE_CONFIG"] != runs[1]["mp"].env["CC_GATE_CONFIG"])
        report.append("distinct-per-invocation: %s" % ("OK" if distinct else "FAIL"))
        ok = ok and distinct
    finally:
        for tmp in tmps:
            shutil.rmtree(tmp, ignore_errors=True)
    return ok


def _control_rejects_unknown_import(report):
    """Negative control (b): a non-allowlisted import is rejected before execution, no real fallback."""
    env = _build_env()
    ok = True
    for bad in ("os", "src.ui.web.app.evil", "totally.made.up"):
        try:
            env["fake_import"](bad, fromlist=("x",))
            ok = False
            report.append("control-b reject-unknown-import: FAIL (%r was NOT rejected)" % bad)
        except ImportError:
            pass
    # and an allowlisted one still resolves to a fake (sanity that the allowlist is not empty-by-accident)
    if env["fake_import"]("src.ui.web.app", fromlist=("create_app",)) is None:
        ok = False
        report.append("control-b reject-unknown-import: FAIL (allowlisted import did not resolve)")
    if ok:
        report.append("control-b reject-unknown-import: OK (os / unknown names blocked; allowlisted ok)")
    return ok


def _control_late_redirection_caught(report):
    """Negative control (c): a seam that is correct at the first construction but reverts by a LATER one
    must be caught. A first-snapshot-only check would pass; validating EVERY snapshot fails it."""
    tmp = _tmpdir()
    try:
        want = _expected(tmp)
        good = {k: want[k] for k in _SEAM_ATTRS}
        reverted = dict(good)
        reverted["web_auth._SECRET_KEY_FILE"] = _SENTINEL  # a late regression on one seam
        gate = want["CC_GATE_CONFIG"]

        class _Fake:
            def __init__(self, snaps):
                self.snapshots = snaps

        # Full, correctly-ordered snapshots where ONLY the final (create_app) seam reverts late.
        good_snaps = [(label, gate, good) for label in _EXPECTED_LABELS[:-1]]
        first_only = _Fake(list(good_snaps))
        all_snaps = _Fake(good_snaps + [("create_app", gate, reverted)])

        env_first = {"recorder": first_only}
        env_all = {"recorder": all_snaps}
        # A validator that only inspects the first snapshot MISSES it; ours checks all snapshots and flags it.
        first_only_problems = _validate_first_only(env_first, tmp)
        all_problems = _validate_run(env_all, tmp)
        ok = (not first_only_problems) and bool(all_problems)
        if ok:
            report.append("control-c late-redirection-caught: OK "
                          "(first-snapshot-only passes; all-snapshot check flags the late revert)")
        else:
            report.append("control-c late-redirection-caught: FAIL "
                          "(first_only=%r all=%r)" % (first_only_problems, all_problems))
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _validate_first_only(env, tmp):
    """A deliberately weak checker (first snapshot only) used purely to contrast control (c)."""
    want = _expected(tmp)
    label, gate, attrs = env["recorder"].snapshots[0]
    problems = []
    if gate != want["CC_GATE_CONFIG"]:
        problems.append("[%s] CC_GATE_CONFIG" % label)
    for seam_key, value in attrs.items():
        if value != want[seam_key]:
            problems.append("[%s] %s" % (label, seam_key))
    return problems


def main():
    report = []
    code = _compile_make_client_only()
    ok = _positive(code, report)
    ok = _control_rejects_unknown_import(report) and ok
    ok = _control_late_redirection_caught(report) and ok
    print("\n".join(report))
    print("RESULT:", "PASS" if ok else "FAIL")
    print("LIMIT: fake-only SETUP proof; NOT a live auth/CSP functional test and NOT proof of host-state repair.")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
