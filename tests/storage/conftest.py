from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPTS_CONFIG_DIR = SCRIPTS_DIR / "config"
SCRIPTS_STORAGE_DIR = SCRIPTS_DIR / "storage"

for path in (SCRIPTS_CONFIG_DIR, SCRIPTS_DIR, str(SCRIPTS_STORAGE_DIR.parent)):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)
