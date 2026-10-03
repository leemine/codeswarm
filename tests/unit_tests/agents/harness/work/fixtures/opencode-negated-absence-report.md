# Research Report

## Scope

This report compares two synthetic four-line field logs — source-a.md (Pilot A field log, source-a.md:1, dated 2026-09-01, source-a.md:2) and source-b.md (Pilot B field log, source-b.md:1, dated 2026-09-02, source-b.md:2) — to determine what they establish about offline retrieval and benchmark comparability. Only these two sources were inspected; no web searches were used.

## Findings

Pilot A recorded offline retrieval taking 42 seconds for 12 documents (source-a.md:3), with all 12 documents indexed locally (source-a.md:4). Pilot A explicitly states that no network requirement was tested (source-a.md:4); this is an untested requirement (unknown), not evidence that a network dependency is absent.

Pilot B recorded connected retrieval taking 31 seconds for 12 documents (source-b.md:3). Pilot B states a network connection was required (source-b.md:4), establishing a requirement in that observed run only, not an architecture-wide conclusion that the system always requires a network.

Pilot B further states the conditions differ from Pilot A, that this is not a controlled benchmark, and that it does not establish superiority (source-b.md:4). The two numeric observations (42 s vs 31 s) are therefore not comparable as a controlled benchmark.

## Limitations

Each source is a single four-line synthetic log representing one recorded run; a single observation does not establish that only one run occurred, nor architecture-wide behavior. Pilot A's network requirement is untested (unknown), not confirmed absent. No controls, repetitions, or system-wide tests are reported in either inspected source (source-a.md:1-4, source-b.md:1-4).

## Sources

- source-a.md: "Pilot A field log" (source-a.md:1), dated 2026-09-01 (source-a.md:2); offline retrieval, 42 s for 12 documents (source-a.md:3); all 12 indexed locally, no network requirement tested (source-a.md:4).
- source-b.md: "Pilot B field log" (source-b.md:1), dated 2026-09-02 (source-b.md:2); connected retrieval, 31 s for 12 documents (source-b.md:3); network required, not a controlled benchmark, does not establish superiority (source-b.md:4).
