"""The double-clickable desktop application (SPEC-INTERFACES 1.1-1.5). A transcription of the
process model proven in the Phase -1 spike (spike/FINDINGS.md), not a rediscovery: pywebview on Edge
WebView2 the machine already has; uvicorn, the worker, and the scheduler on one loop from the
lifespan, a Local\\ single-instance mutex, and close-to-tray so multi-hour runs survive the window
being closed."""

from __future__ import annotations
