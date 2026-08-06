"""Phase 2 analysis: what the recorded laps actually say.

Four modules, layered bottom-up, none of which touch the database — routes fetch rows and
hand them in, which is what keeps every algorithm here testable against a synthetic lap:

* `resample` — the uniform distance grid everything else is defined on, plus the cumulative
  time delta between two laps;
* `compare` — two laps overlaid on one grid (`GET /api/analysis/compare`);
* `corners` — speed-minimum segmentation and per-corner time loss
  (`GET /api/analysis/corners`);
* `stints` — stint grouping and the least-squares degradation fit
  (`GET /api/analysis/stints`).

The response shapes are frozen in `docs/analysis-api.md`. `f126.web.analysis_routes` is the
only caller that should be assembling them.
"""

from __future__ import annotations

from f126.analysis.compare import LapRef, compare_laps
from f126.analysis.corners import analyse_corners
from f126.analysis.resample import (
    GRID_STEP_M,
    AnalysisError,
    LapTrace,
    ResampledLap,
    cumulative_delta_ms,
    make_grid,
    overlap,
    resample_onto,
    trace_from_rows,
)
from f126.analysis.stints import build_stints, fit_degradation

__all__ = [
    "GRID_STEP_M",
    "AnalysisError",
    "LapRef",
    "LapTrace",
    "ResampledLap",
    "analyse_corners",
    "build_stints",
    "compare_laps",
    "cumulative_delta_ms",
    "fit_degradation",
    "make_grid",
    "overlap",
    "resample_onto",
    "trace_from_rows",
]
