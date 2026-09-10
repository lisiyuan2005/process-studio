"""Errors that are safe to report over RPC."""

from __future__ import annotations


class WorkerError(Exception):
    """Base class for failures the client is expected to display."""


class InvalidRequest(WorkerError):
    """The request was malformed or referenced something that does not exist."""


class WorkspaceError(WorkerError):
    """The workspace directory could not be opened or written."""
