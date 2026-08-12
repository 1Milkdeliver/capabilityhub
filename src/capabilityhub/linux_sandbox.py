"""Linux Landlock filesystem allowlist plus libseccomp network denial."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
import sys
import sysconfig
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
    "sendmmsg",
    "recvfrom",
    "recvmsg",
    "recvmmsg",
    "shutdown",
    "socketcall",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
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


class LinuxSandboxApplyError(RuntimeError):
    """Safe stage-only confinement failure for parent-process diagnostics."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(stage)


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
    # libseccomp may inspect kernel state while constructing/loading its filter.
    # Install it before Landlock removes access outside the allow-root. Both
    # restrictions are still active before any provider code executes.
    if deny_network:
        if not capabilities.network:
            raise LinuxSandboxApplyError("seccomp_unavailable")
        try:
            _close_inherited_network_fds()
            _apply_seccomp_network_deny()
        except LinuxSandboxApplyError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise LinuxSandboxApplyError("seccomp_apply_failed") from error
    if filesystem_root is not None:
        if not capabilities.filesystem:
            raise LinuxSandboxApplyError("landlock_unavailable")
        try:
            _apply_landlock(Path(filesystem_root), cast(int, capabilities.landlock_abi))
        except LinuxSandboxApplyError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise LinuxSandboxApplyError("landlock_apply_failed") from error


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
    try:
        selected = root.resolve(strict=True)
    except OSError as error:
        raise LinuxSandboxApplyError("landlock_root_invalid") from error
    if not selected.is_dir() or not selected.is_absolute():
        raise LinuxSandboxApplyError("landlock_root_invalid")
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
        raise LinuxSandboxApplyError("landlock_ruleset_failed")
    try:
        try:
            _add_path_rule(libc, ruleset_fd, selected, handled)
        except OSError as error:
            raise LinuxSandboxApplyError("landlock_root_rule_failed") from error
        for system_path in _system_read_roots():
            try:
                _add_path_rule(libc, ruleset_fd, system_path, _READ_EXECUTE_ACCESS)
            except OSError:
                # Runtime map discovery is conservative and may include deleted,
                # pseudo, or mount-specific files which Landlock cannot safely
                # admit. Skipping such a read-only candidate preserves the
                # default-deny boundary; the descendant execution test proves
                # that the remaining minimal runtime closure is sufficient.
                continue
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise LinuxSandboxApplyError("landlock_no_new_privs_failed")
        if libc.syscall(_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            raise LinuxSandboxApplyError("landlock_restrict_failed")
    finally:
        os.close(ruleset_fd)


def _system_read_roots() -> tuple[Path, ...]:
    candidates = {
        # The dynamic loader may consult this file when starting a descendant.
        # Do not grant all of /etc: provider code must not gain read access to
        # unrelated machine configuration or service credentials.
        Path("/etc/ld.so.cache"),
        Path("/dev/null"),
        Path(sys.executable).resolve(),
    }
    for value in sysconfig.get_paths().values():
        path = Path(value)
        if path.is_absolute():
            candidates.add(path.resolve())
    maps = Path("/proc/self/maps")
    try:
        for line in maps.read_text(encoding="utf-8").splitlines():
            fields = line.split(maxsplit=5)
            if len(fields) == 6 and fields[5].startswith("/"):
                mapped = Path(fields[5])
                if mapped.is_file():
                    candidates.add(mapped.resolve())
    except OSError:
        pass
    return tuple(sorted((path for path in candidates if path.exists()), key=str))


def _close_inherited_network_fds() -> None:
    descriptor_root = Path("/proc/self/fd")
    try:
        descriptors = tuple(descriptor_root.iterdir())
    except OSError as error:
        raise LinuxSandboxApplyError("network_fd_inventory_failed") from error
    for entry in descriptors:
        try:
            descriptor = int(entry.name)
            target = os.readlink(entry)
        except (OSError, ValueError):
            continue
        if descriptor <= 2 or not target.startswith("socket:["):
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise LinuxSandboxApplyError("network_fd_close_failed") from error


def _add_path_rule(libc: Any, ruleset_fd: int, path: Path, access: int) -> None:
    # Landlock rejects directory-only rights such as READ_DIR when the parent
    # fd refers to a regular file. Exact runtime files retain READ_FILE and
    # EXECUTE so the pinned interpreter and loader can start descendants; the
    # ordinary UNIX mode bits still prevent executing non-executable files.
    if path.is_file():
        access &= (1 << 0) | (1 << 2)
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
        raise LinuxSandboxApplyError("seccomp_context_failed")
    try:
        deny = _SCMP_ACT_ERRNO | errno.EPERM
        for name in _NETWORK_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                continue
            if library.seccomp_rule_add(context, deny, number, 0) != 0:
                raise LinuxSandboxApplyError("seccomp_rule_failed")
        if library.seccomp_load(context) != 0:
            raise LinuxSandboxApplyError("seccomp_load_failed")
    finally:
        library.seccomp_release(context)
