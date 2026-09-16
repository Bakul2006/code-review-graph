"""Errors the CLI is expected to report as one line, never as a traceback.

Every exception here means "the tool cannot do the job, and it knows why".
``cli.main`` turns each of them into the house failure style — a single
``Error: ...`` line on stderr and exit 1 — so the message is the only thing
the user sees. The MCP tools turn them into ``{"status": "error", ...}``.

Keep the ``str()`` of each one short enough to read on a terminal line and
specific enough to act on: what is wrong, where, and what to do about it.
"""

from __future__ import annotations


class CodeReviewGraphError(Exception):
    """Base class for every self-explaining failure in this package."""


class GraphStoreError(CodeReviewGraphError):
    """The graph database cannot be opened, read, or written.

    Raised for a corrupt or foreign ``graph.db``, one written by a newer
    schema than this installation understands, and for a data directory the
    process may not write to.
    """


class GraphRootMismatchError(GraphStoreError):
    """The graph on disk was built for a different repository root.

    Answering from it would serve one repository another repository's
    symbols, so every consumer refuses instead.
    """


class ChangeDiscoveryError(CodeReviewGraphError, RuntimeError):
    """The set of changed files or lines could not be determined.

    Distinct from "there are no changes" on purpose: a review gate keyed on
    an all-clear must not pass a pull request that was never looked at.
    Raised when the VCS binary is missing, times out, or fails.

    Also a ``RuntimeError``: ``get_changed_files(strict=True)`` has raised
    one since before this class existed, and callers that catch
    ``RuntimeError`` there keep working.
    """


__all__ = [
    "ChangeDiscoveryError",
    "CodeReviewGraphError",
    "GraphRootMismatchError",
    "GraphStoreError",
]
