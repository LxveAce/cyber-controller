"""Standalone, fake-only proof for the SW-gate subprocess diagnostics helpers in tests/test_web_pwa.py.

Containment: this compiles ONLY the two constants and the two plain helper functions (`_sw_gate_diagnostic`,
`_run_sw_gate_harness`), parsed from the test source, into an isolated namespace whose `subprocess` and `time`
are FAKES and whose builtins are minimal. It never imports/collects `test_web_pwa.py`, never loads pytest or
conftest, never spawns a real child process, and never touches app/auth. It proves: a successful run returns
the child STDOUT unchanged; timeout / nonzero-exit / malformed-output produce bounded, kind-specific
diagnostics that preserve the original cause; the diagnostic is length-bounded; and a diagnostic that hits an
internal error still surfaces the original failure kind (never masks it).

Run directly (no pytest):
    python tests/test_sw_timeout_diagnostics.py
Prints a report; exit status 0 means every check passed.
"""
from __future__ import annotations

import ast
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE / "test_web_pwa.py"
_WANTED = ("_DIAG_MAX_STDERR_LINES", "_DIAG_MAX_LINE_CHARS", "_DIAG_MAX_STDERR_CHARS", "_DIAG_MAX_CHARS",
           "_DIAG_MAX_TOTAL_CHARS", "_diag_decode", "_sw_gate_diagnostic", "_run_sw_gate_harness")


class _FakeTimeoutExpired(Exception):
    def __init__(self, stdout=None, stderr=None):
        super().__init__("fake timeout")
        self.stdout = stdout
        self.stderr = stderr


class _FakeCalledProcessError(Exception):
    def __init__(self, returncode, cmd, output=None, stderr=None):
        super().__init__("fake nonzero exit %r" % returncode)
        self.returncode = returncode
        self.cmd = cmd
        self.output = output
        self.stderr = stderr


class _Completed:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSubprocess:
    """Stands in for the `subprocess` module: only `run` and `TimeoutExpired` are modelled; anything else
    fails closed (AttributeError). No real process is ever spawned."""

    TimeoutExpired = _FakeTimeoutExpired
    CalledProcessError = _FakeCalledProcessError

    def __init__(self, behavior):
        self._b = behavior
        self.calls = []

    def run(self, args, capture_output=False, text=False, timeout=None):
        self.calls.append({"args": args, "capture_output": capture_output, "text": text, "timeout": timeout})
        if self._b["mode"] == "timeout":
            raise _FakeTimeoutExpired(self._b.get("stdout"), self._b.get("stderr"))
        return _Completed(self._b["returncode"], self._b.get("stdout", ""), self._b.get("stderr", ""))


class _FakeTime:
    """Deterministic monotonic clock: successive calls return a fixed increasing sequence."""

    def __init__(self, seq):
        self._seq = list(seq)
        self._i = 0

    def monotonic(self):
        value = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return value


