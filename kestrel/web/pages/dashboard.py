"""The / landing page is now Today (kestrel.web.pages.today). This module keeps the `dashboard`
entry in kestrel.web.app._PAGE_MODULES valid by re-exporting that router, and keeps the
`dashboard` coroutine name that kestrel.web.pages.setup wraps for the first-run banner."""

from __future__ import annotations

from kestrel.web.pages.today import router, today

dashboard = today

__all__ = ["dashboard", "router"]
