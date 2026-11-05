"""A tiny pytest plugin: run the tests in a seeded random order (used by scripts/soak.py when
pytest-randomly is not installed). Seed comes from the SOAK_SHUFFLE_SEED environment variable."""

from __future__ import annotations

import os
import random


def pytest_collection_modifyitems(config, items):
    seed = int(os.environ.get("SOAK_SHUFFLE_SEED", "1"))
    random.Random(seed).shuffle(items)


def pytest_report_header(config):
    return f"soak shuffle seed: {os.environ.get('SOAK_SHUFFLE_SEED', '1')}"
