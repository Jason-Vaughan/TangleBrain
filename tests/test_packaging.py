"""Tests for packaging metadata invariants declared in pyproject.toml.

Dependency constraints are not exercised by any runtime code path, so nothing catches a bad one
until an install resolves differently — which can be months after the constraint was written, in
someone else's environment. These tests pin the invariants that have already bitten.

The history behind them: the ``delegate`` extra shipped as ``mcp >= 1.0``. When the SDK released
2.0.0 it renamed ``FastMCP`` to ``MCPServer`` and dropped the ``mcp.server.fastmcp`` import path
``tanglebrain/mcp_server.py`` uses, so every fresh resolve installed an SDK the code could not
import. Nothing in the repo had changed. The defect was never the SDK's release — it was the
open-ended constraint that let a major land unannounced.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only, where the check skips
    tomllib = None

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
DEPENDABOT_PATH = REPO_ROOT / ".github" / "dependabot.yml"


@unittest.skipIf(tomllib is None, "tomllib requires Python 3.11+; covered by the 3.11/3.12 CI jobs")
class DependencyBoundTest(unittest.TestCase):
    """Every declared dependency must carry an upper bound, and ``mcp`` must stay within 2.x."""

    def setUp(self):
        """Parse pyproject.toml once per test."""
        self.pyproject = tomllib.loads(PYPROJECT_PATH.read_text())

    def _all_requirements(self):
        """Yield ``(source, requirement)`` for every dependency the project declares.

        Covers ``build-system.requires``, the core ``project.dependencies`` list, and every
        ``project.optional-dependencies`` extra, so a dependency added to a new extra is held to
        the same rule without anyone remembering to extend this test.

        ``build-system.requires`` is included because leaving it out is what let ``setuptools>=68``
        sit unbounded — and vulnerable to two advisories — while every other requirement in the
        file was checked. A build requirement is resolved when someone builds from sdist, so an
        unbounded one lets a major land in that build with no announcement and no commit to blame,
        which is the same defect the rule exists for. It is a narrower blast radius than a runtime
        dependency, not a different kind of problem.

        Returns:
            A list of ``(source, requirement)`` pairs, where ``source`` names the table the
            requirement came from (for a legible failure message).
        """
        project = self.pyproject["project"]
        build_system = self.pyproject.get("build-system", {})
        pairs = [
            ("build-system.requires", req) for req in build_system.get("requires", [])
        ]
        pairs.extend(("dependencies", req) for req in project.get("dependencies", []))
        for extra, reqs in project.get("optional-dependencies", {}).items():
            pairs.extend((f"optional-dependencies.{extra}", req) for req in reqs)
        return pairs

    @staticmethod
    def _version_spec(requirement):
        """Return the version-specifier portion of a PEP 508 requirement string.

        Everything after ``;`` is an environment marker, and markers carry their own comparison
        operators — ``foo >= 1 ; python_version < "3.13"`` contains a ``<`` while placing no
        ceiling on ``foo`` at all. Whitespace is stripped because ``mcp>=2,<3`` and
        ``mcp >= 2, < 3`` are the same constraint, and a test that only accepts one of them reds
        the suite over formatting.

        Args:
            requirement: A PEP 508 requirement string as declared in pyproject.toml.

        Returns:
            The requirement with any environment marker removed and all whitespace stripped.
        """
        return requirement.split(";", 1)[0].replace(" ", "")

    def test_every_declared_dependency_carries_an_upper_bound(self):
        # The general rule, not a restatement of the mcp case: an unbounded constraint lets a
        # major version land in a user's fresh install with no announcement, no commit to blame,
        # and a green CI that predates the release. Bounding every declared dependency makes a
        # major bump a deliberate act with a migration behind it.
        requirements = self._all_requirements()
        self.assertTrue(requirements, "expected pyproject to declare at least one dependency")
        for source, requirement in requirements:
            with self.subTest(source=source, requirement=requirement):
                self.assertIn(
                    "<",
                    self._version_spec(requirement),
                    f"{source}: {requirement!r} has no upper bound — an unbounded constraint "
                    "lets the next major land unannounced (see the module docstring)",
                )

    def test_requirement_discovery_reaches_the_build_system_table(self):
        # The bound rule above is only as wide as _all_requirements, and for most of this file's
        # life that was `project.dependencies` plus the extras — which is how `setuptools>=68` sat
        # unbounded, and below two advisories, while every other requirement in the file was
        # checked. `test_every_declared_dependency_carries_an_upper_bound` cannot notice that
        # regression: it iterates whatever it is handed, so deleting the build-system lines from
        # _all_requirements leaves it passing over a smaller set. This pins the set itself.
        sources = {source for source, _ in self._all_requirements()}
        self.assertIn(
            "build-system.requires",
            sources,
            "_all_requirements no longer discovers build-system.requires — the upper-bound rule "
            "would silently stop covering the build backend, which is the gap that let an "
            "unbounded, vulnerable setuptools floor through",
        )

    def test_mcp_is_pinned_to_the_2x_major(self):
        # Stronger than the general rule above, and separate from it on purpose. `mcp` needs a
        # specific major rather than merely *a* ceiling: `mcp_server.py` imports
        # `mcp.server.mcpserver`, which exists in 2.x and in no earlier major, so a floor that
        # admits 1.x ships an extra that cannot import. That is the failure the < 2 cap was
        # holding off before the migration, pointing the other way.
        #
        # Both ends are asserted. The floor stops a resolve reaching an SDK without the module;
        # the ceiling stops the next major landing unannounced, which is the rule every other
        # dependency here follows. Moving either end is a breaking change for installs and is
        # announced as one — docs/design/deprecation-policy.md, "Dependency floors".
        delegate = self.pyproject["project"]["optional-dependencies"]["delegate"]
        mcp_requirements = [req for req in delegate if req.startswith("mcp")]
        self.assertEqual(len(mcp_requirements), 1, "expected exactly one mcp requirement")
        requirement = mcp_requirements[0]
        spec = self._version_spec(requirement)
        self.assertIn(
            ">=2",
            spec,
            f"mcp_server.py imports mcp.server.mcpserver, which needs 2.x; got {requirement!r}",
        )
        self.assertIn(
            "<3",
            spec,
            f"the mcp requirement must stay below the next major; got {requirement!r}",
        )

class DependabotConfigTest(unittest.TestCase):
    """The Dependabot config makes a claim about a provider, so pin the half that is ours.

    Nothing here can observe what Dependabot actually does — that is the provider's behaviour, and
    the evidence for it is a citation in the pull request, not an assertion. What *is* ours is the
    option we set, and it is the one line the feature turns on: `versioning-strategy: increase`.

    Left at the `auto` default it resolves to `increase` or `widen` from Dependabot's own
    app-vs-library classification, and `widen` rewrites `>= 0.27, < 1` into a constraint permitting
    both 0.x and 1.x. That erases the upper bound the test above exists to enforce — so the two
    tests defend one invariant from opposite ends, and dropping this line would quietly undo the
    other. It is exactly the shape of defect this module was written for: nothing exercises it at
    runtime, and a bad value is discovered months later in someone else's resolve.
    """

    def _config(self) -> dict:
        """Return the parsed Dependabot config.

        Returns:
            The config as a dict. Fails the test if the file is absent or not a mapping.
        """
        self.assertTrue(DEPENDABOT_PATH.is_file(), f"{DEPENDABOT_PATH} is missing")
        loaded = yaml.safe_load(DEPENDABOT_PATH.read_text(encoding="utf-8"))
        self.assertIsInstance(loaded, dict, "dependabot.yml did not parse as a mapping")
        return loaded

    def test_every_pip_entry_pins_versioning_strategy_to_increase(self):
        entries = [u for u in self._config()["updates"] if u["package-ecosystem"] == "pip"]
        self.assertTrue(entries, "no pip ecosystem is configured — nothing watches pyproject.toml")
        for entry in entries:
            with self.subTest(directory=entry.get("directory")):
                self.assertEqual(
                    entry.get("versioning-strategy"), "increase",
                    "a pip entry left versioning-strategy unset or widened: `widen` dissolves the "
                    "upper bounds asserted above instead of forcing the decision they exist for",
                )

    def test_both_ecosystems_named_by_the_supply_chain_gap_are_watched(self):
        # github-actions is not incidental: `publish.yml` holds `id-token: write` for PyPI
        # trusted publishing, so its pinned actions are the highest-privilege dependency here and
        # nothing else watches them.
        #
        # A floor, not an exact set. Watching a *further* ecosystem is the direction this test is
        # named for, and an equality assertion would red on it — turning "cover the supply chain"
        # into "cover exactly this much of it", which is the opposite instruction.
        ecosystems = {u["package-ecosystem"] for u in self._config()["updates"]}
        self.assertLessEqual({"pip", "github-actions"}, ecosystems)

    def test_major_bumps_are_not_grouped_away(self):
        # A cap exists to force one deliberate decision per major. A group covering `major` would
        # bundle two unrelated majors into a single accept-or-reject, which is the decision the
        # cap was protecting, taken away again by the tooling meant to surface it.
        #
        # Every entry must HAVE a group, asserted rather than assumed: without it the loop below
        # iterates nothing and reports success over a config that groups everything by default.
        seen = 0
        for entry in self._config()["updates"]:
            groups = entry.get("groups") or {}
            with self.subTest(ecosystem=entry["package-ecosystem"]):
                self.assertTrue(groups, "no groups — routine bumps would arrive one PR per package")
            for name, group in groups.items():
                seen += 1
                with self.subTest(ecosystem=entry["package-ecosystem"], group=name):
                    self.assertNotIn("major", group.get("update-types", []))
        self.assertTrue(seen, "no group was examined — this test would pass over anything")


if __name__ == "__main__":
    unittest.main()
