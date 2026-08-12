# Compatibility policy

CapabilityHub publishes transport compatibility separately from manifest migration.

- `capabilityhub.io/v1alpha1` is the only currently supported transport API version. It was
  introduced on 2026-08-11.
- Unknown or pre-release client versions fail closed with `api_version_unsupported`; they are
  never guessed or silently upgraded.
- Before a supported version can be removed, its release record must publish both a deprecation
  date and a migration target. The sunset date must be at least 180 days later.
- During that window the version remains accepted and is reported as deprecated. On and after
  sunset it is rejected deterministically.
- Optional unknown features are ignored and reported; unknown required features are rejected.
- Legacy manifest documents are not transport versions. `migrate-manifest` performs an explicit,
  idempotent `v1alpha0` document migration and preserves extensions; it never changes active state.

`CompatibilityPolicy`, the protocol handshake, and the shared adapter conformance tests are the
executable source of truth. A future release must update the policy record, migration tooling,
tests, and this document in the same change.
