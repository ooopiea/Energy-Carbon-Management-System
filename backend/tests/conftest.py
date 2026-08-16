from __future__ import annotations

import sys
import os
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

# Unit and integration tests are deterministic and must never consume a real key
# from the developer's project-local .env. Tests that exercise GLM inject a mock.
for key_name in ("ZAI_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY", "ENERGY_AUTO_APPROVE"):
    os.environ[key_name] = ""
os.environ["ENERGY_AUTO_APPROVE"] = "false"
