"""Extractor protocol: every extractor deterministically emits graph fragments."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..graph import ImpactGraph


class Extractor(ABC):
    """Builds a fragment of the unified graph from one artifact type.

    Extractors are deterministic: no LLM is involved in graph construction.
    """

    name: str = "extractor"

    @abstractmethod
    def extract(self) -> ImpactGraph:
        """Return an ImpactGraph fragment to be merged into the unified graph."""
        raise NotImplementedError
