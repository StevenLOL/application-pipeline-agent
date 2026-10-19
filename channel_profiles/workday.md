# Channel Profile: Workday (wd102 / wd3 external redirect)

> F11a (D11) filed | Evidence: user F11 field evidence (2026-10-17: Workday redirect,
> external form manual re-entry), run_003 (jobs.careers.gov.sg split 3 workable backends)
> **Limits vary per job**: Workday is the most tenant-heavy ATS — **every tenant /
> every posting can be a different form configuration**; per-form discovery is the
> only reliable method.

## Basics

| Item | Value |
|---|---|
| channel id | `wd102` / `wd3` (Careers@Gov's workable backend is listed separately as `workable` — Helios=GOV-SYNTH00001 evidence) |
| Entry | Careers@Gov posting page -> "Apply" redirect to the employer's Workday tenant (`<tenant>.wd102.myworkdayjobs.com` etc.) |
| Auth | **Per-tenant accounts** (each employer's Workday needs its own registration — not SSO) |
| Health probe | Redirect target sits on the employer domain — probes must follow redirects (current HEAD only probes the Careers@Gov page) |

## Form fields (known set — large tenant variance)

- Standard Workday flow: personal info -> work experience (**segment-by-segment textareas, often with no maxlength attribute** — limits surface via submit errors = "measured" state) -> education -> questionnaire (screening questions, **enum/free-text mixed**) -> self-identification (demographics, skippable) -> resume upload
- **The questionnaire is the biggest variable**: MCF screening-question conventions (INC-SYNTH-1015 evidence: one form had a skills/competency table + 256-char cap per experience segment)
- **External form manual re-entry**: the agent fills no fields (F11 division-of-labour red line) — draft candidates + user manual entry + final-text re-check

## Limit discovery (three states)

| State | Notes |
|---|---|
| DOM discovery | Workday React forms often have **no maxlength** — the DOM state is frequently unavailable |
| Measured | Submit-error echo (expensive: a full submission) -> draft at conservative length |
| Unverified | Default state |

## Attachment slots

- Resume: mandatory (PDF/DOCX); after upload Workday **auto-parses and pre-fills** the form — parse results are often wrong, so **field-by-field verification is mandatory**
- CL: some tenants support a "Cover Letter" upload slot

## Known risks

1. **Per-tenant accounts** -> large credential-management surface (recommend tracking tenant account status in the ledger)
2. Questionnaire conventions vary per job -> the extraction layer's (F3) assessment defence matters most on this channel
3. Workday sessions time out quickly (~30 min) — long forms can expire mid-entry and require re-login
