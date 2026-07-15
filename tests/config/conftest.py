from __future__ import annotations

import sys
from pathlib import Path

# Add scripts/config/ directly so `import validate_config` resolves to
# scripts/config/validate_config.py rather than the YAML-only config/ directory
# at the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CONFIG_DIR = REPO_ROOT / "scripts" / "config"
if str(SCRIPTS_CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_CONFIG_DIR))
