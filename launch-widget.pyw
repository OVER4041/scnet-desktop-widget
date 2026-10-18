"""Double-click launcher; keeps local SSH configuration outside distributable code."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
if len(sys.argv) == 1:
    config = ROOT / "widget.local.json"
    if not config.exists():
        config = ROOT / "widget.example.json"
    sys.argv.extend(["--widget-config", str(config)])

from scnet_widget.widget import main

main()
