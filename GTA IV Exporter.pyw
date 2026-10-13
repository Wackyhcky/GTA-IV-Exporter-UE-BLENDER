# Double-click to start the GTA IV Exporter (no console window).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gtaiv_exporter_gui  # noqa: E402

gtaiv_exporter_gui.main()
