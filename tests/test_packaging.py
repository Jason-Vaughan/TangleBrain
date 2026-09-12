"""Tests for packaging metadata invariants declared in pyproject.toml.

Dependency constraints are not exercised by any runtime code path, so nothing catches a bad one
until an install resolves differently — which can be months after the constraint was written, in
someone else's environment. Pinning them here moves that discovery to commit time. Some were
written after an incident and some ahead of one.

The founding case: the ``delegate`` extra shipped as ``mcp >= 1.0``. When the SDK released
2.0.0 it renamed ``FastMCP`` to ``MCPServer`` and dropped the ``mcp.server.fastmcp`` import path
``tanglebrain/mcp_server.py`` uses, so every fresh resolve installed an SDK the code could not
import. Nothing in the repo had changed. The defect was never the SDK's release — it was the
open-ended constraint that let a major land unannounced.
"""
from __future__ import annotations

import re
import subprocess
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
    """The dependency invariants asserted over ``pyproject.toml``'s requirement tables.

    Deliberately described by subject rather than by listing the assertions: each one added here
    used to strand a prose enumeration somewhere, and a longer list only relocates the staleness.
    The tests below are the enumeration.
    """

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

    @staticmethod
    def _requirement_name(requirement):
        """Return the PEP 503-normalized distribution name from a PEP 508 requirement string.

        Truncated at the first character that cannot appear in a name, so ``wheel >= 0.45, < 1``
        yields ``wheel`` while a different distribution that merely starts with those letters
        yields its own full name and does not collide. Normalized so ``Wheel`` and ``whe_el``
        compare equal to ``wheel``.

        Args:
            requirement: A PEP 508 requirement string as declared in pyproject.toml.

        Returns:
            The lowercased, PEP 503-normalized distribution name.
        """
        name = re.match(r"[A-Za-z0-9._-]*", requirement.strip()).group(0)
        return re.sub(r"[-_.]+", "-", name).lower()

    def test_wheel_is_not_a_build_requirement(self):
        # Why the requirement is unnecessary is stated once, on the `[build-system]` table in
        # pyproject.toml, where someone re-adding the line is already looking; the assertion
        # message below points there rather than restating it, so a future setuptools fact has
        # only one place to be corrected.
        #
        # Pinned here rather than left to that comment, because of the shape the
        # question comes back in. Restored, the line does not return as "should this exist at all"
        # — it returns as a routine `wheel x.y -> x.z` bump that reads like every other dependency
        # pull request and merges without anyone reaching the decision. Failing this test is what
        # turns that back into a deliberate edit.
        requires = self.pyproject["build-system"]["requires"]
        offenders = [req for req in requires if self._requirement_name(req) == "wheel"]
        self.assertEqual(
            offenders,
            [],
            f"`wheel` is back in build-system.requires as {offenders!r} — setuptools ships its "
            "own bdist_wheel, so the build never resolves it; see the comment on that table in "
            "pyproject.toml",
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

class DesignDocCitationTest(unittest.TestCase):
    """Every design doc a shipped module cites must be readable in the public tree.

    Deliberately not skipped on 3.10: this reads text files and needs no ``tomllib``, so it covers
    the whole matrix rather than the subset the dependency tests above reach.
    """

    #: A Markdown filename, with or without surrounding backticks. Both spellings occur in this
    #: package, so matching only the backticked form would leave the bare ones unchecked.
    CITATION = re.compile(r"[A-Za-z0-9_./-]+\.md")

    @staticmethod
    def _readable_doc_names():
        """Return the exact-case name of every Markdown doc readable in the *public* tree.

        Sourced from ``git ls-files`` rather than from a directory listing, because what this test
        is about is whether a reader who clones the repository can open the file. A listing of the
        working tree answers a different question: this repo keeps several gitignored Markdown docs
        at its root, so a citation to one of them would resolve on a maintainer's laptop and fail
        on CI — which is the same defect the test exists to catch, relocated one directory. Tracked
        is the property; present is not.

        Set membership is also case-sensitive whatever the filesystem is. That matters because
        :meth:`pathlib.Path.is_file` is not: on macOS it answers True for a name differing only in
        case, silently resolving a citation against a *different* document, and False on every
        Linux CI leg.

        The two locations are named here and nowhere else, so a doc can be added, renamed, or moved
        between them without an allow-list to remember.

        Returns:
            The set of tracked Markdown filenames, by exact case, from the repository root and
            ``docs/design/``.
        """
        listed = subprocess.run(
            ["git", "ls-files", "--", "*.md"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        allowed_parents = {Path("."), Path("docs/design")}
        return {
            path.name
            for path in map(Path, listed.stdout.splitlines())
            if path.parent in allowed_parents
        }

    def test_every_doc_cited_from_the_package_resolves(self):
        # A citation naming a file the reader cannot open is worse than no citation: it implies a
        # source was consulted and can be consulted again. The failure is invisible in review
        # because the name looks right — `observability-strategy.md` reads exactly like a doc this
        # repo has, and it is one, under `.prawduct/`, which is gitignored. So a reader of the
        # public tree found nothing, and could not tell a deleted doc from a renamed one from a
        # doc that was never published.
        #
        # Resolving every cited name, rather than grepping for the one known-bad one, is what makes
        # this a construction instead of a sweep somebody has to remember to re-run: the next
        # citation into a gitignored doc fails here whatever it is called.
        readable = self._readable_doc_names()
        unresolved = []
        for module in sorted((REPO_ROOT / "tanglebrain").rglob("*.py")):
            text = module.read_text(encoding="utf-8")
            for cited in self.CITATION.findall(text):
                if Path(cited).name not in readable:
                    unresolved.append(f"{module.relative_to(REPO_ROOT)} cites {cited!r}")
        self.assertEqual(
            unresolved,
            [],
            "these citations name a file that is not readable in the public tree — a reader cannot "
            "follow them, and cannot tell a deleted doc from a gitignored one. A case-only "
            f"mismatch counts: it resolves on macOS and fails on CI. {unresolved}",
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
