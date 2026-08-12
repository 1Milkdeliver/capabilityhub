# Optional remote TLS profile

CapabilityHub remains loopback-only by default. Remote deployment is an explicit host
integration using `RemoteTlsControl`; it starts separate data and administration TLS
listeners and requires a client certificate signed by the configured CA.

- The data listener accepts only `/protocol` and `capability.search`, `capability.load`,
  or `capability.execute` envelopes.
- The administration listener accepts only `/admin` management envelopes. Certificate
  fingerprints map to an `admin` audience and fixed roles such as `approver`.
- A data-plane certificate is rejected by the administration listener even when both
  certificates use the same CA. Approvals record the mapped remote principal identity.
- The data adapter is created from the mapped certificate principal for each request, so
  authorization, budgets, references and tenant state use authenticated identity rather than
  client-supplied payload fields.
- TLS 1.2 or newer and client certificates are mandatory. Streaming and cancellation
  remain protocol-negotiated; this terminal HTTP profile rejects modes it cannot deliver.
- Certificate and key paths are operator configuration. Private-key contents are never
  returned by the API, status, errors, or object representations.

Production operators should issue short-lived client certificates, protect private-key
files with operating-system ACLs, rotate the CA under a documented overlap procedure,
and place both listener ports behind explicit network policy. TLS termination must not be
moved to an intermediary unless authenticated client identity is preserved by an equally
strong, reviewed boundary.
