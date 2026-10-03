## Scope

Compares two synthetic four-line field logs — `source-a.md` (Pilot A, 2026-09-01) and `source-b.md` (Pilot B, 2026-09-02) — on offline retrieval and benchmark comparability. Only the two supplied files were inspected; no web searches.

## Findings / Comparison

Pilot A recorded offline retrieval of 42 seconds for 12 documents (source-a.md:3); all 12 were indexed locally (source-a.md:4). Its log states "No network requirement was tested." (source-a.md:4). An untested requirement is unknown, not an absent dependency: the inspected sources do not report whether Pilot A's architecture lacks a network dependency.

Pilot B recorded connected retrieval of 31 seconds for 12 documents (source-b.md:3). Its log states "A network connection was required." (source-b.md:4); this establishes a network requirement in the observed Pilot B run only, not an architecture-wide conclusion.

Both runs covered 12 documents (source-a.md:3, source-b.md:3) but differ in network mode (source-a.md:3, source-b.md:3). Pilot B's log itself warns that "The conditions differ from Pilot A; this is not a controlled benchmark and does not establish superiority." (source-b.md:4), so the 31- versus 42-second gap is not a valid performance comparison.

## Limitations

Each log records a single observed run; the inspected sources do not report repetitions, controls, or representativeness across operating modes. Source A's untested network requirement remains unknown, not proven absent. Source B's required connection is documented only for its observed run. The logs do not report retrieval method, hardware, dataset identity beyond count, or measurement protocol.

## Sources

- source-a.md — Pilot A field log, 2026-09-01, L1-4 (workspace file).
- source-b.md — Pilot B field log, 2026-09-02, L1-4 (workspace file).
