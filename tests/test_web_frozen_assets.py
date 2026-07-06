"""The Web Remote UI's Jinja templates + static assets must be (a) resolved frozen-safe via resource_path,
not Path(__file__), and (b) bundled by build.py. If either regresses, every web page 500s (TemplateNotFound)
and every /static asset 404s in the installed PyInstaller build — while dev tests still pass.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_app_resolves_web_assets_via_resource_path():
    src = (_ROOT / "src" / "ui" / "web" / "app.py").read_text(encoding="utf-8")
    assert "_TEMPLATE_DIR = resource_path(" in src, "templates must resolve via resource_path (MEIPASS-safe)"
    assert "_STATIC_DIR = resource_path(" in src, "static must resolve via resource_path (MEIPASS-safe)"
    # The frozen-build trap: __file__ points into an unpopulated MEIPASS path.
    assert 'Path(__file__).parent / "templates"' not in src
    assert 'Path(__file__).parent / "static"' not in src


def test_build_bundles_web_assets():
    b = (_ROOT / "build.py").read_text(encoding="utf-8")
    assert "src/ui/web/templates" in b, "build.py must --add-data the web templates dir"
    assert "src/ui/web/static" in b, "build.py must --add-data the web static dir"


def test_resource_path_web_dirs_exist_and_have_content():
    from src.core.resources import resource_path

    tpl = resource_path("src", "ui", "web", "templates")
    stat = resource_path("src", "ui", "web", "static")
    assert tpl.is_dir() and any(tpl.glob("*.html")), "web templates dir missing or empty"
    assert stat.is_dir(), "web static dir missing"
