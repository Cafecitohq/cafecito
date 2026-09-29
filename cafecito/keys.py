"""Lease-key granularity — pure functions, no imports.

A leaf module on purpose. `guard` answers "does this lease cover this path?" on
every edit an agent makes, and reaching these through `engine` would drag in the
whole landing pipeline: importing `cafecito.cli` costs ~100ms, importing the
package alone costs ~0.2ms. The guard's budget is 120ms for the entire call.

`engine` re-exports both names, so `from .engine import key_path` still works.
"""

from __future__ import annotations


def key_path(key: str) -> str:
    """The repo path a lease key covers: `file:<path>` and the oracle's
    `<lang>:<path>::<qual>` both map to <path>; anything else covers itself."""
    if key.startswith("file:"):
        return key[5:]
    head, sep, _ = key.partition("::")
    if sep and ":" in head:
        return head.split(":", 1)[1]
    return key


def keys_overlap(a: str, b: str) -> bool:
    """Granularity-aware lease overlap. Identical keys overlap; a `file:` key
    overlaps every key on its path (symbol leases live inside it); two
    distinct symbols in one file do NOT overlap — symbol-disjoint writers
    commute, so their leases must not contend either."""
    if a == b:
        return True
    if key_path(a) != key_path(b):
        return False
    return a.startswith("file:") or b.startswith("file:")
