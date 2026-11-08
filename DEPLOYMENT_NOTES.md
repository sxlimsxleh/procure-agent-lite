# Deployment notes

Assumptions made while building the demo that a real customer has to confirm. Expanded into
discovery questions, assumptions and product feedback in milestone 8.

## Discovery questions collected so far

| Topic | Assumption in the demo | Question for the customer |
|---|---|---|
| Multi-line quotes | `total` = line amount of the requested item; freight and other lines excluded | Is freight part of the price comparison? Do you split POs per item? |
| Scanned / faxed quotes | Values that OCR cannot read reliably are flagged for a human, never repaired | What share of quotes arrive as scans or faxes? Who checks flagged fields today? |
| Scoring weights | Price 0.60, delivery 0.25, payment terms 0.15 | How many days of earlier delivery is 100 EUR worth to you? |
| Approver identity | Approvers type their name | Which SSO / identity provider? Who may be the second approver above 25k? |
