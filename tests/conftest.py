"""Load the plugin as a package while retaining real AstrBot imports."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
