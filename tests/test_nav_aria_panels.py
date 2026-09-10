"""Bounded, inert acceptance checks for the navigation tab/panel ARIA associations.

The rail and every subtab bar already follow the WAI-ARIA tabs pattern for the tab side
(``role="tab"``/``aria-selected``/roving ``tabindex`` are set by ``reform.js``). This checks the
static panel side that the template must carry: each tab has an ``aria-controls`` pointing at its
content panel, and each panel is a ``role="tabpanel"`` that points back with ``aria-labelledby``
(bidirectional), completing the pattern.

Parsing is stdlib ``html.parser`` only over the raw template text -- no Flask render, no browser,
no network. Jinja ``{{ }}``/``{% %}`` fragments are inert text to the parser. This is an
APG-completeness check, not a rendered-browser or assistive-technology certification.
"""

import os
import unittest
from collections import Counter
from html.parser import HTMLParser

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE = os.path.join(_HERE, "..", "src", "ui", "web", "templates", "reform.html")

# The six real tablists and their tabs, enumerated from the template. ``<bar>`` is the rail (view)
# name or the subtab bar's ``data-tabs`` value; each pair yields the expected tab/panel id strings.
RAIL_VIEWS = ["device", "hunt", "operate", "crack", "map", "offline-data", "terminal", "settings"]
SUBTAB_BARS = {
    "device": ["dash", "fw", "sw", "mesh"],
    "xcomm": ["pool", "rules", "hist", "stream"],
    "hunt": ["wifi", "ble", "incidents", "targets", "tail", "sense", "graph"],
    "operate": ["console", "macros", "broadcast", "antenna"],
    "map": ["wardrive", "multi", "flock", "offline"],
}


def _expected_pairs():
    """Return {tab_id: panel_id} for every expected tab/panel association."""
    pairs = {}
    for v in RAIL_VIEWS:
        pairs["railtab-" + v] = "view-" + v
    for bar, subs in SUBTAB_BARS.items():
        for s in subs:
            pairs["subtab-{}-{}".format(bar, s)] = "subpanel-{}-{}".format(bar, s)
    return pairs


class _Collector(HTMLParser):
    """Collect every start tag's attributes so the assertions can reason about the whole document."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.tags = []  # (tagname, {attr: value})

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v if v is not None else "") for k, v in attrs}))

    # Treat start-end tags (e.g. self-closing SVG paths) the same as start tags.
    handle_startendtag = handle_starttag


def _classes(attrs):
    return set((attrs.get("class") or "").split())


class NavAriaPanelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_TEMPLATE, encoding="utf-8") as fh:
            cls.html = fh.read()
        p = _Collector()
        p.feed(cls.html)
        cls.tags = p.tags
        cls.expected = _expected_pairs()
        # Tabs are exactly the elements whose aria-controls targets a nav panel (view-*/subpanel-*),
        # which deliberately excludes the "Actions" menu buttons (aria-controls="hunt-target-menu").
        cls.tabs = [
            (t, a)
            for (t, a) in cls.tags
            if a.get("aria-controls", "").startswith(("view-", "subpanel-"))
        ]
        cls.panels = [(t, a) for (t, a) in cls.tags if a.get("role") == "tabpanel"]

    def test_all_ids_unique(self):
        ids = [a["id"] for (_t, a) in self.tags if "id" in a and a["id"]]
        dupes = [i for i, n in Counter(ids).items() if n > 1]
        self.assertEqual(dupes, [], "duplicate id(s) introduced: {}".format(dupes))

    def test_expected_tab_and_panel_sets_present(self):
        tab_ids = sorted(a.get("id", "") for (_t, a) in self.tabs)
        panel_ids = sorted(a.get("id", "") for (_t, a) in self.panels)
        self.assertEqual(tab_ids, sorted(self.expected.keys()))
        self.assertEqual(panel_ids, sorted(self.expected.values()))

    def test_bidirectional_pairing(self):
        panels_by_id = {a["id"]: a for (_t, a) in self.panels if a.get("id")}
        controllers = Counter()
        for _t, a in self.tabs:
            tab_id = a.get("id", "")
            target = a["aria-controls"]
            self.assertIn(tab_id, self.expected, "unexpected tab id {!r}".format(tab_id))
            self.assertEqual(
                target, self.expected[tab_id], "tab {} controls wrong panel".format(tab_id)
            )
            self.assertIn(target, panels_by_id, "tab {} controls missing panel".format(tab_id))
            self.assertEqual(
                panels_by_id[target].get("aria-labelledby"),
                tab_id,
                "panel {} does not label back to {}".format(target, tab_id),
            )
            controllers[target] += 1
        multi = [pid for pid, n in controllers.items() if n > 1]
        self.assertEqual(multi, [], "panel(s) with more than one controller: {}".format(multi))

    def test_rail_tabs_are_navitems_and_views_are_sections(self):
        for t, a in self.tabs:
            if a["aria-controls"].startswith("view-"):
                self.assertEqual(t, "div")
                self.assertIn("navitem", _classes(a))
                self.assertEqual(a.get("id"), "railtab-" + a.get("data-view", ""))
        for t, a in self.panels:
            if a.get("id", "").startswith("view-"):
                self.assertEqual(t, "section")
                self.assertIn("view", _classes(a))
                self.assertEqual(a.get("aria-labelledby"), "railtab-" + a.get("data-view", ""))

    def test_subtabs_are_buttons_and_panels_are_sub_divs(self):
        for t, a in self.tabs:
            if a["aria-controls"].startswith("subpanel-"):
                self.assertEqual(t, "button")
                self.assertTrue(a.get("data-sub"), "subtab button missing data-sub")
        for t, a in self.panels:
            if a.get("id", "").startswith("subpanel-"):
                self.assertEqual(t, "div")
                self.assertIn("sub", _classes(a))
                self.assertTrue(a.get("data-sub"), "sub panel missing data-sub")

    def test_no_count_badge_is_a_tabpanel(self):
        # <span class="sub"> count badges (e.g. #rail-device-count, "live") carry class="sub" but no
        # data-sub; they must never be labeled tab panels.
        for t, a in self.panels:
            self.assertIn(t, ("section", "div"), "unexpected tabpanel host <{}>".format(t))
            self.assertTrue(
                a.get("data-view") or a.get("data-sub"),
                "a role=tabpanel element lacks data-view/data-sub (a badge?): {}".format(a.get("id")),
            )
        for t, a in self.tags:
            if t == "span" and "sub" in _classes(a) and "data-sub" not in a:
                self.assertNotEqual(a.get("role"), "tabpanel")
                self.assertNotIn("aria-labelledby", a)

    def test_preexisting_count_badge_id_preserved(self):
        ids = {a.get("id") for (_t, a) in self.tags if a.get("id")}
        self.assertIn("rail-device-count", ids, "existing count-badge id was dropped")

    def test_panel_data_values_match_enumeration(self):
        view_panels = sorted(
            a.get("data-view") for (_t, a) in self.panels if a.get("id", "").startswith("view-")
        )
        self.assertEqual(view_panels, sorted(RAIL_VIEWS))
        sub_panels = sorted(
            a.get("data-sub") for (_t, a) in self.panels if a.get("id", "").startswith("subpanel-")
        )
        expected_subs = sorted(s for subs in SUBTAB_BARS.values() for s in subs)
        self.assertEqual(sub_panels, expected_subs)


if __name__ == "__main__":
    unittest.main()
