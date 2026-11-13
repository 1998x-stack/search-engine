"""Immutable local raw bytes; atomic rename after fsync."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import tempfile

class BlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(content).hexdigest()
        folder = self.root / digest[:2]
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / digest
        if not final.is_file():
            fd, temp_path = tempfile.mkstemp(dir=folder, prefix='.tmp-')
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, final)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
        return digest, str(final.relative_to(self.root))

    def get(self, ref: str) -> bytes:
        if not re_full_sha_ref(ref):
            raise ValueError('Invalid blob reference')
        return (self.root / ref).read_bytes()

def re_full_sha_ref(ref: str) -> bool:
    import re
    return bool(re.fullmatch(r'[a-f0-9]{2}/[a-f0-9]{64}', ref)) and ref[:2] == ref[3:5]
