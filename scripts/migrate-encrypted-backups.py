from __future__ import annotations

import os
from pathlib import Path

from moneygun_api.operations import _decrypt_backup, _encrypt_backup


def main() -> int:
    root = Path("data/backups").resolve()
    root.mkdir(parents=True, exist_ok=True)
    sources = sorted(root.glob("moneygun-*.sqlite3"))
    migrated = 0
    for source in sources:
        source = source.resolve()
        if source.parent != root or not source.name.startswith("moneygun-"):
            raise RuntimeError("Backup migration target escaped the allowed directory.")
        target = source.with_name(f"{source.name}.aes")
        partial = target.with_name(f"{target.name}.partial")
        if target.exists() or partial.exists():
            raise RuntimeError(f"Encrypted target already exists: {target.name}")
        plaintext = source.read_bytes()
        if not plaintext.startswith(b"SQLite format 3"):
            raise RuntimeError(f"Source is not a SQLite backup: {source.name}")
        encrypted = _encrypt_backup(plaintext)
        if _decrypt_backup(encrypted) != plaintext:
            raise RuntimeError(f"Encryption verification failed: {source.name}")
        partial.write_bytes(encrypted)
        os.replace(partial, target)
        if _decrypt_backup(target.read_bytes()) != plaintext:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"Written backup verification failed: {source.name}")
        source.unlink()
        migrated += 1
        print(f"Encrypted and verified: {target.name}")
    print(f"Migration complete: {migrated} plaintext backup(s) converted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
