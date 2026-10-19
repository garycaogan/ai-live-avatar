"""Placeholder pictures for the tests. Character images are the operator's own assets and are not
shipped, so a fresh clone gets plain stand-ins (same size as the originals); existing files are kept."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def sample_jpg(path: Path, size: tuple[int, int] = (718, 1280)) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        w, h = size
        img = Image.new("RGB", size, (58, 74, 92))
        d = ImageDraw.Draw(img)
        d.ellipse((w * 0.3, h * 0.18, w * 0.7, h * 0.42), fill=(226, 192, 172))
        d.rectangle((w * 0.18, h * 0.46, w * 0.82, h), fill=(36, 38, 48))
        img.save(path, quality=90)
    return path


def ensure_default_avatars() -> None:
    for name in ("xiaoshen.jpg", "pilot.jpg"):
        sample_jpg(ROOT / "assets" / "avatars" / name)
