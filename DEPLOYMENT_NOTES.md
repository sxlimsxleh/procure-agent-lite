# Deployment notes

What a real deployment at Brunner (or the next customer) has to find out, what this demo assumed
instead, and which parts should become product rather than custom work.

## 1. Discovery questions

### Process
- Who raises purchase requests, and in which system: SAP purchase requisitions, email, a form?
- How do quotes arrive: a shared mailbox, personal inboxes, a supplier portal? How many per week?
- What happens today when fewer than three quotes come back? Who may waive the rule?
- Which purchases are out of scope (framework contracts, catalogue items, capex, services)?
- How long does a request really take today, measured, not estimated? Where does the time go:
  reading quotes, comparing, typing into SAP, chasing approvals?

### Policy
- Approval limits per role and cost centre, not one threshold. Who is the second approver?
- How many days of earlier delivery is 100 EUR worth? (The demo weights price 0.60, delivery
  0.25, payment terms 0.15, which is a guess.)
- Is freight part of the price comparison? Are multi-item quotes split into one PO per item?
- Blocked or preferred suppliers: where is that list kept today, and who maintains it?
- What counts as "expired": the validity date, or the date of order placement in SAP?

### Systems
- SAP release (S/4HANA cloud or on-premise), the client for testing, and how APIs are exposed
  (API management, principal propagation, technical user).
- Mandatory PO fields beyond the demo: purchasing organisation and group, plant, tax code,
  account assignment, `NetPriceQuantity` for prices per N pieces.
- Is the supplier master clean enough to map names from quotes to supplier numbers?
- Identity provider for approvers (Entra ID, Okta, ...).

### Data and compliance
- What share of quotes are scans or faxes?
- Where may supplier documents be processed (data residency), and for how long kept?
- Which data protection review is needed (supplier contact data is personal data)?
- Does the works council need to agree, since approvals are logged per person?

## 2. Assumptions this demo makes

| Topic | Assumption | Why it matters |
|---|---|---|
| Time per request | 40 minutes, from the scenario | The time saving is unmeasured until a pilot |
| Multi-line quotes | `total` = line amount of the requested item; freight and other lines excluded | Changes which quote is cheapest |
| Scanned and faxed quotes | Values OCR cannot read are flagged for a human, never repaired | Safe, but more manual work if faxes are common |
| Scoring weights | Price 0.60, delivery 0.25, payment terms 0.15 | Müller wins partly on delivery; other weights pick other suppliers |
| Approvers | Typed name; second approver above €25k | Meaningless without SSO |
| FX rates | Fixed table in the config | Must be dated rates from a source |
| Supplier numbers | Small table in the config | Must come from the supplier master |
| Revisions | Same supplier + same base quote number, highest revision wins | Two different "Rev. 2" quotes are both kept (no newest one) |
| Demo date | Pinned to 2026-11-08 in Docker Compose | Quotes expire; the demo should not change |

## 3. What should become product, and what stays per customer

The test: would the next customer need the same code unchanged? Then it is product. Would they
need different values? Then it is configuration. Would they need different logic? Then it is a
per-customer procedure, and the second time it is needed, a candidate for product.

| Part | Product | Per customer |
|---|---|---|
| Document ingest (PDF, email, spreadsheet, text) and content-hash dedupe | ✓ | |
| Grounding check and number/date readings (DE, EN, CH) | ✓ | More locales as customers need them |
| Extraction schema and prompt | ✓ core fields | Extra fields per industry |
| Eval harness: labels, recordings, stale-prompt check, wrong-value gate | ✓ | The labelled sample itself |
| Idempotent ERP writer with retries | ✓ | |
| SAP field mapping | Connector | Mapping values per customer |
| Approval queue derived from an append-only audit log | ✓ | Roles and limits from config |
| Policy engine: exclusions, minimum quotes, scoring | ✓ engine | Thresholds, weights, blocked suppliers |
| Revision handling (superseded quotes) | ✓ | Supplier-specific quote number formats if needed |
| Customer config schema, validated at startup | ✓ | `config/<customer>.yaml` |

### Repeated work seen while building this that should become product
- **Every new document type needed a new label and a new eval run.** A labelling tool for buyers
  (confirm or correct each value with its source text highlighted) would build the customer's eval
  set as a side effect of normal work.
- **Every schema change made all recordings stale.** That is correct, but re-recording should be
  one click, with a diff of what changed per document.
- **API limits only surfaced against the real API** (16 nullable fields in a strict schema). The
  schema builder should check provider limits before anything is sent.
