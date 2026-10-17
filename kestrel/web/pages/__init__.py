"""Per-page route modules (SPEC-INTERFACES 2.1). Each page owns one module exposing `router`, so the
Phase 13 fan-out builds pages independently without touching a shared routes file. app.build_app
includes them all."""

from __future__ import annotations
