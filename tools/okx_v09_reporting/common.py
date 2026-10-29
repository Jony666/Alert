from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

class VerificationError(ValueError):
    pass

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()

def sha256(value):
    return hashlib.sha256(value).hexdigest()

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError('duplicate JSON key: ' + key)
        result[key] = value
    return result

def read_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(VerificationError('nonfinite JSON: ' + value)))
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationError('invalid JSON') from exc

def at(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError('timezone required')
        return result.astimezone(timezone.utc)
    except (TypeError, AttributeError, ValueError) as exc:
        raise VerificationError('invalid/timezone-less timestamp') from exc

def now():
    return datetime.now(timezone.utc)

def number(value, name='number'):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise VerificationError('invalid finite ' + name)
    return float(value)

def close(a, b, name, tolerance=1e-6):
    a, b = number(a, name), number(b, name)
    if not math.isclose(a, b, rel_tol=1e-12, abs_tol=tolerance):
        raise VerificationError('financial mismatch: ' + name)

def local_path(value):
    text = str(value)
    if not text or '://' in text or '\x00' in text or '\\' in text or '..' in Path(text).parts:
        raise VerificationError('only explicit local filesystem paths are supported')
    p = Path(text).absolute()
    for candidate in (p, *p.parents):
        if candidate.is_symlink():
            raise VerificationError('symlink destination refused')
    return p

def atomic_directory(destination, files):
    destination = local_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise VerificationError('destination already exists; never overwrite a ledger or report')
    temp = Path(tempfile.mkdtemp(prefix='.' + destination.name + '-', dir=destination.parent))
    try:
        for name, data in files.items():
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts:
                raise VerificationError('unsafe output path')
            target = temp / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        # Rename a fully written directory; an existing nonempty output is never replaced.
        rename_exclusive(temp, destination)
        fd = os.open(destination.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if temp.exists(): shutil.rmtree(temp)
    return destination


def rename_exclusive(source, destination):
    """Linux atomic publish with RENAME_NOREPLACE; no race that replaces an empty directory."""
    if os.name == 'nt':
        os.rename(source, destination)
        return
    import ctypes
    import errno
    libc=ctypes.CDLL(None,use_errno=True)
    rename=getattr(libc,'renameat2',None)
    if rename is None:
        raise VerificationError('safe no-replace directory publication is unsupported on this platform')
    result=rename(-100,os.fsencode(source),-100,os.fsencode(destination),1)
    if result:
        code=ctypes.get_errno()
        if code in (errno.EEXIST,errno.ENOTEMPTY):
            raise VerificationError('destination appeared during commit; no replacement performed')
        raise OSError(code,os.strerror(code))
