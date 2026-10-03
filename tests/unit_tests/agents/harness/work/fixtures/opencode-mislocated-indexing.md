## Scope

Compare two synthetic four-line pilot field logs (`source-a.md`, `source-b.md`) on offline retrieval and benchmark comparability. Audience: research requester. Evidence limited to these two provided files; no web sources used.

## Findings

`source-a.md` (Pilot A, 2026-09-01) records offline retrieval taking **42 seconds for 12 documents**, all indexed locally [source-a.md:3]. It states "No network requirement was tested" [source-a.md:4]: this is an **untested requirement**, not evidence of an absent network dependency.

`source-b.md` (Pilot B, 2026-09-02) records connected retrieval taking **31 seconds for 12 documents** [source-b.md:3] and states "A network connection was required" [source-b.md:4]. That requirement holds only for the observed Pilot B run; it does **not** establish an architecture-wide network dependency.

Pilot B explicitly cautions: "The conditions differ from Pilot A; this is not a controlled benchmark and does not establish superiority" [source-b.md:4]. The 31s vs 42s gap therefore cannot be attributed to network presence alone, and the two runs are **not** comparable as a controlled benchmark.

## Limitations

Each log is a single four-line synthetic observation; no variance, load, configuration, hardware, or repetition data is provided. Source-a does not prove offline retrieval works without network — only that network was not tested. Source-b's required network is observed-run-specific, not a general conclusion. No other modes or runs were inspected.

## Sources

- **source-a.md** — "Pilot A field log", dated 2026-09-01. Line 3: "offline retrieval took 42 seconds for 12 documents." Line 4: "All 12 documents were indexed locally. No network requirement was tested."
- **source-b.md** — "Pilot B field log", dated 2026-09-02. Line 3: "connected retrieval took 31 seconds for 12 documents." Line 4: "A network connection was required. The conditions differ from Pilot A; this is not a controlled benchmark and does not establish superiority."
