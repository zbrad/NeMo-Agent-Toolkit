"""One-off CLI tool: turn a slice of nemo-agent-toolkit's uv.lock into
plain pip requirements files.

uv.lock pins every package to one exact resolved version per
resolution-marker branch (its `resolution-markers` cap at
`python_full_version >= '3.13'` -- there is no 3.14 branch at all). This
tool walks the dependency graph from a chosen set of workspace-member
roots (+ extras), and for every third-party package it reaches, emits
`name>=<max resolved version across marker branches>` instead of an exact
pin -- a floor a plain `pip install` under Python 3.14 can resolve
forward from, rather than a pin that was never validated on 3.14.

Local workspace members (uv.lock `source.editable` entries) are excluded
from the version-pin output and instead emitted as `-e packages/<dir>`
lines, since they aren't real PyPI releases.

Usage (run from anywhere; paths are relative to this file's location,
i.e. <repo>/requirements/lock_requirements_extractor.py):
    python3 requirements/lock_requirements_extractor.py
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class RequirementSpec:
    """A single resolved third-party requirement: a name, a floor version,
    and any sub-extras (e.g. `uvicorn[standard]`) requested of it."""

    name: str
    version: str
    extras: set[str] = field(default_factory=set)

    def as_requirement_line(self) -> str:
        """Render as a pip requirement line, e.g. `uvicorn[standard]>=0.38`."""
        extras_suffix = f"[{','.join(sorted(self.extras))}]" if self.extras else ""
        return f"{self.name}{extras_suffix}>={self.version}"


@dataclass
class ClosureResult:
    """The outcome of resolving one set of roots against the lock graph."""

    third_party: dict[str, RequirementSpec]
    local_editable: list[str]


class LockRequirementsExtractor:
    """Resolves the transitive third-party requirement closure for a chosen
    set of nemo-agent-toolkit workspace-member roots (and their extras) out
    of `uv.lock`, and renders it as pip-installable `>=` floors instead of
    uv's exact per-marker-branch pins."""

    def __init__(self, lock_path: Path) -> None:
        self._packages_by_name: dict[str, list[dict[str, Any]]] = {}
        self._load(lock_path)

    def _load(self, lock_path: Path) -> None:
        with lock_path.open("rb") as handle:
            data = tomllib.load(handle)
        for package in data.get("package", []):
            self._packages_by_name.setdefault(package["name"], []).append(package)

    @staticmethod
    def _is_local(package: dict[str, Any]) -> bool:
        source = package.get("source", {})
        return "editable" in source or "virtual" in source

    def _max_version(self, name: str) -> Optional[str]:
        """Highest version among this name's marker-branch variants, using
        plain dotted-tuple comparison (no PyPI `packaging` dependency)."""
        versions = [
            entry["version"]
            for entry in self._packages_by_name.get(name, [])
            if entry.get("version")
        ]
        if not versions:
            return None

        def sort_key(version: str) -> tuple[int, ...]:
            parts: list[int] = []
            for chunk in version.split(".")[:4]:
                digits = "".join(
                    character for character in chunk if character.isdigit()
                )
                parts.append(int(digits) if digits else 0)
            return tuple(parts)

        return max(versions, key=sort_key)

    def _dependency_edges(
        self, entries: list[dict[str, Any]], extras_wanted: list[str]
    ) -> list[dict[str, Any]]:
        """Union base `dependencies` (across all marker-branch variants of
        this package) plus each requested extra's dependencies.

        uv.lock only materializes a `[package.optional-dependencies]` group
        for an extra when something *else* in the workspace resolution
        consumes it directly (e.g. nvidia-nat-langchain only has an "all"
        group there, even though it `provides-extras` many more) -- so an
        extra missing from that table falls back to scanning
        `[package.metadata].requires-dist` for `extra == '<name>'`-marked
        entries instead of silently resolving to nothing.
        """
        edges: list[dict[str, Any]] = []
        for entry in entries:
            edges.extend(entry.get("dependencies", []))
            optional = entry.get("optional-dependencies", {})
            requires_dist = entry.get("metadata", {}).get("requires-dist", [])
            for extra_name in extras_wanted:
                if extra_name in optional:
                    edges.extend(optional[extra_name])
                    continue
                marker_token = f"extra == '{extra_name}'"
                edges.extend(
                    item
                    for item in requires_dist
                    if marker_token in (item.get("marker") or "")
                )
        return edges

    def resolve(self, roots: dict[str, list[str]]) -> ClosureResult:
        """Walk the dependency graph from `roots` (name -> requested extras).

        Args:
            roots: mapping of workspace-member (or any package) name to the
                list of its extras to activate at that root.

        Returns:
            The third-party requirement closure and the local editable
            packages encountered, both excluding the roots' own extras
            bookkeeping (only real graph nodes).
        """
        extras_by_name: dict[str, list[str]] = {
            name: list(extras) for name, extras in roots.items()
        }
        pending_extras: dict[str, set[str]] = {}
        visited: set[str] = set()
        queue: list[str] = list(roots.keys())
        third_party: dict[str, RequirementSpec] = {}
        local_editable: list[str] = []

        while queue:
            name = queue.pop(0)
            if name in visited:
                continue
            visited.add(name)

            entries = self._packages_by_name.get(name)
            if not entries:
                continue

            if self._is_local(entries[0]):
                local_editable.append(name)
            else:
                version = self._max_version(name)
                if version is not None:
                    third_party[name] = RequirementSpec(
                        name=name,
                        version=version,
                        extras=pending_extras.get(name, set()),
                    )

            for edge in self._dependency_edges(entries, extras_by_name.get(name, [])):
                dep_name = edge["name"]
                dep_extras = set(edge.get("extra") or edge.get("extras") or [])
                if dep_name in third_party:
                    third_party[dep_name].extras.update(dep_extras)
                else:
                    pending_extras.setdefault(dep_name, set()).update(dep_extras)
                queue.append(dep_name)

        return ClosureResult(third_party=third_party, local_editable=local_editable)

    @staticmethod
    def render(
        result: ClosureResult,
        package_dir_by_name: dict[str, str],
        header: str,
        extends: Optional[str] = None,
    ) -> str:
        """Render a `ClosureResult` as a pip requirements file's text.

        Args:
            result: the closure to render.
            package_dir_by_name: workspace package name -> its `packages/`
                subdirectory, for `-e` lines.
            header: a comment block describing this file's scope.
            extends: an optional `-r <file>` line for layering onto a base
                requirements file already covering some of this closure.
        """
        lines: list[str] = [header, ""]
        if extends is not None:
            lines.append(f"-r {extends}")
            lines.append("")
        if result.local_editable:
            lines.append("# workspace members (editable)")
            for name in result.local_editable:
                directory = package_dir_by_name.get(name)
                if directory is None:
                    raise ValueError(
                        f"no packages/ directory known for local package '{name}'"
                    )
                lines.append(f"-e packages/{directory}")
            lines.append("")
        if result.third_party:
            lines.append("# third-party floors (>=), extracted from uv.lock's resolved")
            lines.append(
                "# versions -- not exact pins, so pip can resolve forward on 3.14"
            )
            for spec in sorted(result.third_party.values(), key=lambda item: item.name):
                lines.append(spec.as_requirement_line())
            lines.append("")
        return "\n".join(lines)


