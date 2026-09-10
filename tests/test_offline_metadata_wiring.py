"""Source/AST wiring checks for the Offline Data view (no execution, no app/router import).

Reads only the candidate source files by relative path (matching the repo layout) and inspects them
statically: the rail navitem + view section + script/style includes in reform.html, the crumbNames entry in
reform.js, and -- via AST -- that the app.py route is bound with BOTH existing auth decorators and injects the
accepted parser callable. It never imports Flask, the app, or the parser, and runs nothing.

Import-safe: importing this file runs no assertions and never exits; the discoverable
``test_offline_metadata_wiring()`` raises AssertionError on the first failed check; direct invocation runs the
same assertions and exits nonzero on failure.
"""

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(_HERE, *rel), encoding="utf-8") as f:
        return f.read()


_passed = [0]


def check(name, cond, detail: object = ""):
    if not cond:
        raise AssertionError("%s -- %s" % (name, detail))
    _passed[0] += 1
    print("PASS  " + name)


def test_offline_metadata_wiring():
    _passed[0] = 0

    # ---- reform.html: navitem + view + includes, and placement after the grow spacer, before TERMINAL ----
    html = _read(("..", "src", "ui", "web", "templates", "reform.html"))
    check("html has offline-data navitem", 'data-view="offline-data" id="railtab-offline-data"' in html, "navitem missing")
    check("html has offline-data view section", 'id="view-offline-data"' in html and 'role="tabpanel"' in html, "view missing")
    check("html view has ARIA link to railtab", 'aria-labelledby="railtab-offline-data"' in html, "aria-labelledby missing")
    check("html has the editor controls", all(x in html for x in ('id="od-input"', 'id="od-analyze"', 'id="od-clear"', 'id="od-status"', 'id="od-detail"')), "editor ids missing")
    check("html includes offline_metadata.js before reform.js",
          html.index("offline_metadata.js") < html.index("filename='reform.js'"), "script order wrong")
    check("html includes offline_metadata.css", "offline_metadata.css" in html, "css link missing")
    # placement: the navitem sits after the grow spacer and before the TERMINAL navitem
    i_grow = html.index('<div class="grow">')
    i_offline = html.index('data-view="offline-data" id="railtab-offline-data"')
    i_terminal = html.index('data-view="terminal" id="railtab-terminal"')
    check("navitem placed after grow spacer, before TERMINAL", i_grow < i_offline < i_terminal, (i_grow, i_offline, i_terminal))
    # preserve the five job destinations
    for v in ("device", "hunt", "operate", "crack", "map"):
        check("preserves job destination " + v, ('data-view="%s" id="railtab-%s"' % (v, v)) in html, v + " missing")
    check("no inline onclick handlers added", "onclick" not in html.split('id="view-offline-data"')[1].split("</section>")[0], "inline handler present")

    # ---- reform.js: crumbNames entry only ----
    js = _read(("..", "src", "ui", "web", "static", "reform.js"))
    check("reform.js crumbNames has offline-data label", '"offline-data": "OFFLINE DATA"' in js, "crumbNames entry missing")
    check("reform.js binds CCOfflineData.syncVisibility on nav", "window.CCOfflineData.syncVisibility()" in js, "visibility hook not wired")

    # ---- app.py: AST-verify the route's decorators + parser injection ----
    app_src = _read(("..", "src", "ui", "web", "app.py"))
    tree = ast.parse(app_src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "api_offline_metadata_summarize":
            fn = node
            break
    check("app.py defines api_offline_metadata_summarize", fn is not None, "route function missing")
    assert fn is not None  # narrows the type after the check above (which already raises on None)
    dec_names = []
    route_paths = []
    for d in fn.decorator_list:
        if isinstance(d, ast.Name):
            dec_names.append(d.id)
        elif isinstance(d, ast.Call):
            # e.g. @app.route("/api/offline-metadata/summarize", methods=["POST"])
            if d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str):
                route_paths.append(d.args[0].value)
    check("route guarded by requires_auth", "requires_auth" in dec_names, dec_names)
    check("route guarded by requires_csrf_header", "requires_csrf_header" in dec_names, dec_names)
    check("route path is /api/offline-metadata/summarize", "/api/offline-metadata/summarize" in route_paths, route_paths)

    fn_src = ast.get_source_segment(app_src, fn) or ""
    check("route delegates to offline_metadata_api.summarize_response", "offline_metadata_api.summarize_response(" in fn_src, "no delegation")
    check("route injects the accepted parser callable", "summarize=summarize_sigmf_metadata" in fn_src, "parser not injected")
    check("route validates raw CONTENT_LENGTH", "validated_content_length(request.environ.get(\"CONTENT_LENGTH\"))" in fn_src, "content-length not validated")
    check("route reads bounded raw wsgi.input", 'request.environ["wsgi.input"].read(limit)' in fn_src, "raw read pattern missing")
    check("route imports the accepted parser + helper", "from src.core.sigmf_metadata import summarize_sigmf_metadata" in fn_src and "from src.ui.web import offline_metadata_api" in fn_src, "imports missing")
    # no second SigMF parser is defined anywhere in app.py by this change (delegation only)
    check("no second parser implemented in the route", "def summarize_sigmf_metadata" not in fn_src, "route re-implements parser")


if __name__ == "__main__":
    try:
        test_offline_metadata_wiring()
    except AssertionError as exc:
        print("\nFAIL  " + str(exc))
        sys.exit(1)
    print("\nALL PASS (%d assertions)" % _passed[0])
    sys.exit(0)
