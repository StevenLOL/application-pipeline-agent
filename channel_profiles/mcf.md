# Channel Profile: MyCareersFuture (MCF)

> F11a (D11) filed | Evidence: D4 probes, user submission practice, ledger (49 rows sourced=MCF)
> **Limits vary per job**: same as HRP — confirm fields/limits per posting; this profile is not a specification.

## Basics

| Item | Value |
|---|---|
| channel id | `mcf` |
| Entry | `https://www.mycareersfuture.gov.sg/job/<...>` |
| Auth | MyCareersFuture account (Singpass login) |
| Health probe | HEAD 200 (D4: 8 of 70 targets are mcf, all ok) |

## Form fields (known set)

- Applications go through the in-site "Apply" flow: personal info (carried over by Singpass) + **skills checklist** (ticked from the profile's skills, matched item-by-item against the employer's posted skills) + expected salary
- **Resume upload**: supported (PDF); mandatory for some postings
- High-frequency constraint: ticked skills must match the JD's posted skills — **under-ticking gets you screened out employer-side** (user field experience)

## Limit discovery (three states)

- DOM discovery: form maxlength
- Measured: paste validation + final-text re-check before submitting
- Unverified: conservative drafting + annotation

## Attachment slots

- Resume: supported (mandatory-leaning)
- CL: no standalone CL slot in-site — CL content merges into the resume or a message box (varies per posting)

## Known risks

1. **Anti-bot**: MCF pages restrict non-browser clients (D4 never triggered 403, but keep UA discipline)
2. **Fair-consideration framework**: MCF postings publish salary ranges — negotiation room is limited (an input to application decisions)
3. Ledger deadline column: MCF postings are often closed by the employer (no fixed date) — evergreen semantics
