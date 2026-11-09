---
name: money-flow-integrity
description: Use when touching booking, purchase, order, payment, claim, credit, inventory-hold or fulfilment code — a booking API (quote / book / cancel / complete), a checkout flow, a marketplace listing, a registration that takes a fee — to make sure every operation happens exactly once, quantities and amounts never use floats, and a reconciliation query proves the books balance. Do NOT use for pricing strategy, tax or legal questions, or for wiring real payment-provider keys.
---

> Adapted from agency-agents `engineering-payments-billing-engineer` (https://github.com/msitarzewski/agency-agents, MIT, commit 4e3ac3c). Persona removed, method kept, examples made generic.

# Money-flow integrity

Any operation that moves value — money, credits, units of stock, certificates, seats — must happen
exactly once, reach a final state, and be provable afterwards. Retries, double clicks, replays and
out-of-order events are normal, not edge cases.

The method is the same whether or not real money moves yet: a system that only *holds* inventory
today (no payment provider) should already follow it, so nothing changes on the day payments start.
Advisory only: never configure live payment keys; payment-provider work uses test mode.

## When to use

- Changes to the modules that create, hold, cancel, fulfil or refund value — e.g.
  `<your-app>/src/modules/booking/`, `orders/`, `checkout/`, `inventory/`, `ledger/`, `payments/` —
  or to the database models behind them (orders, holds, allocations, ledger entries, payouts).
- Any registration, subscription or fee flow, as soon as it can take money.

## Inputs to gather

1. The operation, its trigger and every state it can end in (use `workflow-spec` if no spec exists).
2. The current schema for amounts and quantities (e.g. `Decimal(18,6)` for quantities,
   `Decimal(18,2)` or integer cents for currency). Check before changing anything.
3. Existing idempotency handling: which business key is unique (e.g.
   `UNIQUE (partner_id, booking_ref)`), and what an identical replay returns today.

## Checklist

1. **Idempotency key from the business operation**, not a random UUID per HTTP call
   (e.g. partner + booking reference; order id + attempt). Enforced by a database unique
   constraint, not only by an application check.
   - Same key + same payload → return the original result (no second hold, no second charge).
   - Same key + different payload → reject with a clear conflict error.
2. **Concurrency.** Two simultaneous requests with the same key, and two different keys competing
   for the same last units: exactly one wins; the loser gets a deterministic error. Use a
   transaction with row locks or conditional updates; prove it with a concurrency test (two
   parallel requests in an e2e or integration test).
3. **Quantities and money never as floats.** Use integer minor units (cents) with an ISO 4217
   currency, or a fixed-precision Decimal end to end. No `Number(...)` / `parseFloat` on amounts
   in JS/TS, no `float` in Python; keep the ORM's Decimal type and do arithmetic with it. State the
   rounding rule and where it is applied (once, at a named step). Non-currency quantities (units,
   weights, credits) follow the same rule.
4. **Every state modelled, especially the unhappy ones:** quote expired, partially held, pending
   inventory, cancelled after hold (units released), fulfilled/completed, cancel after fulfilment
   (refused or refunded), and for payments: requires action, processing, partial refund, dispute.
5. **External confirmations are the source of truth**, not redirects or client claims. For a
   payment provider: verify webhook signatures, store the verified event durably before
   acknowledging, dedupe by event id, mark complete only after side effects succeed, re-fetch
   current state before acting on an out-of-order event.
6. **Side effects safe to replay:** fulfilment records, receipts and outbound notifications keyed
   so a crash between "did it" and "marked done" does not duplicate.
7. **Reconciliation ships with the feature.** A query that must return zero rows, e.g.:
   ```sql
   -- held quantity on each order must equal the sum of its allocations
   SELECT o.id, o.held_qty, COALESCE(SUM(a.qty), 0) AS allocated
   FROM orders o
   LEFT JOIN order_allocations a ON a.order_id = o.id
   GROUP BY o.id, o.held_qty
   HAVING o.held_qty <> COALESCE(SUM(a.qty), 0);
   ```
   Add the equivalent for: inventory marked held vs active orders, fulfilled units vs fulfilment
   records, and (when money moves) provider payouts vs your ledger. Verify column names in the
   current schema before running. Any non-zero drift is an incident.
8. **Failure catalogue tested:** duplicate request, concurrent request, timeout mid-operation,
   expiry, cancel after partial hold, replay after crash. For a payment provider, its published
   test cases for declines and authentication.

## Output

```markdown
Money-flow integrity — <change / PR>
Operation: <name>   Key: <idempotency key + enforcing constraint>
| Check | Status (OK / GAP / N/A) | Evidence (file:line, test name) |
Reconciliation queries: <path or inline>, result on local data: <rows>
Gaps to fix before merge: <numbered, concrete>
```

Then summarise for the user in two lines of plain language (in the user's language): "can anything
be duplicated or lost?" and what is still pending.

## Stop / escalate

- Any request to use live payment-provider keys, move real funds or change payout settings → stop;
  the user does it themselves.
- A business rule is missing (who absorbs a shortfall, refund window) → ask the user, ideally as a
  short multiple-choice question (AskUserQuestion in Claude Code); do not invent.

## Out of scope

Pricing and margins, tax and invoicing law, payment-provider selection, PCI audits, executing payments.
