"""DB-backed task queue (SPEC.md 4.6). Restart-safe: every unit of work is a task row, and killing
the process at any moment leaves in-flight tasks re-leasable."""

from kestrel.queue.queue import TaskQueue

__all__ = ["TaskQueue"]
