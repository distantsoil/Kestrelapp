"""dmgbuild settings for the Kestrel disk image window (#198).

dmgbuild runs this file with a `defines` mapping from its `-D name=value` options:
  app          path to Kestrel.app (required)
  background   path to the background image (optional; omitted = plain window)
  volume_icon  path to the .icns shown for the mounted disk (optional)
  format       UDZO (the download) or UDRW (handed to the signing job), default UDZO
  size         image size such as 900m (optional; dmgbuild sizes it when absent)

The layout is written straight into the image's .DS_Store by dmgbuild, so no Finder scripting
runs on the build machine. Window and icon positions match packaging/macos/dmg-background.svg.
"""

from __future__ import annotations

import os

_defines = globals().get("defines", {})

# The window, in points. The background image is drawn at this size (and @2x for Retina).
WINDOW_SIZE = (660, 400)
ICON_SIZE = 128
APP_POSITION = (170, 200)
APPLICATIONS_POSITION = (490, 200)

app = _defines["app"]
files = [app]
symlinks = {"Applications": "/Applications"}

_background = _defines.get("background") or ""
if _background:
    background = _background
_volume_icon = _defines.get("volume_icon") or ""
if _volume_icon:
    icon = _volume_icon

format = _defines.get("format", "UDZO")  # noqa: A001 (the setting name dmgbuild reads)
if format not in ("UDZO", "UDRW"):
    raise ValueError(f"format must be UDZO or UDRW, not {format!r}")
size = _defines.get("size") or None
filesystem = "HFS+"

window_rect = ((200, 120), WINDOW_SIZE)
default_view = "icon-view"
icon_size = ICON_SIZE
text_size = 13
show_icon_preview = False
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
icon_locations = {
    os.path.basename(app): APP_POSITION,
    "Applications": APPLICATIONS_POSITION,
}
