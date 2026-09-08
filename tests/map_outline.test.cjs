/* CI entry point for the published offline map-outline regression suite.
 *
 * The Node regression workflow runs `node --test "tests/*.test.cjs"`. The sibling
 * `tests/test_map_outline.cjs` (a `test_*.cjs` name) does not match that glob, so its cases would
 * not execute in CI. This thin entry loads that already-published suite so its tests register and
 * run exactly once under the CI glob, without renaming or duplicating the published file. */
require("./test_map_outline.cjs");
