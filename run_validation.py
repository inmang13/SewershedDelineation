"""
CLI runner: Phase 8 — boundary-method validation sweep.

Usage:
    python run_validation.py --config config.yaml --sweep
    python run_validation.py --sweep --methods morph_close,concave --sel 50,100,150

Sweeps each candidate boundary method x selection_radius x (close_r|ratio) against
the 25-site truth set, prints the IoU summary table, writes the full per-site
table to output/validation_sweep.csv, and — once the IoU floor is set in config —
writes output/validation_overlay.gpkg for the winning combo. See src/validation.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from validation import main   # noqa: E402

if __name__ == "__main__":
    main()
