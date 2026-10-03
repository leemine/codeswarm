# Research Report

## Scope

Question: What do two recorded retrieval runs establish about offline retrieval and benchmark comparability? Audience: research reviewer. Time range: 2026-09-01 to 2026-09-02. Sources: source-a.md and source-b.md, two synthetic field logs. Deliverable: a cited comparison report plus evidence JSON.

## Findings

Pilot A recorded offline retrieval taking 42 seconds for 12 documents (source-a.md:3). All 12 documents were indexed locally in that run (source-a.md:4). Pilot B recorded connected retrieval taking 31 seconds for 12 documents (source-b.md:3). In Pilot B's observed run, a network connection was required (source-b.md:4).

Pilot A states that no network requirement was tested (source-a.md:4). This means the network dependency for Pilot A's mode is unknown, not that a network dependency is absent. Conversely, a network requirement observed in Pilot B's run (source-b.md:4) does not establish that the architecture always requires a network connection.

Pilot B explicitly states that its conditions differ from Pilot A, that this is not a controlled benchmark, and that it does not establish superiority (source-b.md:4). The two runs therefore cannot be compared as a controlled benchmark (source-b.md:4), despite both processing 12 documents (source-a.md:3, source-b.md:3).

## Limitations

Each source records a single observation; one recorded run does not establish that only one run occurred. The inspected sources do not report repetitions, controls, or architecture-wide network behavior. Pilot A's untested network requirement remains unknown, not proven absent.

## Sources

- source-a.md: "Pilot A field log," dated 2026-09-01 (source-a.md:2). L3: "offline retrieval took 42 seconds for 12 documents." L4: "All 12 documents were indexed locally. No network requirement was tested."
- source-b.md: "Pilot B field log," dated 2026-09-02 (source-b.md:2). L3: "connected retrieval took 31 seconds for 12 documents." L4: "A network connection was required. The conditions differ from Pilot A; this is not a controlled benchmark and does not establish superiority."
