"""Build a clean AstrBot upload ZIP with plugin files at its root."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
destination = root / "dist" / "astrbot_plugin_flj_finder.zip"
destination.parent.mkdir(exist_ok=True)
files = [
    "main.py",
    "search.py",
    "google_api.py",
    "x_recovery.py",
    "title_llm.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
    "CHANGELOG.md",
    "logo.png",
    "LICENSE",
    "assets/success.png",
    "assets/README.md",
    "assets/readme/workflow.svg",
]
with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
    for name in files:
        archive.write(root / name, arcname=name)
print(destination)
