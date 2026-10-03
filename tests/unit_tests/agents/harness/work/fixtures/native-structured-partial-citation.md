# Research Report

## Scope

Question: Compare the two recorded retrieval runs: what do they establish about offline retrieval and benchmark comparability?

Pilot A was logged on 2026-09-01 (source-a.md:2).

Pilot B was logged on 2026-09-02 (source-b.md:2).

## Findings

Pilot A recorded offline retrieval taking 42 seconds for 12 documents (source-a.md:3).

All 12 documents in Pilot A were indexed locally (source-a.md:4).

Pilot A states that no network requirement was tested (source-a.md:4).

Inference: The network requirement for Pilot A is therefore unknown, not absent (source-a.md:4).

Pilot B recorded connected retrieval taking 31 seconds for 12 documents (source-b.md:3).

Pilot B states that a network connection was required (source-b.md:4).

Inference: The network requirement for Pilot B applies to the observed run, not an architecture-wide conclusion (source-b.md:4).

Pilot B states the conditions differ from Pilot A and the comparison is not a controlled benchmark (source-b.md:4).

Pilot B states the comparison does not establish superiority (source-b.md:4).

## Limitations

The two runs used different conditions (offline vs connected), so they are not a controlled benchmark (source-b.md:4).

Inference: The network requirement for Pilot A remains unknown because it was not tested; this does not establish that the network is absent (source-a.md:4).

Inference: The network requirement for Pilot B was observed in one run and does not prove the architecture always requires a network (source-b.md:4).

Only two single-run logs were inspected; the sources do not report additional runs, controls, or repetitions (source-a.md:1-4; source-b.md:1-4).

## Sources

- source-a.md:1-4 — Pilot A field log — source-a.md
- source-b.md:1-4 — Pilot B field log — source-b.md
