"""Ingestion: source adapters produce a canonical RawPosting; normalize.py does the shared
canonicalization (org resolution, comp, remote, dedup, ghost, tokens, embed enqueue) and persists.
Adapters own their platform's field shapes (SPEC.md Appendix A); normalize owns the common logic."""
