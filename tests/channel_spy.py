"""Record what the desktop shell tells its pages (kestrel/desktop/channel.py, D-SEC-61).

The shell used to call pages through pywebview's evaluate_js; it now posts events the pages
fetch. Tests that asserted on those calls read this list instead. Each event is written as the
call it stands for (window.KSplash.fail(...), KUI.frame.closing(), ...), so the assertions read
as before."""

from __future__ import annotations

import json


def spy(monkeypatch) -> list[str]:
    from kestrel.desktop import channel

    said: list[str] = []
    original = channel.Channel.post

    def post(self, name, *args):
        if name in channel.SPLASH_EVENTS:
            said.append(f"window.KSplash && window.KSplash.{name}("
                        f"{', '.join(json.dumps(a) for a in args)})")
        elif name == "state":
            said.append(f"window.KUI && KUI.frame.state({'true' if args[0] else 'false'})")
        elif name == "closing":
            said.append("window.KUI && KUI.frame.closing()")
        else:
            said.append(f"window event {name}")
        return original(self, name, *args)

    monkeypatch.setattr(channel.Channel, "post", post)
    return said
