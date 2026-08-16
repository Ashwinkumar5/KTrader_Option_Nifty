from __future__ import annotations

import sys
from pathlib import Path

SIMULATOR_ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = SIMULATOR_ROOT.parent
for source in (SIMULATOR_ROOT / "src", BOT_ROOT):
    resolved = str(source)
    if resolved not in sys.path:
        sys.path.insert(0, resolved)

from ktrader_simulator.intake.bot_runner import main


if __name__ == "__main__":
    main()
