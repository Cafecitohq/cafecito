# oracle — symbol-level write sets (v0.1)

[writeset.py](writeset.py): changed lines mapped to the innermost enclosing
def/class via `ast` (Python); everything else degrades to whole-file
granularity. Uncertainty always widens the write set. Validated in phase0:
93.8–100% of concurrent changes across 10 repos are write-set-disjoint.

tree-sitter extractors for more languages are a welcome contribution.
