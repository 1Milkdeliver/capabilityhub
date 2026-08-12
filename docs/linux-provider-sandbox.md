# Linux provider sandbox

CapabilityHub can enforce a production worker boundary on Linux using two mature
kernel interfaces:

- Landlock restricts the worker and descendants to read/write access under one
  resolved allowlisted root. System and interpreter directories receive read/execute
  access only.
- libseccomp installs an inherited filter that rejects socket creation and related
  network syscalls with `EPERM`.

The restrictions are installed inside the spawned worker after resource limits and
before the provider is invoked. They are irreversible for that worker and inherited by
children. Capability probing checks the running kernel's Landlock ABI and the system
libseccomp library; missing support produces a typed policy denial before a worker is
started. Windows continues to fail closed for filesystem or network confinement:
Job Objects provide CPU, memory and process-tree control, not these boundaries.

The GitHub gate is pinned to `ubuntu-24.04` rather than the moving `ubuntu-latest`
label. Its malicious-provider test requires Landlock and libseccomp, writes inside the
allowlisted root, attempts an outside write and socket creation, and repeats the attacks
from a child process. Missing kernel/library support or any successful escape fails CI.

This is a local worker boundary, not container or VM isolation. It does not claim
protection from kernel vulnerabilities, pre-opened privileged descriptors, or a hostile
host administrator.
