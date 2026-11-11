"""Public response fixtures bundled with the plugin for offline replay."""
from pathlib import Path


CAPTURE_ROOT = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'captures'


def capture_path(name, folder=''):
    path = (CAPTURE_ROOT / folder / name).resolve()
    if not path.is_relative_to(CAPTURE_ROOT):
        raise ValueError('capture must stay inside the bundled fixture directory')
    if not path.is_file():
        raise FileNotFoundError(path)
    return path
