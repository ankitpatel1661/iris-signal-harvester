"""
Streamlit Community Cloud entry point (its default main-file name).

It only runs the real app in ``ui/app.py``; the deploy form needs no custom path. With no
``IRIS_DB_DSN`` the app shows the committed synthetic sample runs under a persistent banner.
"""

from __future__ import annotations

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "ui" / "app.py"), run_name="__main__")