def _load_helpers(subprocess_fake, time_fake):
    """Compile ONLY the wanted top-level nodes into an isolated namespace with fakes + minimal builtins."""
    tree = ast.parse(_TARGET.read_text(encoding="utf-8"))
    picked = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED:
            if node.decorator_list:
                raise AssertionError("%s unexpectedly carries decorators" % node.name)
            picked.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _WANTED for t in node.targets):
            picked.append(node)
    names = [n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in picked]
    for wanted in _WANTED:
        if wanted not in names:
            raise AssertionError("did not find %r in the test source" % wanted)
    module = ast.Module(body=picked, type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {
        "__builtins__": {"AssertionError": AssertionError, "Exception": Exception, "str": str, "len": len,
                         "isinstance": isinstance, "bytes": bytes, "bytearray": bytearray},
        "subprocess": subprocess_fake,
        "time": time_fake,
    }
    exec(compile(module, "<sw-diag-helpers>", "exec"), ns)  # defines constants + the two helpers only
    return ns


def _check(report, label, cond):
    report.append("%s: %s" % (label, "OK" if cond else "FAIL"))
    return cond


def main():
    report = []
    ok = True
    json_line = '[{"name": "css", "got": true, "want": true}]\n'

    # success: STDOUT returned unchanged; no exception; subprocess.run called with the 30s deadline + capture.
    sp = _FakeSubprocess({"mode": "run", "returncode": 0, "stdout": json_line, "stderr": "irrelevant markers"})
    ns = _load_helpers(sp, _FakeTime([100.0, 100.02]))
    got = ns["_run_sw_gate_harness"]("fake-node", "fake.js")
    ok = _check(report, "success stdout returned byte-identical", got == json_line) and ok
    ok = _check(report, "success passes 30s timeout + capture to subprocess.run",
                sp.calls and sp.calls[0]["timeout"] == 30 and sp.calls[0]["capture_output"] is True) and ok

    # timeout: raises AssertionError; kind-specific bounded diagnostic; ORIGINAL cause preserved (__cause__).
    sp = _FakeSubprocess({"mode": "timeout", "stdout": "partial", "stderr": "[sw-gate-harness] load:begin +1.0ms"})
    ns = _load_helpers(sp, _FakeTime([200.0, 230.0]))
    try:
        ns["_run_sw_gate_harness"]("fake-node", "fake.js")
        ok = _check(report, "timeout raises", False) and ok
    except AssertionError as exc:
        msg = str(exc)
        ok = _check(report, "timeout diagnostic names 'timeout'", "timeout" in msg) and ok
        ok = _check(report, "timeout diagnostic reports within-process elapsed",
                    "elapsed=" in msg and "monotonic" in msg) and ok
        ok = _check(report, "timeout preserves original cause (__cause__ is TimeoutExpired)",
                    isinstance(exc.__cause__, _FakeTimeoutExpired)) and ok
        ok = _check(report, "timeout surfaces a stderr phase marker", "load:begin" in msg) and ok

    # nonzero-exit: distinct kind + the returncode.
    sp = _FakeSubprocess({"mode": "run", "returncode": 2, "stdout": "", "stderr": "[sw-gate-harness] start +0.0ms"})
    ns = _load_helpers(sp, _FakeTime([0.0, 0.5]))
    try:
        ns["_run_sw_gate_harness"]("fake-node", "fake.js")
        ok = _check(report, "nonzero-exit raises", False) and ok
    except AssertionError as exc:
        ok = _check(report, "nonzero-exit diagnostic distinct + carries returncode",
                    "nonzero-exit" in str(exc) and "returncode=2" in str(exc)) and ok
        ok = _check(report, "nonzero-exit preserves explicit cause (CalledProcessError w/ returncode)",
                    isinstance(exc.__cause__, _FakeCalledProcessError) and exc.__cause__.returncode == 2) and ok

    # malformed-output: the diagnostic helper alone yields a distinct, bounded, kind-named string.
    ns = _load_helpers(_FakeSubprocess({"mode": "run", "returncode": 0}), _FakeTime([0.0]))
    malformed = ns["_sw_gate_diagnostic"]("malformed-output", None, "<not json>", "", parse_error="x")
    ok = _check(report, "malformed-output diagnostic distinct", "malformed-output" in malformed) and ok

    # bounded (finding 1) — every shape must yield a report <= the final total cap; no raw dumps.
    cap = ns["_DIAG_MAX_TOTAL_CHARS"]
    diag = ns["_sw_gate_diagnostic"]("timeout", 12.3, "X" * 100000, "Y" * 100000, timeout_s=30)
    ok = _check(report, "bound: one 100k-char line -> report <= total cap", len(diag) <= cap) and ok
    byte_stderr = ("\n".join("[sw-gate-harness] m%d +%d.0ms" % (i, i) for i in range(4000))).encode("utf-8")
    diag = ns["_sw_gate_diagnostic"]("timeout", 1.0, b"", byte_stderr, timeout_s=30)
    ok = _check(report, "bound: 4000-line BYTES stderr -> report <= total cap", len(diag) <= cap) and ok
    ok = _check(report, "bytes stderr decoded to real newlines (no b'' / no escaped-\\n leak)",
                "\\n" not in diag and "b'" not in diag and "[sw-gate-harness]" in diag) and ok
    diag = ns["_sw_gate_diagnostic"]("nonzero-exit", 1.0, "", "", returncode=2, blob="Z" * 100000)
    ok = _check(report, "bound: oversized metadata -> report <= total cap", len(diag) <= cap) and ok

    # diagnostic failure preserves the outcome: a stderr whose str() raises must NOT make the diagnostic raise.
    class _Hostile:
        def __bool__(self):
            return True

        def __str__(self):
            raise RuntimeError("hostile stderr")

    safe = ns["_sw_gate_diagnostic"]("timeout", 1.0, "out", _Hostile())
    ok = _check(report, "diagnostic never masks the failure (hostile input -> fallback names kind)",
                isinstance(safe, str) and "timeout" in safe) and ok

    print("\n".join(report))
    print("RESULT:", "PASS" if ok else "FAIL")
    print("LIMIT: fake-only proof of the diagnostics helpers; NOT a real subprocess/CI run, NOT a cause finding.")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
