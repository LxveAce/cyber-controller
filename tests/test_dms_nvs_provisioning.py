"""Dead Man's Switch NVS generation must work IN-PROCESS (frozen-safe).

The frozen app cannot run ``sys.executable -m nvs_partition_gen`` — ``sys.executable`` is
``CyberController.exe``, not a Python interpreter — so ``provision.generate_nvs_bin`` MUST succeed via
its in-process ``generate()`` entry point and never fall through to the subprocess fallback. This
regressed as "Could not find the NVS partition generator (esp-idf-nvs-partition-gen)" because that
tool was neither a declared dependency nor bundled by ``build.py`` — fixed by adding it to
``pyproject`` dependencies and ``--collect-all``'ing it in the PyInstaller build.
"""
import os
import tempfile

import pytest

from src.core import suicide_setup

# The DMS host provisioner lives in the git submodule; skip cleanly if it isn't checked out.
pytestmark = pytest.mark.skipif(
    not (suicide_setup._HOST / "provision.py").exists(),
    reason="deadmans-switch submodule not checked out",
)


def test_nvs_partition_gen_is_available():
    """The tool that bakes the guardcfg NVS image must be resolvable (declared dep / vendored / bundled)."""
    prov = suicide_setup._load_provision()
    kind, _target = prov._find_nvs_gen(None)
    assert kind in ("module", "script")


def test_generate_nvs_bin_is_frozen_safe(monkeypatch):
    """generate_nvs_bin must build the image via its IN-PROCESS path — never the
    ``sys.executable -m ...`` subprocess fallback, which fails in a frozen build. We prove this by
    making any subprocess use fatal and asserting a correct image is still produced."""
    prov = suicide_setup._load_provision()

    def _no_subprocess(*_a, **_k):
        raise AssertionError("subprocess used — would fail in a frozen build (sys.executable is the .exe)")

    monkeypatch.setattr(prov.subprocess, "run", _no_subprocess)

    d = tempfile.mkdtemp()
    csv = os.path.join(d, "guardcfg.csv")
    out = os.path.join(d, "guardcfg.bin")
    with open(csv, "w", newline="") as fh:
        fh.write("key,type,encoding,value\n")
        fh.write("guardcfg,namespace,,\n")
        fh.write("pwhash,data,string,deadbeef\n")

    prov.generate_nvs_bin(csv, out, 0x3000, None)
    assert os.path.getsize(out) == 0x3000  # exact partition size, read/write NVS minimum
