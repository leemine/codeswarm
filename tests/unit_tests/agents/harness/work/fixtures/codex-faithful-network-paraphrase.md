# Research Report

## Scope

Comparison of two synthetic field logs (source-a.md, source-b.md) on offline retrieval behavior and benchmark comparability. Audience: parent research agent. Time range: 2026-09-01 to 2026-09-02.

## Findings

Pilot A recorded offline retrieval taking 42 seconds for 12 documents (source-a.md:3). All 12 documents were indexed locally (source-a.md:4). Pilot B recorded connected retrieval taking 31 seconds for 12 documents (source-b.md:3). A network connection was required for Pilot B (source-b.md:4).

Both runs processed the same document count (12) (source-a.md:3, source-b.md:3). Pilot B was faster, 31 s versus 42 s (source-a.md:3, source-b.md:3). However, the conditions differ between the two pilots (source-b.md:4). Source-b states this is not a controlled benchmark and does not establish superiority (source-b.md:4).

Pilot A did not test any network requirement (source-a.md:4). This untested requirement remains unknown, not an absent dependency; no network-free architecture follows from it. Pilot B required a network connection in the observed run (source-b.md:4), a condition of that run, not proof of an architecture-wide network dependency.

## Limitations

Sources are two synthetic four-line logs with no protocol detail, controls, or replication (source-a.md:1, source-b.md:1). One run per pilot. No test of network absence was performed in Pilot A (source-a.md:4). Comparability is explicitly disclaimed (source-b.md:4).

## Sources

- source-a.md — Pilot A field log, 2026-09-01, lines 1–4.
- source-b.md — Pilot B field log, 2026-09-02, lines 1–4.
