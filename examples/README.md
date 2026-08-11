# Examples

These examples are local and intentionally avoid process, network, model, and provider calls.

- `manifest-api.json` is a valid JSON-only `v1alpha1` API manifest. It contains no endpoint or credential.
- `validate_manifest.py` parses the manifest, stores its immutable revision, and activates it in an in-memory registry.
- `dashboard.py` runs `DashboardServer` on loopback with a safe, injected status snapshot.

From a source checkout with the package installed, run:

```bash
python examples/validate_manifest.py
python examples/dashboard.py
```

The dashboard is read-only. Production applications must supply their own authentication, status filtering, provider configuration, and lifecycle management.
