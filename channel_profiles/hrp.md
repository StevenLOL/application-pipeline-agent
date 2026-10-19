# Channel Profile: HRP (Careers@Gov / jobs.careers.gov.sg /jobs/hrp/)

> F11a (D11) filed | Evidence: D4/D5 probe runs, INC-2026-1016, run_003 screenshots,
> user submission practice (packages 01-25) | Updated: 2026-10-19
> **Limits vary per job**: the fields below are the *known set as of the update
> date*, not a specification — every posting may have different form fields,
> character limits and required items. Confirm per form before submitting, using
> the F11 per-form discovery flow (DOM maxlength / measured / unverified).

## Basics

| Item | Value |
|---|---|
| channel id | `hrp` (split by ATS backend path under jobs.careers.gov.sg: /jobs/hrp/) |
| Entry | `https://jobs.careers.gov.sg/jobs/hrp/<jobid>/<uuid>` |
| Auth | Careers@Gov account (Singpass-linkable); **login required before Apply** |
| Page architecture | Next.js App Router (RSC+CSR); **deadline/closed labels live in the rendered state** (detail-page SSR has no "Closing in N day(s)" — run_003 evidence) |
| Health probe | HEAD 200 is reliable (is the channel up); posting open/closed needs the rendered state (run_003 conclusion) |

## Form fields (known set — varies per job)

- Personal info: name (ALL-CAPS English convention) / contact email / phone (+65 format) / NRIC-FIN (often auto-filled once logged in)
- Education: entered item by item (institution / degree / major / years) — **resume upload is not a substitute**; manual entry required
- Work experience: entered segment by segment (organisation / title / start-end months / duties) — **per-segment character limits commonly <=256** (but **vary per job**, see INC-2026-1015: one form capped each experience segment at 256 chars plus a skills/competency table)
- Skills/competency table: some postings require item-by-item entry
- Expected salary: required by some postings

## Limit discovery (three states)

| State | Method |
|---|---|
| DOM discovery | `maxlength` attribute on form elements (grab after opening the form page) |
| Measured | Paste over-long text and watch truncation/errors (**final text must be re-checked before submitting** — F11 division-of-labour red line) |
| Unverified | No DOM hint and not measured — draft at conservative length (<=256) and mark "unverified" |

## Attachment slots

- Resume: **supported** (PDF preferred) — user practice: fill form fields + attach the original resume in parallel
- Cover letter: some postings support an attachment slot; otherwise paste into the body

## Known risks and evidence

1. **Maintenance window** (INC-2026-1016): HRP-wide maintenance 16 Oct 21:00 - 19 Oct 08:00; 16 postings could not be submitted; the notice lives only in the **manual announcement archive** (jobs/投递通道/) — the Statuspage JSON API returns S3 AccessDenied for non-browser clients (B7)
2. **Three sources, three deadline values risk** (Contoso case: archive 22 / ledger 24 / page 27) — the deadline follows the browser's current value, updated via `jd_registry_ops.py`
3. **Evergreen postings** (no published deadline): see the ledger's "页面未公布（WORKABLE 通道）" rows for the convention — HRP-backed postings can also be evergreen; confirm openness manually before applying
4. Government-posting discipline: deadline-buffer -48h (f10 rules) / no aggregator sites (channel-divergence)
