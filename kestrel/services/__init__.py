"""Service-layer helpers that enforce invariants the ORM cannot: notifications are recorded from
Phase 1 (delivery is Phase 16), and a pursuit status never changes without a status_event."""
