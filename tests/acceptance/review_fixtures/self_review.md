# APA Self-Produced Review (Synthetic Fixture — Regression Target for the Reviewer's Self-Audit Discipline)

> This file is a **synthetic** document under tests/acceptance/review_fixtures/:
> it contains no real project data; it exists only so that the discipline
> "the reviewer must be able to audit its own output" can be replayed on any
> machine (docs/14 bidirectional output-and-review discipline).

Reviewer: APA self-audit
Review date: 2026-10-19
Review subject: F11 delivery-channel adaptation layer design (docs/07)
Source: D4-D5 probe evidence + ledger snapshot

## 1. Overall Assessment

- The channel-adaptation layer direction is correct; treating "per-form limits" as a per-form discovery is the most important correction in this case
- After comparing with competitors: there is no isomorphic competitor; the governance layer is the differentiator (basis: others/ three-repo code-level research)

## 2. Key Findings

- Character limits must be measured per-form (DOM maxlength / measured); when unknown, measure first, then generate
- Division of labor: the agent discovers the limit and drafts candidates, the user enters them by hand, and the original resume is still attached at submission
- Attachment slot (resume/CL attached or not) should enter the channel profile, distinguishing the summary layer from the full-information layer

## 3. Risks and Discipline

- Hard rule: never pass Chinese parameters through shell strings — always use subprocess argv list passing
- Never bypass HITL: the submit action is always performed by a human; the agent never clicks submit on the user's behalf
- Never delete the audit trail: all write-backs are logged through the single channel

## 4. TODO

- [ ] Complete the channel seven-category profile with Workday anti-scraping observations
- [ ] Make the three-state limit discovery (DOM / measured / unverified) visible in the UI
