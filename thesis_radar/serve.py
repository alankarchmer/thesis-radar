"""`radar serve`: the dashboard on localhost with write-back.

STUB: implemented by the serve work package.
"""

from __future__ import annotations

from .app import App


def serve(app: App, *, port: int, open_browser: bool = True) -> None:
    raise NotImplementedError
