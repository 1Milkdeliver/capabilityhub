"""Linux Landlock filesystem allowlist plus libseccomp network denial."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000
_READ_EXECUTE_ACCESS = (1 << 0) | (1 << 2) | (1 << 3)
_NETWORK_SYSCALLS = (
    "socket",
    "socketpair",
    "connect",
    "bind",
    "listen",
    "accept",
    "accept4",
    "sendto",
    "sendmsg",
    "recvfrom",
    "recvmsg",
)


class _RulesetAttr(ctypes.Structure):
    _fields_ = (("handled_access_fs", ctypes.c_uint64),)


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


@dataclass(frozen=True, slots=True)
class LinuxSandboxCapabilities:
    landlock_abi: int | None
    libseccomp: bool
    filesystem: bool
    network: bool
    reason_code: str


def probe_linux_sandbox() -> LinuxSandboxCapabilities:
    if os.name != "posix" or platform.system() != "Linux":
        return LinuxSandboxCapabilities(None, False, False, False, "linux_required")
    if platform.machine().lower() not in {"x86_64", "amd64", "aarch64", "arm64"}:
        return LinuxSandboxCapabilities(None, False, False, False, "architecture_unsupported")
    abi = _landlock_abi()
    seccomp = ctypes.util.find_library("seccomp") is not None
    return LinuxSandboxCapabilities(
        landlock_abi=abi,
        libseccomp=seccomp,
        filesystem=abi is not None and abi >= 1,
        network=seccomp,
        reason_code=(
            "linux_landlock_seccomp_available"
            if abi is not None and abi >= 1 and seccomp
            else "linux_landlock_unavailable"
            if abi is None or abi < 1
            else "linux_libseccomp_unavailable"
        ),
    )


def apply_linux_sandbox(
    *,
    filesystem_root: str | None,
    deny_network: bool,
) -> None:
    """Apply irreversible restrictions to the calling worker and descendants."""

    capabilities = probe_linux_sandbox()
    if filesystem_root is not None:
        if not capabilities.filesystem:
            raise RuntimeError("landlock unavailable")
        _apply_landlock(Path(filesystem_root), cast(int, capabilities.landlock_abi))
    if deny_network:
        if not capabilities.network:
            raise RuntimeError("libseccomp unavailable")
        _apply_seccomp_network_deny()


def _landlock_abi() -> int | None:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(),
        ctypes.c_size_t(0),
        ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    return int(result) if result >= 0 else None


def _handled_access(abi: int) -> int:
    highest_bit = 12 if abi == 1 else 13 if abi == 2 else 14 if abi < 5 else 15
    return (1 << (highest_bit + 1)) - 1


def _apply_landlock(root: Path, abi: int) -> None:
    selected = root.resolve(strict=True)
    if not selected.is_dir() or not selected.is_absolute():
        raise RuntimeError("sandbox root must be an existing absolute directory")
    libc = ctypes.CDLL(None, use_errno=True)
    handled = _handled_access(abi)
    ruleset_attr = _RulesetAttr(handled)
    ruleset_fd = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "landlock ruleset creation failed")
    try:
        _add_path_rule(libc, ruleset_fd, selected, handled)
        for system_path in _system_read_roots():
            _add_path_rule(libc, ruleset_fd, system_path, _READ_EXECUTE_ACCESS)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "no_new_privs failed")
        if libc.syscall(_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock restrict_self failed")
    finally:
        os.close(ruleset_fd)


def _system_read_roots() -> tuple[Path, ...]:
    candidates = {
        Path("/usr"),
        Path("/bin"),
        Path("/lib"),
        Path("/lib64"),
        # The dynamic loader may consult this file when starting a descendant.
        # Do not grant all of /etc: provider code must not gain read access to
        # unrelated machine configuration or service credentials.
        Path("/etc/ld.so.cache"),
        Path(sys.executable).resolve().parent.parent,
    }
    return tuple(sorted((path for path in candidates if path.exists()), key=str))


def _add_path_rule(libc: Any, ruleset_fd: int, path: Path, access: int) -> None:
    # Landlock rejects directory-only rights such as READ_DIR when the parent
    # fd refers to a regular file.  The only file currently admitted here is
    # the dynamic-loader cache, which needs read access and nothing else.
    if path.is_file():
        access &= 1 << 2
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_PATH", 0)
    path_fd = os.open(path, flags)
    try:
        rule = _PathBeneathAttr(access, path_fd)
        result = libc.syscall(
            _LANDLOCK_ADD_RULE,
            ruleset_fd,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(rule),
            0,
        )
        if result != 0:
            raise OSError(ctypes.get_errno(), "landlock path rule failed")
    finally:
        os.close(path_fd)


def _apply_seccomp_network_deny() -> None:
    library_name = ctypes.util.find_library("seccomp")
    if library_name is None:
        raise RuntimeError("libseccomp unavailable")
    library = ctypes.CDLL(library_name, use_errno=True)
    library.seccomp_init.argtypes = (ctypes.c_uint32,)
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    )
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = (ctypes.c_void_p,)
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = (ctypes.c_void_p,)
    context = library.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise RuntimeError("seccomp filter creation failed")
    try:
        deny = _SCMP_ACT_ERRNO | errno.EPERM
        for name in _NETWORK_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                continue
            if library.seccomp_rule_add(context, deny, number, 0) != 0:
                raise RuntimeError("seccomp rule creation failed")
        if library.seccomp_load(context) != 0:
            raise RuntimeError("seccomp filter load failed")
    finally:
        library.seccomp_release(context)
