"""NAT tool: save a markdown document to a local "project store" directory.

zbrad-local addition (not upstream NVIDIA code). Registers a single
`save_project_note` function-tool an agent can call mid-chat to write a
markdown summary of the session to disk, under a configured root
directory (default `~/nemo-project-store/`). Deliberately a single
save-only tool, not a full `nat.object_store.ObjectStore` CRUD backend --
see the `-home-zbrad-gh-nemo-agent-toolkit` project memory's
`pip-venv-py314-migration.md` for why that scope was chosen.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import AsyncGenerator

from pydantic import BaseModel, Field

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig

logger = logging.getLogger(__name__)


class SaveProjectNoteInput(BaseModel):
    """Arguments the agent supplies for one `save_project_note` call."""

    filename: str = Field(
        description=(
            "Relative filename to save under, e.g. 'nemo-tool-calling-notes.md'. "
            "Must be a plain relative path ending in '.md' -- no '..' segments and "
            "no leading '/'. Subdirectories are allowed, e.g. 'research/session-1.md'."
        )
    )
    content: str = Field(description="The full markdown content to write to the file.")


class SaveProjectNoteConfig(FunctionBaseConfig, name="save_project_note"):
    """Saves a markdown document to a local project-store directory on disk."""

    store_root: str = Field(
        default="~/nemo-project-store",
        description="Root directory the saved .md files are written under.",
    )


class ProjectStoreWriter:
    """Validates and writes a markdown note under a fixed root directory.

    Args:
        store_root: the root directory notes are saved under; created on
            first use if it doesn't already exist.
    """

    def __init__(self, store_root: Path) -> None:
        self._root = store_root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def save(self, filename: str, content: str) -> str:
        """Validate `filename` and write `content` under the store root.

        Returns:
            A human-readable success message, or an `Error: ...` message
            for the agent to relay/retry on -- never raises, so a bad
            filename doesn't abort the whole agent run.
        """
        target = self._resolve_target(filename)
        if isinstance(target, str):
            return target  # error message

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        logger.info("Saved project note to %s (%d chars)", target, len(content))
        return f"Saved {len(content)} characters to {target}"

    def _resolve_target(self, filename: str) -> "Path | str":
        """Resolve `filename` under the store root, or return an error string."""
        if not filename.endswith(".md"):
            return f"Error: filename must end in '.md', got {filename!r}"
        if filename.startswith("/") or ".." in Path(filename).parts:
            return f"Error: filename must be a relative path with no '..', got {filename!r}"

        target = (self._root / filename).resolve()
        if target != self._root and self._root not in target.parents:
            return f"Error: resolved path escapes the project store root: {filename!r}"
        return target


@register_function(config_type=SaveProjectNoteConfig)
async def save_project_note(
    config: SaveProjectNoteConfig, _builder: Builder
) -> AsyncGenerator[FunctionInfo, None]:
    """NAT entry point: build a `ProjectStoreWriter` and expose it as a tool."""
    writer = ProjectStoreWriter(Path(config.store_root))

    async def _save(input_data: SaveProjectNoteInput) -> str:
        return writer.save(input_data.filename, input_data.content)

    yield FunctionInfo.from_fn(
        _save,
        description=(
            "Saves a markdown (.md) document to the local project store on disk. "
            "Use this when asked to write up, summarize, or save notes/research/a "
            "description of the current session -- pass a short descriptive "
            "filename ending in '.md' and the full markdown content to save."
        ),
        input_schema=SaveProjectNoteInput,
    )
