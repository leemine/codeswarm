# Research Report

## Scope

Compare source-a.md and source-b.md to establish what they say about offline retrieval and benchmark comparability. Only these two files were used.

## Findings

Pilot A recorded offline retrieval of 42 seconds for 12 documents (source-a.md:L3). All 12 documents were indexed locally (source-a.md:L4). Pilot A states no network requirement was tested (source-a.md:L4), leaving the network requirement unknown rather than confirmed absent.

Pilot B recorded connected retrieval of 31 seconds for 12 documents (source-b.md:L3). Pilot B states a network connection was required (source-b.md:L4). The runs occurred on different dates: Pilot A on 2026-09-01 (source-a.md:L2) and Pilot B on 2026-09-02 (source-b.md:L2).

Pilot B states the conditions differ from Pilot A, the comparison is not a controlled benchmark, and it does not establish superiority (source-b.md:L4). The 42-second (source-a.md:L3) and 31-second (source-b.md:L3) figures are therefore not directly comparable.

## Limitations

Pilot A's network requirement is untested, not proven absent (source-a.md:L4); the inspected sources do not report whether offline retrieval operates without a network. Each condition has one observed run (source-a.md:L3; source-b.md:L3), so neither establishes an architecture-wide conclusion. No controlled repetition is reported in either log.

## Sources

- source-a.md: Pilot A field log, 2026-09-01 (source-a.md:L2); offline retrieval 42 s/12 docs (source-a.md:L3); local index, no network tested (source-a.md:L4).
- source-b.md: Pilot B field log, 2026-09-02 (source-b.md:L2); connected retrieval 31 s/12 docs (source-b.md:L3); network required, not a controlled benchmark (source-b.md:L4).
