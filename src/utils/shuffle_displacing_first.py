# shuffle_displacing_first.py
from __future__ import annotations
import random
from typing import MutableSequence, TypeVar, Union

T = TypeVar("T")
_RngLike = Union[None, int, random.Random]

def _normalize_rng(rng: _RngLike) -> random.Random:
    if isinstance(rng, random.Random):
        return rng
    if isinstance(rng, int):
        return random.Random(rng)
    return random.Random()

def shuffle_displacing_first_inplace(seq: MutableSequence[T], *, rng: _RngLike = None) -> None:
    """Guarantee original first element leaves index 0; no-op for len < 2."""
    n = len(seq)
    if n < 2:
        return
    rnd = _normalize_rng(rng)
    j0 = rnd.randrange(1, n)               # forces displacement of the first element
    seq[0], seq[j0] = seq[j0], seq[0]
    for i in range(1, n):
        j = rnd.randrange(i, n)
        seq[i], seq[j] = seq[j], seq[i]
