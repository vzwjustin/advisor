"""Optional local web dashboard — lazy-imported.

Importing this subpackage has no side effects beyond exposing :func:`run_server`.
The CLI base path (``advisor.__main__``) deliberately does NOT import from
``advisor.web`` at module load; only the ``ui`` subcommand handler does, so
users who never touch the dashboard pay zero cost.

The server itself is built on :mod:`http.server` from the standard library —
no Flask, no FastAPI, no build step. That keeps the "pure CLI, zero new
dependencies" guarantee intact while still providing a browseable UI on
``http://127.0.0.1:<port>`` when invoked with ``advisor ui <target>``.
"""

from __future__ import annotations

from .server import DEFAULT_HOST, DEFAULT_PORT, build_app_state, run_server

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "build_app_state", "run_server"]
