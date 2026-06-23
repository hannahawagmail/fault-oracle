#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# CLI entry point — delegates to the importable module.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from controller_collector import main
if __name__ == "__main__":
    main()
