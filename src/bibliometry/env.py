"""Where the project folder is, locally and on Google Colab.

Locally the notebooks live in ``notebooks/`` and the project folder (holding
``config.yaml`` and ``data/``) is its parent. On Colab the files would vanish with the
session, so the project folder goes to Google Drive when it can be mounted.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

COLAB_FOLDER = "bibliometry"


def in_colab() -> bool:
    return "google.colab" in sys.modules


def project_root(use_drive: bool = True) -> Path:
    """Find (and ``chdir`` into) the project folder; returns it."""
    if in_colab():
        base = Path("/content")
        if use_drive:
            from google.colab import drive  # type: ignore[import-not-found]

            drive.mount("/content/drive")
            base = Path("/content/drive/MyDrive")
        root = base / COLAB_FOLDER
    else:
        cwd = Path.cwd()
        root = cwd.parent if cwd.name == "notebooks" else cwd
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    print(f"Project folder: {root}")
    return root


def offer_download(path: Path) -> None:
    """On Colab, send a file to the browser; locally, just say where it is."""
    if in_colab():
        from google.colab import files  # type: ignore[import-not-found]

        files.download(str(path))
    else:
        print(f"File ready: {path}")


def upload_roster(root: Path) -> str:
    """On Colab, ask for a file from your computer and save it in ``data/input/``.

    Returns the saved path relative to ``root`` (empty string if nothing was chosen).
    """
    if not in_colab():
        print("Upload is for Colab; locally, put the file in the project folder.")
        return ""
    from google.colab import files  # type: ignore[import-not-found]

    uploaded = files.upload()
    if not uploaded:
        return ""
    name, content = next(iter(uploaded.items()))
    target = Path(root) / "data" / "input" / Path(name).name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    relative = str(target.relative_to(root))
    print(f"Saved {relative}")
    return relative
