**What this changes**


**New source checklist** (delete if not a source)

- [ ] `rh_preflight/sources/<name>.py` with `Client(Source)` and a keyless, free, read-only endpoint
- [ ] registered in `MODULES`, `ORDER`, `Network.enrich` and `known_sources`
- [ ] row in the README Sources table and in `docs/SOURCES.md`
- [ ] saved response in `tests/fixtures/` and a parser test
- [ ] `python -m pytest` passes offline
