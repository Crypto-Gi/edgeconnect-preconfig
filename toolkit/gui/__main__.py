"""python3 -m toolkit.gui (or python3 toolkit/gui): start the preconfig GUI (docs/gui/BUILD_PLAN.md step 5)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "scripts"), str(HERE)]  # the GUI modules import the CLI's as top-level modules, as the tests do

import gui_server  # noqa: E402  guard-ok: imported after the sys.path insert

if __name__ == "__main__":
    sys.exit(gui_server.main())
