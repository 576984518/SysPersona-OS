# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：工作目录：收集、SQLite 生命周期、原子解包、单写者锁（§10、§17）。
"""工作目录：收集、SQLite 生命周期、原子解包、单写者锁（§10、§17）。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .errors import VaultError

DB_NAME = "memory.db"
CONFIG_NAME = "config.json"
PERSONA_DIR = "persona"
KERNEL_FILE = "kernel.json"
TRAITS_FILE = "traits.json"
STATE_FILE = "state.json"

LOCK_STALE_SECONDS = 15 * 60


def collect(home: Path, include_state: bool = True) -> tuple[dict[str, bytes], dict, dict]:
    """读取工作目录；返回 (files, kernel, persona)。"""
    home = Path(home)
    files: dict[str, bytes] = {}
    directory = home / PERSONA_DIR
    if directory.is_dir():
        kernel_path = directory / KERNEL_FILE
        if not kernel_path.exists():
            raise VaultError("E_FORMAT", "缺少 persona/kernel.json")
        for name in (KERNEL_FILE, TRAITS_FILE, STATE_FILE):
            path = directory / name
            if path.exists() and (include_state or name != STATE_FILE):
                files[f"{PERSONA_DIR}/{name}"] = path.read_bytes()
        kernel = _load_json(kernel_path, "内核")
    else:
        path = home / "persona.json"
        if not path.exists():
            raise VaultError("E_FORMAT", "找不到人格文件")
        files["persona.json"] = path.read_bytes()
        kernel = _load_json(path, "人格")

    if (home / DB_NAME).exists():
        files[DB_NAME] = (home / DB_NAME).read_bytes()
    if (home / CONFIG_NAME).exists():
        files[CONFIG_NAME] = (home / CONFIG_NAME).read_bytes()

    snapshots = home / "snapshots"
    if snapshots.is_dir():
        entries = sorted(snapshots.glob("*.json"))
        if len(entries) > 500:
            raise VaultError("E_POLICY", "快照数量过多（>500），请先清理")
        for path in entries:
            files[f"snapshots/{path.name}"] = path.read_bytes()

    persona = {"id": kernel.get("id"), "version": kernel.get("version")}
    if not persona["id"] or not persona["version"]:
        raise VaultError("E_FORMAT", "人格缺少 id / version")
    return files, kernel, persona


def _load_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VaultError("E_FORMAT", f"{label}不是合法 JSON", str(exc))
    if not isinstance(value, dict):
        raise VaultError("E_FORMAT", f"{label}必须是对象")
    return value


def checkpoint_sqlite(home: Path) -> None:
    """封装前必须做的三件事：checkpoint → 关连接 → 让旁路文件消失。"""
    db = Path(home) / DB_NAME
    if not db.exists():
        return
    try:
        conn = sqlite3.connect(db, timeout=10)
    except sqlite3.Error as exc:
        raise VaultError("E_BUSY", "数据库被占用", str(exc))
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        raise VaultError("E_BUSY", "数据库忙，无法 checkpoint", str(exc))
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists() and sidecar.stat().st_size == 0:
            sidecar.unlink(missing_ok=True)


def purge(home: Path, names) -> list[str]:
    """只删收集过的明文文件、SQLite 旁路文件与快照。"""
    removed: list[str] = []
    home = Path(home)
    for name in names:
        path = home / name
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if candidate.exists() and candidate.is_file():
                candidate.unlink()
                removed.append(str(candidate))
    snapshots = home / "snapshots"
    if snapshots.is_dir():
        shutil.rmtree(snapshots, ignore_errors=True)
        removed.append(str(snapshots))
    return removed


def write_bytes_atomic(path: Path, blob: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(blob)
        fd = os.open(tmp, os.O_RDWR)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def extract_atomic(files: dict[str, bytes], home: Path, force: bool = False) -> str | None:
    """写暂存目录 → 校验 → 原子替换；目标已有数据时必须 --force，且保留备份。"""
    home = Path(home)
    staging = home.with_name(f".{home.name}.unseal-{uuid.uuid4().hex}")
    backup: Path | None = None
    if home.exists() and any(home.iterdir()):
        if not force:
            raise VaultError("E_POLICY", f"{home} 已有数据；确需覆盖请加 --force（会保留备份）")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = home.with_name(f"{home.name}.backup-{stamp}-{uuid.uuid4().hex[:6]}")
        os.replace(home, backup)
    elif home.exists():
        home.rmdir()
    try:
        staging.mkdir(parents=True)
        for name, blob in files.items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or name.startswith("."):
                raise VaultError("E_FORMAT", f"非法路径：{name}")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        os.replace(staging, home)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and not home.exists():
            os.replace(backup, home)
        raise
    return str(backup) if backup else None


@contextmanager
def lock(home: Path):
    """单写者锁；锁文件放在目标目录旁边，目标不存在也能加锁。"""
    home = Path(home)
    lock_path = home.with_name(f".{home.name}.vault.lock")
    handle = None
    try:
        for _ in range(2):
            try:
                handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                age = time.time() - lock_path.stat().st_mtime
                if age > LOCK_STALE_SECONDS:
                    lock_path.unlink(missing_ok=True)
                    continue
                raise VaultError("E_BUSY")
        if handle is None:
            raise VaultError("E_BUSY")
        os.write(handle, json.dumps({"pid": os.getpid(), "at": time.time()}).encode("utf-8"))
        yield
    finally:
        if handle is not None:
            os.close(handle)
        lock_path.unlink(missing_ok=True)
