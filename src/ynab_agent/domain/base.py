"""The shared base for every domain model.

Domain models are *frozen* (immutable — a new state is a new value, never a
mutation) and *closed* (``extra="forbid"`` rejects unknown fields, so a typo or
a stale payload cannot smuggle data in). This is the substrate on which illegal
states are made unrepresentable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Frozen(BaseModel):
    """Immutable, closed base model. Hashable for use as set/dict keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")
