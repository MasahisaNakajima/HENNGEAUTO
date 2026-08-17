from datetime import datetime
from pathlib import Path


def now_str() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
