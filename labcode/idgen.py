"""Object identity generators for the labcode ``_id`` feature.

labcode gives every Object a stable, value-layer identity carried in its view under the
reserved key ``_id`` (see `labcode.objectid` for how it is injected / stamped). An
``IdGenerator`` mints those ids. It is deliberately a small, swappable seam:

* `SeededUuid4Generator` (the default) mints **reproducible** uuid4-shaped ids, keyed by
  a *provenance* string (a node instance + port, or a boundary port) rather than draw
  order -- so two runs of the same workflow produce the same ids, and the wall-clock
  backend's jittering completion order cannot change them. Reproducibility is what makes
  the checked-in example observations stable.
* `RealUuid4Generator` mints fresh random uuid4s (ignoring the key) -- for a real run
  where each physical object should be globally unique.

A generator's `new_id(provenance_key)` is called once per minted Object (an
``objects.create`` output, or a boundary Object input lacking an id); a single instance
serves a whole run so its ids are consistent across both mint sites.
"""

from __future__ import annotations

import random
import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class IdGenerator(Protocol):
    """Mints an Object's ``_id``. `provenance_key` identifies *where* the Object is
    minted (a node instance + port, or a boundary port); a deterministic generator keys
    off it, a random one ignores it."""

    def new_id(self, provenance_key: str) -> str: ...


class SeededUuid4Generator:
    """Reproducible uuid4-shaped ids, derived from ``seed`` + the provenance key.

    Same ``(seed, provenance_key)`` always yields the same id, independent of call order
    -- so ids are stable across runs and unaffected by the labcode backend's wall-clock
    completion-order jitter. The output is a valid version-4 UUID string (the version /
    variant bits are set by `uuid.UUID(int=..., version=4)`), just sourced from a seeded
    PRNG instead of the OS entropy pool."""

    def __init__(self, seed: object = 0) -> None:
        self._seed = seed

    def new_id(self, provenance_key: str) -> str:
        rng = random.Random(f"{self._seed}\x00{provenance_key}")
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))


class RealUuid4Generator:
    """Fresh random uuid4 per Object (the provenance key is ignored). For real runs
    where each physical Object must be globally unique rather than reproducible."""

    def new_id(self, provenance_key: str) -> str:  # noqa: ARG002 - key intentionally unused
        return str(uuid.uuid4())


# The default generator for `lc run`: reproducible, so example outputs are stable. Swap
# in `RealUuid4Generator` (via `labcode_backend_factory(id_generator=...)`) for a real run.
DEFAULT_ID_GENERATOR: IdGenerator = SeededUuid4Generator()
