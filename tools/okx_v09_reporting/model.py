"""Load only the independently pinned model, never executable files from an Artifact."""
import importlib
from pathlib import Path
import sys
from .common import VerificationError, read_json, sha256

REGISTRY = Path(__file__).with_name('registry')
REGISTRY_HASHES = {
    'freeze.json': 'cd7bc5b00815385bbe2bd967abd92145bc14163b07ffd3966573f96d7f655bbe',
    'anchor.json': 'ba429a27a5b472e1dfe51c142ab7a769875f6a6166b2f1f6f520c870e44d3b17',
    'artifact-profile.json': 'fd3d57adf6934bd2e8479513bb53902101fa386bf032f70f4d1e51795dde472e',
}

def registry():
    values = []
    for name in ('freeze.json', 'anchor.json', 'artifact-profile.json'):
        raw = (REGISTRY / name).read_bytes()
        if sha256(raw) != REGISTRY_HASHES[name]:
            raise VerificationError('unreviewed registry version: ' + name)
        values.append(read_json(raw))
    return tuple(values)

class Model:
    def __init__(self, backtest_path):
        self.freeze, self.anchor, self.profile = registry()
        base = Path(backtest_path).absolute()
        root = base / 'v09'
        expected = self.freeze['model_hashes']
        actual = {str(p.relative_to(root)) for p in [*root.glob('*.py'), *(root/'phase3').glob('*.py'), *(root/'forward').glob('*.py')]}
        if actual != set(expected):
            raise VerificationError('frozen model file set mismatch')
        for name, digest in expected.items():
            path = root / name
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or sha256(path.read_bytes()) != digest:
                raise VerificationError('untrusted model source: ' + name)
        loaded = sys.modules.get('v09')
        if loaded and Path(loaded.__file__).resolve().parent != root.resolve():
            raise VerificationError('another v09 module is already loaded')
        sys.path.insert(0, str(base))
        self.state = importlib.import_module('v09.forward.state')
        self.report = importlib.import_module('v09.forward.report')
        self.engine = importlib.import_module('v09.forward.engine')
        self.freeze_module = importlib.import_module('v09.forward.freeze')
        self.freeze_module.verify(self.freeze, root)
