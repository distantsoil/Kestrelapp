"""Reranker features (SPEC-MODULES 5.4): the opportunity embedding reduced to 64 dims by a PCA
fitted on the corpus, plus structured tabular features. The fitted PCA MUST be persisted alongside
the model — without it the 64-dim features are not reproducible and the model silently scores
garbage after a restart.

Categorical features use FIXED vocabularies so the column layout (feature_names) is stable and
reproducible across restarts. An unseen category maps to all-zeros rather than shifting columns. The
soft-signal binaries are read from the opportunity's Tier 0 stage_reason ("soft: id1, id2; …"), so
no rule re-evaluation is needed at train/score time."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from kestrel import paths
from kestrel.db.enums import REMOTE_TYPES, SOURCE_KINDS
from kestrel.llm.embeddings import from_bytes
from kestrel.rules import helpers

_PCA_DIMS = 64
_SIZE_BANDS = ["1-50", "50-100", "100-500", "500-1000", "1000-5000", "5000+"]
_OWNERSHIP = ["pe_backed", "public", "vc_backed", "nonprofit", "government", "private"]
_EVENT_TYPES = [
    "cio_departure",
    "cto_departure",
    "acquisition",
    "divestiture",
    "pe_investment",
    "funding_round",
    "security_incident",
    "control_weakness",
    "compliance_obligation",
    "rapid_it_hiring",
    "expansion",
    "restructuring",
    "other",
]
_SOFT_RE = re.compile(r"soft:\s*([^;]+)")


def soft_signal_ids(track: str) -> list[str]:
    """The ordered soft-signal rule ids for a track, from the packaged filters.yml, so the binary
    columns are stable. Falls back to an empty list if the config cannot be read."""
    try:
        filters = helpers.load_filters(str(paths.config_file("filters.yml")))
    except Exception:
        return []
    return [
        r.get("id")
        for r in (filters.get(track, {}) or {}).get("soft_signals", []) or []
        if r.get("id")
    ]


def _onehot(value, vocab: list[str]) -> list[float]:
    return [1.0 if value == v else 0.0 for v in vocab]


@dataclass
class FeatureSpec:
    """The fixed, serializable description of the tabular columns. Persisted with the PCA so a
    reloaded model reproduces the exact feature layout."""

    track: str
    soft_ids: list[str] = field(default_factory=list)
    size_bands: list[str] = field(default_factory=lambda: list(_SIZE_BANDS))
    ownership: list[str] = field(default_factory=lambda: list(_OWNERSHIP))
    source_kinds: list[str] = field(default_factory=lambda: list(SOURCE_KINDS))
    remote_types: list[str] = field(default_factory=lambda: list(REMOTE_TYPES))
    event_types: list[str] = field(default_factory=lambda: list(_EVENT_TYPES))

    def tabular_names(self) -> list[str]:
        names = [
            "seniority_rank",
            "comp_min",
            "comp_max",
            "comp_known",
            "pre_score",
            "description_tokens",
            "seen_count",
            "ghost_score",
            "days_since_posted",
        ]
        names += [f"soft_{s}" for s in self.soft_ids]
        names += [f"source_{k}" for k in self.source_kinds]
        names += [f"remote_{r}" for r in self.remote_types]
        names += [f"size_{b}" for b in self.size_bands]
        names += [f"own_{o}" for o in self.ownership]
        if self.track == "consulting":
            names += [f"event_{e}" for e in self.event_types]
            names += ["trigger_confidence"]
        return names

    def tabular_row(self, opp, org) -> list[float]:
        comp_min = float(opp.comp_min) if opp.comp_min is not None else 0.0
        comp_max = float(opp.comp_max) if opp.comp_max is not None else 0.0
        comp_known = 1.0 if (opp.comp_source and opp.comp_source != "none") else 0.0
        soft_hits = self._soft_hits(opp.stage_reason)
        row = [
            float(helpers.seniority_rank(opp.title)),
            comp_min / 1000.0,  # scale so tree splits are readable; monotone-invariant anyway
            comp_max / 1000.0,
            comp_known,
            float(opp.pre_score or 0.0),
            float(opp.description_tokens or 0),
            float(opp.seen_count or 0),
            float(opp.ghost_score or 0.0),
            float(min(helpers.age_days(opp.date_posted), 3650)),
        ]
        row += [1.0 if s in soft_hits else 0.0 for s in self.soft_ids]
        # Source kind lives on the Source row; the dataset builder attaches it as opp._source_kind.
        row += _onehot(getattr(opp, "_source_kind", None), self.source_kinds)
        row += _onehot(opp.remote_type, self.remote_types)
        row += _onehot(org.size_band if org else None, self.size_bands)
        row += _onehot(org.ownership if org else None, self.ownership)
        if self.track == "consulting":
            td = opp.trigger_data or {}
            row += _onehot(td.get("event_type"), self.event_types)
            conf = td.get("confidence")
            row.append(float(conf) if isinstance(conf, (int, float)) else 0.0)
        return row

    @staticmethod
    def _soft_hits(stage_reason: str | None) -> set[str]:
        if not stage_reason:
            return set()
        m = _SOFT_RE.search(stage_reason)
        if not m:
            return set()
        return {t.strip() for t in m.group(1).split(",") if t.strip() and t.strip() != "none"}


@dataclass
class FittedFeatures:
    """A fitted feature transform: the PCA plus the tabular spec. Pickled as one artifact."""

    spec: FeatureSpec
    pca: object  # sklearn PCA, or None if the corpus was too small to fit one
    embed_dim: int
    pca_dims: int

    def feature_names(self) -> list[str]:
        return [f"pca_{i}" for i in range(self.pca_dims)] + self.spec.tabular_names()

    def transform(self, opp, org, embedding: np.ndarray | None) -> np.ndarray:
        if self.pca is not None and embedding is not None and embedding.size == self.embed_dim:
            reduced = self.pca.transform(embedding.reshape(1, -1))[0]
        else:
            reduced = np.zeros(self.pca_dims, dtype=float)
        tab = np.asarray(self.spec.tabular_row(opp, org), dtype=float)
        return np.concatenate([np.asarray(reduced, dtype=float), tab])


def fit_features(track: str, embeddings: np.ndarray) -> FittedFeatures:
    """Fit the PCA on the corpus embedding matrix (rows = opportunities). If there are fewer rows or
    columns than the target PCA size, reduce to what is available; if there is nothing, no PCA."""
    from sklearn.decomposition import PCA

    spec = FeatureSpec(track=track, soft_ids=soft_signal_ids(track))
    if embeddings.ndim != 2 or embeddings.shape[0] == 0:
        return FittedFeatures(spec=spec, pca=None, embed_dim=0, pca_dims=_PCA_DIMS)
    n, dim = embeddings.shape
    k = min(_PCA_DIMS, n, dim)
    pca = PCA(n_components=k, random_state=0).fit(embeddings)
    # Pad the effective output to _PCA_DIMS names by recording pca_dims=k (feature_names uses k).
    return FittedFeatures(spec=spec, pca=pca, embed_dim=dim, pca_dims=k)


def embedding_of(blob: bytes | None) -> np.ndarray | None:
    return from_bytes(blob) if blob else None
