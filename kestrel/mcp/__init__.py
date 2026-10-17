"""The MCP server (SPEC-INTERFACES 4). fastmcp over SSE on a distinct configurable port (default
9110), exposing the pipeline to the operator's existing Claude/Cowork sessions. Read tools plus a
deliberately small set of action tools, and NONE of them act externally: the same hand-over boundary
that governs the rest of Kestrel governs every tool here."""

from __future__ import annotations
