# Research Report

## Scope
Question: what do two recorded retrieval runs establish about offline retrieval and benchmark comparability? Audience: research reviewer. Sources: source-a.md and source-b.md (synthetic field logs dated 2026-09-01 and 2026-09-02). Deliverable: this report plus research-evidence.json.

## Findings
Pilot A recorded offline retrieval of 42 seconds for 12 documents (source-a.md:3), with all 12 documents indexed locally (source-a.md:4). Pilot B recorded connected retrieval of 31 seconds for 12 documents (source-b.md:3). In Pilot B a network connection was required (source-b.md:4). In Pilot A, no network requirement was tested (source-a.md:4); per the evidence policy, an untested requirement remains unknown, so Pilot A's offline run does not prove an absent network dependency — only that the requirement was not examined in that run.

The two runs are not comparable as a controlled benchmark. Source B states the conditions differ from Pilot A and that this is not a controlled benchmark and does not establish superiority (source-b.md:4). The 31-second versus 42-second difference therefore cannot be attributed to the offline/connected distinction.

## Limitations
Each log records a single observed run (source-a.md:3, source-b.md:3); a single observation does not establish that only one run occurred, nor does Pilot B's required connection generalize to every mode. The inspected sources do not report repetition, controls, or hardware/software parity. Pilot A's network status is unknown, not absent.

## Sources
- source-a.md, L1–L4: Pilot A field log, 2026-09-01; offline retrieval 42s/12 docs; "No network requirement was tested."
- source-b.md, L1–L4: Pilot B field log, 2026-09-02; connected retrieval 31s/12 docs; "A network connection was required."
