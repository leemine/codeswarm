# Runtime instance extension assembly

The existing `ExtensionRegistry` supports both the legacy process singleton and
explicit instances. An instance host supplies a separate callback framework,
configuration and `ExtensionManager(..., config=..., root_dir=...)`. Registry
configuration is copied on construction. The manager's `config` controls search
paths; plugin initialization receives `registry.config`. Pass the same instance
configuration to both; a conflicting explicit manager config is rejected. Omitting manager configuration/root preserves legacy
configuration and directory discovery.

A host can inject services through `register_capability(name, provider,
version="1")`. Names belong to the host; the registry does not define a Projects
or Provider policy. `get_capability(name)` returns the provider or `None`.
`require_capabilities({name: ">=1,<2"})` synchronously raises
`ExtensionCapabilityError` for missing/incompatible services. An empty version
specifier accepts any valid capability version. Duplicate capability names are
rejected. These checks establish availability and compatibility, not an execution
authorization decision; current authorization still belongs at the execution
boundary.

`ExtensionManager(..., required_capabilities=...)` validates requirements after
loading. Optional extension failures are available in `diagnostics`; a missing
required capability prevents successful startup and rolls back owned loads.

## Manifests and compatibility

- `version`: implementation version, parsed independently of capability versions.
- `min_jiuwenswarm_version`: minimum supported release family. An existing final
  floor such as `0.2.5` includes the application's `0.2.5.beta1`; an explicitly
  prerelease floor is compared precisely.
- `dependencies`: preserves the loader's legacy Python distribution semantics.
  Installed versions must satisfy the given packaging specifier. Missing-package
  installation remains the existing behavior; installation/version errors fail
  that extension instead of importing incompatible code. Tests replace the
  installer and never install dependencies.
- `requires_extensions`: optional mapping of extension IDs to implementation
  version specifiers. Manager orders these dependencies before dependants and
  diagnoses cycles; Loader rejects absent or incompatible dependencies.
- `requires_capabilities`: optional mapping of host capability names to contract
  version specifiers, checked before importing the extension entrypoint. These
  services must already be injected or provided by an explicitly ordered
  extension dependency.

No existing `dependencies` entry is reinterpreted as an extension ID.

## Loading and ownership

Loading stages registry writes and callback registration in the loading task.
During loading, `unregister` may only undo that transaction's own staged
callbacks; attempts to remove previously published or borrowed callbacks fail
without changing them.
Other tasks continue seeing the previously published registry until success.
Concurrent/nested lifecycle operations on one registry are rejected, including
load versus close and close versus close across different loaders/managers.
A rejected close consumes no receipts and preserves the manager's owned list
for retry. Closing an earlier load also rebases later cleanup receipts so they
cannot restore an already closed provider. Application plugins retain
the loader's one initialization call; legacy non-application plugins retain
control of their own initialization.

On initialization failure or cancellation, staged entries are discarded and
registered/returned owned resources are shut down in reverse order. A plugin is
responsible for cleaning resources it allocates but neither registers nor returns.
Direct writes to the raw callback framework and arbitrary process side effects
are outside registry transactions; plugins should use `registry.register` and
`registry.unregister` for owned callbacks.

The loader keeps cleanup receipts for every successful load, including modules
that only register callbacks. `shutdown_loaded()` and manager shutdown revoke
owned registrations, remove owned callback wrappers, then shut down owned
resources. Existing borrowed registrations are retained, and later replacement
registrations are not removed. Cleanup is idempotent. Ordinary shutdown reports
cleanup failures after attempting every resource; rollback logs them while
preserving the original initialization/cancellation error.

The Runtime that owns a manager owns its load/close lifecycle. A Runtime borrowing
an already initialized registry only validates/uses it and must not shut down the
external manager. Registry creation does not implicitly create a second process
singleton or acquire the global Runner callback framework.
