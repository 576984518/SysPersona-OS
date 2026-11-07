# -*- coding: utf-8 -*-
"""vault 命令行：seal / unlock / verify / inspect / passwd / recover / import。

口令绝不进 argv：只走 TTY 或 stdin。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from . import api
from .errors import VaultError
from .provider import DEFAULT_PROFILE

PROG = "syspersona vault"


def _line() -> str:
    value = sys.stdin.readline()
    if not value:
        raise VaultError("E_POLICY", "需要从 stdin 读取口令，但没有输入")
    return value.rstrip("\r\n")


def _secret(args, stdin_flag: str, prompt: str) -> str:
    if getattr(args, stdin_flag, False):
        return _line()
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    raise VaultError("E_POLICY", f"非交互环境请使用 --{stdin_flag.replace('_', '-')}")


LABELS = {
    "container": "已写出",
    "bytes": "容器大小",
    "header_bytes": "头部大小",
    "files": "文件数",
    "persona_id": "人格",
    "persona_version": "版本",
    "profile": "KDF 档位",
    "binding": "绑定",
    "home": "目录",
    "container_id": "容器",
    "sealed_at": "封装时间",
    "kernel_sha256": "内核哈希",
    "db_sha256": "记忆库哈希",
    "backup": "已保留备份",
    "imported_from": "迁移来源",
}


def _print(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for key, value in payload.items():
        if key == "ok" or value in (None, "", [], {}):
            continue
        if key == "purged":
            print(f"已清除明文：{len(value)} 个文件")
        elif key == "mnemonic":
            print("恢复助记词（只显示这一次，请离线抄写）：")
            print(f"  {value}")
        else:
            print(f"{LABELS.get(key, key)}：{value}")


def cmd_seal(args) -> int:
    passphrase = _secret(args, "passphrase_stdin", "新口令：")
    if len(passphrase) < 8 and not args.allow_weak:
        raise VaultError("E_POLICY", "口令至少 8 个字符；确需短口令请加 --allow-weak")
    result = api.seal_home(
        Path(args.home),
        Path(args.out),
        passphrase,
        profile=args.profile,
        recovery=args.recovery,
        include_state=not args.no_state,
        binding=args.bind,
        approve_kernel_change=args.approve_kernel_change,
        purge=args.purge,
    )
    _print(result, args.json)
    return 0


def cmd_unlock(args) -> int:
    passphrase = None
    mnemonic = None
    if args.mnemonic_stdin:
        mnemonic = _line()
    else:
        passphrase = _secret(args, "passphrase_stdin", "口令：")
    result = api.unlock_home(Path(args.file), Path(args.home), passphrase, mnemonic=mnemonic, force=args.force)
    _print(result, args.json)
    return 0


def cmd_verify(args) -> int:
    passphrase = None
    mnemonic = None
    if args.mnemonic_stdin:
        mnemonic = _line()
    else:
        passphrase = _secret(args, "passphrase_stdin", "口令：")
    result = api.verify_file(Path(args.file), passphrase, mnemonic=mnemonic)
    _print(result, args.json)
    return 0


def cmd_inspect(args) -> int:
    result = api.inspect_file(Path(args.file))
    _print(result, args.json)
    return 0


def cmd_passwd(args) -> int:
    file = Path(args.file)
    if args.stdin:
        old = _line()
        new = _line()
    else:
        old = getpass.getpass("当前口令：")
        new = getpass.getpass("新口令：")
        if getpass.getpass("再输入一次新口令：") != new:
            raise VaultError("E_POLICY", "两次输入的新口令不一致")
    if len(new) < 8 and not args.allow_weak:
        raise VaultError("E_POLICY", "新口令至少 8 个字符；确需短口令请加 --allow-weak")
    result = api.rotate_passphrase(file, old, new)
    _print(result, args.json)
    return 0


def cmd_recover(args) -> int:
    mnemonic = _line() if args.mnemonic_stdin else getpass.getpass("助记词：")
    result = api.recover_home(Path(args.file), mnemonic, Path(args.out), force=args.force)
    _print(result, args.json)
    return 0


def cmd_import(args) -> int:
    passphrase = _secret(args, "passphrase_stdin", "新口令：")
    if len(passphrase) < 8 and not args.allow_weak:
        raise VaultError("E_POLICY", "口令至少 8 个字符；确需短口令请加 --allow-weak")
    result = api.import_plaintext(
        Path(args.file),
        Path(args.home),
        passphrase,
        profile=args.profile,
        allow_plaintext=args.allow_plaintext,
        force=args.force,
    )
    _print(result, args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="SysPersona 加密容器（M4）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("seal", help="把工作目录封装成加密容器")
    p.add_argument("--home", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--profile", default=DEFAULT_PROFILE, choices=["low", "mid", "high", "scrypt"])
    p.add_argument("--recovery", action="store_true", help="生成 BIP39 恢复助记词")
    p.add_argument("--no-state", action="store_true", help="不封装 state.json")
    p.add_argument("--bind", default="none", choices=["none", "device"])
    p.add_argument("--approve-kernel-change", action="store_true")
    p.add_argument("--purge", action="store_true", help="封装成功后删除明文")
    p.add_argument("--passphrase-stdin", action="store_true")
    p.add_argument("--allow-weak", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_seal)

    p = sub.add_parser("unlock", help="解封到工作目录")
    p.add_argument("--file", required=True)
    p.add_argument("--home", required=True)
    p.add_argument("--passphrase-stdin", action="store_true")
    p.add_argument("--mnemonic-stdin", action="store_true")
    p.add_argument("--force", action="store_true", help="覆盖非空目录（会保留备份）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("verify", help="只校验，不落盘")
    p.add_argument("--file", required=True)
    p.add_argument("--passphrase-stdin", action="store_true")
    p.add_argument("--mnemonic-stdin", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("inspect", help="只看头部元数据（不需要口令）")
    p.add_argument("--file", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("passwd", help="改口令（只重包 KS，不重加密载荷）")
    p.add_argument("--file", required=True)
    p.add_argument("--stdin", action="store_true", help="从 stdin 读两行：旧口令、新口令")
    p.add_argument("--allow-weak", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("recover", help="用助记词恢复")
    p.add_argument("--file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--mnemonic-stdin", action="store_true")
    p.add_argument("--force", action="store_true", help="覆盖非空目录（会保留备份）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("import", help="明文 .syspersona → 加密容器（需显式允许）")
    p.add_argument("--file", required=True)
    p.add_argument("--home", required=True)
    p.add_argument("--allow-plaintext", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--profile", default=DEFAULT_PROFILE, choices=["low", "mid", "high", "scrypt"])
    p.add_argument("--passphrase-stdin", action="store_true")
    p.add_argument("--allow-weak", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except VaultError as exc:
        if getattr(args, "json", False):
            print(json.dumps(exc.as_dict(), ensure_ascii=False))
        detail = f"（{exc.detail}）" if exc.detail and os.environ.get("SYSPERSONA_DEBUG") else ""
        print(f"{exc.code}: {exc.message}{detail}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
