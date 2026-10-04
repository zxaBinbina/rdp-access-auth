"""Load dependencies shipped by the native package, before importing application code."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if (ROOT / 'PACKAGED.json').is_file():
    sys.path.insert(0, str(ROOT / 'vendor'))