if __name__ == "__main__":
    OUT_DIR = Path(__file__).resolve().parent
    REPO_ROOT = OUT_DIR.parent
    OUT_DIR.mkdir(exist_ok=True)

    extractor = LockRequirementsExtractor(REPO_ROOT / "uv.lock")

    # Package-name -> packages/<dir> mapping (uv.lock names are the PyPI
    # "nvidia-nat-*" dashed form; the checkout uses underscored dir names).
    package_dirs = {
        "nvidia-nat-core": "nvidia_nat_core",
        "nvidia-nat-atif": "nvidia_nat_atif",
        "nvidia-nat-eval": "nvidia_nat_eval",
        "nvidia-nat-opentelemetry": "nvidia_nat_opentelemetry",
        "nvidia-nat-langchain": "nvidia_nat_langchain",
    }

    base_result = extractor.resolve(
        {
            "nvidia-nat-core": [],
            "nvidia-nat-eval": [],
            "nvidia-nat-opentelemetry": [],
        }
    )
    base_text = extractor.render(
        base_result,
        package_dirs,
        header=(
            "# Base runtime deps for nvidia-nat-core + nvidia-nat-eval +\n"
            "# nvidia-nat-opentelemetry, extracted from uv.lock for a plain\n"
            "# `pip install -r` .venv (Python 3.14; uv.lock's own\n"
            "# resolution-markers never covered 3.14). Regenerate with\n"
            "# lock_requirements_extractor.py, don't hand-edit versions."
        ),
    )
    (OUT_DIR / "base.txt").write_text(base_text)
    print(
        f"wrote {OUT_DIR / 'base.txt'} ({len(base_result.third_party)} third-party, "
        f"{len(base_result.local_editable)} local)"
    )

    langchain_result = extractor.resolve({"nvidia-nat-langchain": ["openai"]})
    # Layer onto base.txt: drop anything base.txt already covers.
    incremental_third_party = {
        name: spec
        for name, spec in langchain_result.third_party.items()
        if name not in base_result.third_party
    }
    incremental_local = [
        name
        for name in langchain_result.local_editable
        if name not in base_result.local_editable
    ]
    langchain_text = extractor.render(
        ClosureResult(
            third_party=incremental_third_party, local_editable=incremental_local
        ),
        package_dirs,
        header=(
            "# nvidia-nat-langchain[openai] on top of base.txt -- the\n"
            "# react_agent workflow + OpenAI-compatible `_type: openai` LLM\n"
            "# provider used against llama-server. Regenerate with\n"
            "# lock_requirements_extractor.py, don't hand-edit versions."
        ),
        extends="base.txt",
    )
    (OUT_DIR / "langchain-openai.txt").write_text(langchain_text)
    print(
        f"wrote {OUT_DIR / 'langchain-openai.txt'} ({len(incremental_third_party)} "
        f"incremental third-party, {len(incremental_local)} incremental local)"
    )
