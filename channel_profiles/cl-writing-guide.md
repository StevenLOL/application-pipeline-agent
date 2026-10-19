# Cover-letter writing guide for Singapore

> Research-based guidance (5 sources, 2025–2026: TAFEP fair-hiring framework,
> recruiter blogs, CV guides) | 2026-10-19
> **The question this answers**: point-by-point response to the JD's concerns,
> or an achievement-led narrative? The Singapore consensus is a **hybrid** —
> targeted framing + achievement evidence — not a mechanical requirements
> checklist, and not a standalone achievements tour.

## The hybrid structure that works in Singapore

1. **Opening: name the role and why *this* employer.** One or two sentences
   quoting or paraphrasing the posting's own mission language shows you read
   it. (The golden corpus does this: quotes the office's "drives the adoption
   of data and AI" phrasing and builds the letter around it.)
2. **Body: map achievements to the JD's 2–4 core demands.** Not a checklist —
   organise paragraphs by the posting's *responsibility dimensions* (the way
   the JD itself splits the role), and argue each with a quantified
   achievement. Recruiters in Singapore read for "relevant to THIS role"
   within 15–30 seconds.
3. **Close: availability + location + a soft call to action.** For returning
   Singaporeans: state relocation timing and family grounding plainly — it
   pre-empts the interviewer's first question.

## What the sources agree on

- **One page maximum** for the CL (all five sources; government-linked
  entities follow the posting's own instructions).
- **Tailoring beats length**: relevance to the specific advert is the single
  strongest differentiator in Singapore's market ("relevance often matters
  more than experience length").
- **Quantified outcomes over responsibilities**: "Managed 15 key accounts and
  increased renewal rates by 22%" — numbers-oriented hiring culture.
- **Clarity over vocabulary**: simple, direct, professional English; no slang,
  no drama.
- **Cover letters are appreciated but not always required** — for MNCs and
  government-linked entities they differentiate; for SMEs a brief direct one
  signals professionalism.

## What is Singapore-specific

- **Work-pass status stated plainly** (Singapore Citizen / PR / EP holder):
  employers screen on eligibility first; ambiguity makes them assume the
  expensive case.
- **Salary expectations**: only if the posting asks — then a figure or narrow
  range in SGD, monthly convention.
- **Regional scope named explicitly** (APAC/SEA markets) carries more weight
  here than in most markets.
- **Fair-hiring framing (TAFEP)**: employers are steered to merit-based
  screening — so the CL should argue merit (outcomes, scope, controls), not
  personal attributes.

## How APA operationalises this

- The compliance engine checks the CL's structural quad (date line / Re:
  subject / salutation / sign-off), the one-page budget, and no-table rule
  (see `src/apa/compliance/rules/format.py`, rule family FMT).
- The extraction layer pulls the JD's deadline / requirements / assessments
  with verbatim provenance — the raw material for the body mapping above.
- The retrieval layer finds your most similar past packages, so each new CL
  starts from your best prior attempt rather than a blank page.
- **The human writes the final text.** The agent surfaces structure, evidence,
  and prior art; the voice is yours.

## Sources

- jot.com.sg — Singapore recruiter's desk (15–30 second scan, achievements
  over responsibilities, work-authourisation clarity)
- careerbldr.com — Resume Writing Guide for Singapore 2026 (CL one page max;
  cover letters appreciated but not always required)
- matchcv.co — Singapore resume format (TAFEP fair-hiring leave-off list;
  ATS single-column rules)
- thecvedge.com — Singapore CV Format 2026 (two-page norm for experienced;
  NRIC/FIN never on a distributed document; salary only if asked)
- coachchiao.com — overseas job-hunting checklist (photo convention
  contradiction noted; TAFEP takes precedence)
