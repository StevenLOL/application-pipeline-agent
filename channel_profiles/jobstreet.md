# Channel Profile: JobStreet (aggregator)

> F11a (D11) filed | Evidence: D4 probes (JobStreet 403 among the 70 targets),
> ledger (AGG-Aurora/AGG-Borealis aggregator placeholder rows), user practice
> **Limits vary per job**: aggregator posting links shift with employer campaigns; this profile records channel-level behaviour only.

## Basics

| Item | Value |
|---|---|
| channel id | `jobstreet` (includes AGG-* aggregator placeholder rows) |
| Entry | `https://myjobstreet.jobstreet.com.sg/...` or the employer's direct-apply link |
| Auth | JobStreet account (regional job-seeker profile) |
| Health probe | **D4 evidence: HEAD 403 anti-bot** -> probe records `blocked` (the channel may be serving; the probe was refused — not an outage) |

## Form fields (known set)

- JobStreet profile (online resume) + per-posting application questionnaire
- **Employers often redirect from JobStreet to their own ATS** (a two-hop re-application) — the ledger's "real link" column must distinguish the JobStreet page from the final ATS

## Limit discovery (three states)

- DOM/measured as with the previous two channels; **under anti-bot conditions DOM probing is often refused** -> rely on the user's manual entry
- Aggregator placeholder rows (AGG-Aurora/Borealis) have no direct link -> ledger to be completed (docs/13 🟡)

## Attachment slots

- Resume: profile-bound + per-posting attachment
- CL: partially supported

## Known risks

1. **403 anti-bot**: automated probing is inherently limited on this channel — for jobstreet-class targets, an f10 `blocked` state is expected behaviour; do not retry repeatedly (IP-ban risk)
2. Aggregator placeholder-row hygiene (AGG-*) -> handed off in docs/13
3. channel-divergence rule: government postings must not go through aggregators (f10 rule set) — this channel is for private-sector postings only
