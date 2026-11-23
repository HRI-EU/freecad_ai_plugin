# hda_software

FreeCAD tools for AI-assisted mechanical design. They run inside FreeCAD's
Python console/Macro system and talk to a backend `fcserver` for analysis,
rendering and LLM feedback.

## Contents

- `AI_design_assistant_v3.py` — a dock panel that exports the active model to
  STL, sends it to the server and shows metrics, renders, ratings and LLM
  feedback.
- `Robot_Platform_Generator_v3.FCMacro` — a parametric robotic-platform
  generator (layout, wheels and dimensions), grouped under `RobotPlatform` and
  checked against a 300 × 300 × 200 mm bounding box.

## Requirements

- FreeCAD (PySide6 / PySide2 / PyQt5, whichever FreeCAD bundles)
- A reachable `fcserver` instance
- `requests`

## Usage

Run either file from FreeCAD's Macro menu. The design assistant docks on the
right of the main window; set the server address in the panel.

## License

BSD 3-Clause — see [LICENSE](LICENSE).
