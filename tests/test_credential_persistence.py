"""Locks in the guarantee that a saved password survives an update.

Cyber Controller keeps the boot gate, the vault, and every secret under
``~/.cyber-controller`` — the user's home directory — on purpose. An update only
rewrites files inside the install tree (the folder that holds the binary). Because
the credential store lives in home instead, an update can't reach it, so a password
set on an old install still unlocks the new one.

If anyone moves a store root back under the install/source tree these tests fail,
which is the whole point: it would silently wipe saved passwords on the next update.
"""

from pathlib import Path

import pytest

from src.security import physical_key, secure_store, vault, web_auth

HOME_STORE = (Path.home() / ".cyber-controller").resolve()

# (module, name of the attribute that holds its on-disk root)
STORE_ROOTS = [
    (vault, "_DEFAULT_DIR"),
    (secure_store, "_DIR"),
    (web_auth, "_CONFIG_DIR"),
    (physical_key, "_CONFIG_DIR"),
]

_IDS = [mod.__name__.rsplit(".", 1)[-1] for mod, _ in STORE_ROOTS]


@pytest.mark.parametrize("module, attr", STORE_ROOTS, ids=_IDS)
def test_store_root_lives_under_home(module, attr):
    root = getattr(module, attr)
    assert isinstance(root, Path), f"{module.__name__}.{attr} is not a Path"
    resolved = root.resolve()
    assert HOME_STORE in (resolved, *resolved.parents), (
        f"{module.__name__}.{attr} = {resolved} is not under {HOME_STORE}; "
        "secrets must live in home so updates can't touch them"
    )


def test_stores_sit_outside_the_install_tree():
    """The install tree gets overwritten on update; the store must not be inside it."""
    import src

    install_tree = Path(src.__file__).resolve().parent.parent
    for module, attr in STORE_ROOTS:
        root = getattr(module, attr).resolve()
        assert install_tree not in (root, *root.parents), (
            f"{module.__name__}.{attr} = {root} is inside the install tree "
            f"{install_tree}; an update would erase saved passwords"
        )
