# Public Release Sanitization Checklist

Maintained by scripts/sync_public.py and manual review. Items below MUST be
resolved before any public push. The allow-list exporter already excludes real
runs / golden sets / personal data by construction; this file records what was
scrubbed in-copy and what remains open.

## Structural exclusions (enforced by the exporter, not by hope)

- `reports/`, root `evals/` (real golden set), `data/`, `models/` — never copied.
- `DENY_FILES`: `scripts/01_build_golden_set.py`, `scripts/02_rule_false_positive_scan.py`
  — these are harnesses that rebuild golden sets from the operator's REAL private
  corpus (git-show of real letters, scans of real delivery packages). They carry no
  meaning in a clean public checkout and would fingerprint the private workspace,
  so they are excluded outright (source of truth stays private).
- A synthetic golden corpus ships instead (`evals/golden-synthetic/`).

## Operator initial-data purge (2026-10-20)

All real identifiers tied to the operator's actual applications were replaced with
synthetic tokens (233 replacements across 33 files), consistently in source, tests,
and fixtures. The real→synthetic mapping is deliberately **not** published (naming
the replaced identifiers here would itself be the leak); it is held in the private
workspace. Publicly, the guarantee is:

- Agencies / employers / programmes / banks → plausible fictional orgs
  (Fabrikam, Helios, Contoso, Acme, CivicTech, Northwind, Litware, Woodgrove,
  Initech, Proseware, AdventureWorks, Aurora, Borealis, Zephyr).
- Government job IDs → `GOV-SYNTH00001/2/3`.
- Personal education references → fictional institution (NBU).

Case variants were normalised case-preservingly (e.g. `test_bca_*` →
`test_contoso_*`). Residual-scan gate: zero real tokens must remain before push
(the gate also scans this file — hence this table names no real org).
The acceptance golden root was env-ified (`APA_GOLDEN_ROOT`); no absolute private
paths remain in exported tests.

## Done

- [x] AI-chatter scrubbed on export: 42 working-log fragments removed from 16 files
      (verbatim user quotes, AI self-narration/decisions, private paths, internal doc refs).
- [x] Operator initial data purged to synthetic tokens (see table above); residual scan clean.
- [x] Private golden-set harnesses structurally excluded (DENY_FILES).

## Open (tracked, intentionally deferred)

- [ ] **English-only (hard requirement)**: ~4.6k CJK characters remain across ~36 files
      (docstrings/comments/test prose). Translation is in progress and was rate-limit
      interrupted; unit-test assertions drift where source messages were translated but
      test expectations were not — expect a red test subset until the translation pass
      completes.
- [ ] `src/apa/config.py` / `cli.py` / `scripts/03` `/05`: remaining default-path env-ify.
- [ ] `tests/acceptance/*`: private-asset skipif paths — kept intentionally (they skip
      gracefully); documented here.
