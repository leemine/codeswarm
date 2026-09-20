# R1-01 execution construction

Use `parse_execution_config` for a dedicated execution configuration, not model-provider settings. Pass complete snapshots through `ExecutionConfigSource` (explicit > project > default) to `prepare_execution`, together with a host-owned `ExecutionBindingStore` and an authorized subject/session/absolute workspace.

The result is an unstarted core `HarnessEngine`. No existing chat or Team routing is changed in this slice. The Runtime caller will own `start`, the single event consumer and `stop` in R1-02; it must validate HarnessContext identity against the binding at that integration boundary. No provider instances are cached here.

A bound scope retains its original snapshot when defaults change. An explicit attempt to change it fails rather than switching an active execution. `release` removes only the same binding object, protecting a replacement from stale cleanup. The store is process-local and contains provider configuration: never log it or treat it as durable session restore. Persistence, policy adaptation and UI integration remain separate tasks.
