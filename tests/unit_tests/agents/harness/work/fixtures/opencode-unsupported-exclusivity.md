# Research Report

## Scope
Compare two synthetic four-line field logs (source-a.md, source-b.md) on offline retrieval and benchmark comparability. Only these two files were inspected; no web sources were used.

## Findings
source-a.md records Pilot A dated 2026-09-01 (source-a.md:1, source-a.md:2). Offline retrieval took 42 seconds for 12 documents (source-a.md:3). All 12 documents were indexed locally; no network requirement was tested (source-a.md:4).

source-b.md records Pilot B dated 2026-09-02 (source-b.md:1, source-b.md:2). Connected retrieval took 31 seconds for 12 documents (source-b.md:3). A network connection was required in that run (source-b.md:4). The conditions differ from Pilot A; Pilot B is not a controlled benchmark and does not establish superiority (source-b.md:4).

The 42s versus 31s figures each cover 12 documents but are not comparable: the runs used different retrieval modes (offline vs connected) (source-a.md:3, source-b.md:3), and only Pilot B required a network connection (source-a.md:4, source-b.md:4). source-a's statement that no network requirement was tested marks an untested requirement, not proof of an absent network dependency (source-a.md:4).

## Limitations
Two synthetic four-line logs do not support architecture-wide conclusions or prove only one run occurred per pilot. source-a's untested network requirement means the dependency is unknown, not absent (source-a.md:4). source-b's required network applies to its observed run only, not every mode (source-b.md:4).

## Sources
- source-a.md:1 "# Pilot A field log"; source-a.md:2 "Date: 2026-09-01"; source-a.md:3 "Primary observation: offline retrieval took 42 seconds for 12 documents."; source-a.md:4 "All 12 documents were indexed locally. No network requirement was tested."
- source-b.md:1 "# Pilot B field log"; source-b.md:2 "Date: 2026-09-02"; source-b.md:3 "Primary observation: connected retrieval took 31 seconds for 12 documents."; source-b.md:4 "A network connection was required. The conditions differ from Pilot A; this is not a controlled benchmark and does not establish superiority."
