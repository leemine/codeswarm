# Research Report

## Scope

Compares two recorded retrieval runs: Pilot A, dated 2026-09-01 (source-a.md:2), and Pilot B, dated 2026-09-02 (source-b.md:2). Establishes what they report about offline retrieval and benchmark comparability.

## Findings

Pilot A recorded offline retrieval taking 42 seconds for 12 documents (source-a.md:3); all 12 documents were indexed locally (source-a.md:4). Pilot B recorded connected retrieval taking 31 seconds for 12 documents (source-b.md:3); a network connection was required (source-b.md:4). Both runs covered 12 documents (source-a.md:3, source-b.md:3). Pilot B states the conditions differ from Pilot A, that it is not a controlled benchmark, and that it does not establish superiority (source-b.md:4). Pilot A states no network requirement was tested (source-a.md:4).

## Limitations

Pilot A's network requirement was untested (source-a.md:4); untested means unknown, not absence of a dependency. Pilot B's network requirement is an observation in one run, not an architecture-wide conclusion (source-b.md:4). Each log records a single run; one observation does not prove only one run occurred. The two runs used differing conditions, offline versus connected (source-a.md:3, source-b.md:3), so direct comparison of the 42-second and 31-second figures (source-a.md:3, source-b.md:3) is not valid; benchmark comparability is explicitly disclaimed (source-b.md:4).

## Sources

- source-a.md:1-4 -- Pilot A field log
- source-b.md:1-4 -- Pilot B field log
