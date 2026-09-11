"""M3: testbed, load generation, metrics, plots, report.

See CLAUDE.md §4. Nothing in this package may be imported by M1's or M2's code —
in particular ``true_class`` must never leave here except as an opaque
passthrough on the wire (§3.8).
"""

from __future__ import annotations
