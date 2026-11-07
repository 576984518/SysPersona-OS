# -*- coding: utf-8 -*-
"""高层流程：封装 / 解封 / 校验 / 改口令 / 恢复 / 迁移。"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from . import container, home as home_mod
from .canonical import canonical
from .container import kernel_subset
from .errors import VaultError
from .provider import DEFAULT_PROFILE, Provider, get_provider


def _provider(provider: Provider | None) -> Provider:
    return provider or get_provider()


def seal_home(
    home: Path,
    out: Path,
    passphrase: str,
    *,
    profile: str = DEFAULT_PROFILE,
    recovery: bool = False,
    include_state: bool = True,
    binding: str = "none",
    binding_material: bytes | None = None,
    approve_kernel_change: bool = False,
    purge: bool = False,
    prior: Path | None = None,
    provider: Provider | None = None,
) -> dict:
    home, out = Path(home), Path(out)
    if not home.is_dir():
        raise VaultError("E_IO", f"工作目录不存在：{home}")
    provider = _provider(provider)
    with home_mod.lock(home):
        home_mod.checkpoint_sqlite(home)
        files, kernel, persona = home_mod.collect(home, include_state)
        prior_path = Path(prior) if prior else (out if out.exists() else None)
        if prior_path is not None:
            previous = container.read_header(prior_path)
            current = hashlib.sha256(canonical(kernel_subset(kernel))).hexdigest()
            if previous.get("persona", {}).get("kernel_sha256") not in (None, current) and not approve_kernel_change:
                raise VaultError("E_POLICY", "内核自上次封装后已变化；需 --approve-kernel-change")
        result = container.seal_container(
            files,
            kernel,
            persona,
            passphrase,
            profile=profile,
            recovery=recovery,
            binding_mode=binding,
            binding_material=binding_material,
            provider=provider,
        )
        home_mod.write_bytes_atomic(out, result.blob)
        _self_check(out, result, passphrase, binding_material, provider)
        removed = home_mod.purge(home, files.keys()) if purge else []

    return {
        "ok": True,
        "container": str(out),
        "bytes": len(result.blob),
        "header_bytes": len(canonical(result.header)),
        "files": len(files),
        "persona_id": persona["id"],
        "persona_version": persona["version"],
        "profile": profile,
        "binding": binding,
        "mnemonic": result.mnemonic,
        "purged": removed,
    }


def _self_check(path: Path, result, passphrase: str, binding_material: bytes | None, provider: Provider) -> None:
    """写出后立刻按用户路径重新打开一次：防止「写成功但打不开」。"""
    opened = container.open_container(
        path, passphrase, binding_material=binding_material, provider=provider
    )
    if opened.header["mac"] != result.header["mac"]:
        raise VaultError("E_INTEGRITY", "写出的容器自检失败")


def unlock_home(
    file: Path,
    home: Path,
    passphrase: str | None = None,
    *,
    mnemonic: str | None = None,
    binding_material: bytes | None = None,
    force: bool = False,
    provider: Provider | None = None,
) -> dict:
    file, home = Path(file), Path(home)
    provider = _provider(provider)
    with home_mod.lock(home):
        result = container.open_container(
            file,
            passphrase,
            mnemonic=mnemonic,
            binding_material=binding_material,
            provider=provider,
        )
        backup = home_mod.extract_atomic(result.files, home, force=force)
    return {
        "ok": True,
        "home": str(home),
        "files": len(result.files),
        "persona_id": result.header["persona"]["id"],
        "persona_version": result.header["persona"]["version"],
        "container_id": result.header["container_id"],
        "backup": backup,
        "db_sha256": hashlib.sha256(result.files["memory.db"]).hexdigest() if "memory.db" in result.files else None,
    }


def verify_file(
    file: Path,
    passphrase: str | None = None,
    *,
    mnemonic: str | None = None,
    binding_material: bytes | None = None,
    provider: Provider | None = None,
) -> dict:
    provider = _provider(provider)
    result = container.open_container(
        file,
        passphrase,
        mnemonic=mnemonic,
        binding_material=binding_material,
        provider=provider,
    )
    return {
        "ok": True,
        "persona_id": result.header["persona"]["id"],
        "persona_version": result.header["persona"]["version"],
        "container_id": result.header["container_id"],
        "sealed_at": result.manifest.get("sealed_at"),
        "files": len(result.files),
        "kernel_sha256": result.manifest["kernel"]["sha256"],
        "db_sha256": hashlib.sha256(result.files["memory.db"]).hexdigest() if "memory.db" in result.files else None,
    }


def inspect_file(file: Path) -> dict:
    header = container.read_header(Path(file))
    return {
        "ok": True,
        "format": header.get("format"),
        "container_id": header.get("container_id"),
        "created_at": header.get("created_at"),
        "persona": header.get("persona"),
        "kdf": {"id": header["kdf"]["id"], "profile": header["kdf"].get("profile"), "params": header["kdf"]["params"]},
        "binding": header.get("binding"),
        "wraps": [wrap["id"] for wrap in header.get("wraps", [])],
        "recovery": header.get("recovery"),
    }


def rotate_passphrase(
    file: Path,
    old_passphrase: str,
    new_passphrase: str,
    *,
    binding_material: bytes | None = None,
    provider: Provider | None = None,
) -> dict:
    return container.rotate_passphrase(
        Path(file),
        old_passphrase,
        new_passphrase,
        binding_material=binding_material,
        provider=_provider(provider),
    )


def recover_home(
    file: Path,
    mnemonic: str,
    home: Path,
    *,
    force: bool = False,
    provider: Provider | None = None,
) -> dict:
    return unlock_home(file, home, mnemonic=mnemonic, force=force, provider=_provider(provider))


def import_plaintext(
    bundle: Path,
    home: Path,
    passphrase: str,
    *,
    profile: str = DEFAULT_PROFILE,
    allow_plaintext: bool = False,
    force: bool = False,
    provider: Provider | None = None,
) -> dict:
    """明文 .syspersona（v0.7 zip）→ 工作目录 → 加密容器。

    默认拒绝：允许明文导入等于允许把「降级」当成正常路径。
    """
    if not allow_plaintext:
        raise VaultError("E_POLICY", "明文包默认拒绝导入；确需迁移请显式 --allow-plaintext")
    bundle, home = Path(bundle), Path(home)
    if (home / home_mod.DB_NAME).exists() and not force:
        raise VaultError("E_POLICY", f"{home} 已有数据；确需覆盖请加 --force")
    try:
        with zipfile.ZipFile(bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            data = {name: archive.read(name) for name in archive.namelist() if name != "manifest.json"}
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
        raise VaultError("E_FORMAT", "不是合法的明文迁移包")
    if manifest.get("schema") != "syspersona/bundle@1":
        raise VaultError("E_FORMAT", "不是 syspersona/bundle@1")
    if hashlib.sha256(data["memory.db"]).hexdigest() != manifest.get("db_sha256"):
        raise VaultError("E_INTEGRITY", "明文包校验失败")
    for name, blob in data.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise VaultError("E_FORMAT", f"非法路径：{name}")
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    summary = seal_home(home, home.with_suffix(".vault"), passphrase, profile=profile, provider=provider)
    summary["imported_from"] = str(bundle)
    return summary
