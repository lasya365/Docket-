Read RFC.md before doing anything. Section 0 is binding.
Build one milestone from section 16 at a time and run pytest before saying it is done.
decide/ is pure: no I/O, no clock, no LLM, imports only docket.models and stdlib.
Nothing is hard-coded that config.json can hold.
A missing section is present=false, never an exception.
Simulated data carries provenance="simulated".

---

## Section 0 of RFC.md, verbatim

1. **Build in milestone order** (section 16). Do not start a milestone until the previous one's "done when" checks pass. Run the tests after every change.
2. **This RFC is the contract.** If something here is ambiguous, pick the simplest reading, write the assumption as a comment at the top of the file you are editing, and continue. Do not redesign.
3. **Never put an LLM call, a network call, a file read, or a clock read inside `decide/`.** Those functions take a `ChangeRecord` and a config object and return values. Nothing else.
4. **Never hard-code a weight, threshold, sensitive path, sensitive tool, Freshservice field code or model name.** They live in `config.json`.
5. **Do not guess vendor field names.** Every external field name Docket reads is listed in one mapping table per adapter (sections 6 and 7). If real data disagrees with the table, change the table, not the callers. Log unknown events at DEBUG and keep going.
6. **Missing evidence is a value, not an exception.** A collector that finds nothing returns a fragment with `present = false`. Nothing downstream may crash on a missing section.
7. **Label simulated data.** Any value not read from a real system MUST carry `provenance = "simulated"` and the UI MUST show it.
8. **Tests run offline.** No test may touch the network. External clients are injected and faked.
9. **Dependencies are limited to section 4.** Ask before adding another.
10. **Escape everything you render.** Prompts, diffs, issue text and commit messages are untrusted input.
