# SPDX-License-Identifier: Apache-2.0
# Import shim + test helpers for ce-forecaster.py
import sys
from pathlib import Path
import numpy as np
import pandas as pd

# Re-export everything from the main module
sys.path.insert(0, str(Path(__file__).parent))
exec(open(Path(__file__).parent / "ce-forecaster.py").read())

def build_synthetic_df(n_days=60, base_rate=1e-6, slope=0.0, noise=0.05):
    """Generate synthetic CE rate time series for testing."""
    dates = pd.date_range("2026-01-01", periods=n_days * 24, freq="h")
    rng   = np.random.default_rng(42)
    vals  = [max(0.0, base_rate + slope * i / 24 + rng.normal(0, noise * base_rate))
             for i in range(len(dates))]
    return pd.DataFrame({"ds": dates, "y": vals})
