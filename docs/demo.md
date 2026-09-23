# Demo Instructions

A scripted walkthrough for a live demonstration, using only real backend
execution — no step below is a UI animation or a canned response.

## Setup (once)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,ml]"
python -m scripts.seed --reset
python -m servicemesh.ml.train          # optional: enables the ML scenario
uvicorn servicemesh.api.main:app &
cd frontend && npm install && npm run dev &
```

Open `http://localhost:5173`.

## Act 1 — the happy path (2 minutes)

1. Sign in as **Aarti Deshpande** (`aarti@example.com` / `customer123`).
2. **New request** → describe: *"My laptop battery is swelling and it shuts
   down after twenty minutes. Order ORD-100001, serial SN-AX14-0001."*
3. Point out: the extraction suggested `BATTERY_SWELLING` with `URGENT`
   urgency from the text — but nothing downstream trusted that suggestion
   blindly.
4. Open the resulting transaction. Walk the **timeline**: five organizations,
   each with a green tick, each timestamped. Open the **Evidence** tab and
   show the actual JSON returned by the marketplace, the OEM, and the
   warranty provider — this is not summarised, it's their real response
   bodies.
5. Open the **Operations** tab, find `RESERVE_PART`, and show the
   `has_idempotency_protection: protected` badge with its key.

## Act 2 — a business rejection is not an outage (1 minute)

6. **New request**, structured mode, serial `SN-AX14-0002` (same customer,
   different laptop — its warranty expired 45 days ago in the seeded data).
7. Show the transaction lands in `REJECTED`, not `FAILED`, with the exact
   reason from the warranty provider's own contract record. Open
   **Operations** — `VERIFY_WARRANTY` shows `attempt_count: 1`. It was not
   retried, because retrying a permanent business answer wastes time and
   never helps the customer.

## Act 3 — a genuinely unauthorized provider gets rejected and replaced (2 minutes)

8. Sign out; sign in as **Rohit Kulkarni** (`rohit@example.com` /
   `customer123`, based in Nagpur).
9. New request with serial `SN-AX15-0001`.
10. Open **Decisions**. Show two `PROVIDER_SELECTION` rows: the first,
    `SVC-NAG-GREY`, marked `REJECTED` with reason *"partner not present in
    OEM authorization registry"* — genuinely the nearest, cheapest,
    best-SLA centre for this customer, ruled out because the OEM's own
    registry says no. The second row shows the actual authorized centre
    selected instead. The transaction still closes.

## Act 4 — inject a real failure and watch it recover (3 minutes)

11. Sign in as **admin** (`admin@servicemesh.io` / `admin123`) →
    **Failure simulation**.
12. Set: organization `parts_supplier`, mode `TEMPORARILY_UNAVAILABLE`,
    operation `inventory.check`, count `2`. Apply.
13. Sign back in as Aarti, create a normal request (`SN-AX14-0001` again).
14. Open the new transaction's **Failures & recovery** tab live. Show:
    - two `SERVICE_UNAVAILABLE` / `TRANSIENT` failure rows from the real
      supplier service,
    - two `RETRY_WITH_BACKOFF` recovery actions with their rationale text,
    - the transaction still reaches `CLOSED` on the third attempt.
15. Reset simulation.

## Act 5 — the hard case: an outcome nobody can be sure of (2 minutes)

16. Admin → Failure simulation → organization `parts_supplier`, mode
    `TIMEOUT_AFTER_COMMIT`, operation `reservations.create`, count `1`.
17. Create another request as Aarti.
18. Show the recovery action: strategy `RECONCILE_VIA_IDEMPOTENCY`, rationale
    explaining that the reservation may or may not have landed. Show that
    exactly one reservation exists (open **Evidence**/operations — the
    `reservation_ref` is populated once, not twice), because ServiceMesh
    asked the supplier directly using the idempotency key rather than
    guessing.

## Act 6 — persistent failure, escalation, and safe resume (2 minutes)

19. Admin → set `parts_supplier` to `TEMPORARILY_UNAVAILABLE` with **no
    count** (indefinite).
20. Create a request. Show it exhausts retries, falls back to a second
    supplier, exhausts again, and lands in `ESCALATED` — "needs review"
    badge visible in the customer's transaction list too.
21. Admin resets the simulation to normal.
22. Back on the transaction, click **Resume**. Show it reaches `CLOSED` —
    and, in **Operations**, that `VERIFY_PURCHASE`, `VERIFY_PRODUCT` and
    `VERIFY_WARRANTY` still show `attempt_count: 1` each. The resume did not
    repeat work that had already succeeded.

## Act 7 — provider portal (1 minute)

23. Sign in as the assigned service centre
    (`svc-pune-01@partners.servicemesh.io` / `provider123`).
24. Show **Assigned jobs** — only this organization's bookings, nothing from
    the supplier or any other centre.
25. Advance a job through `DIAGNOSING` → `REPAIRING` → `COMPLETED` and show
    the customer's transaction close in real time.

## Act 8 — the numbers are real (1 minute)

26. Admin → **Dashboard**. Point out every figure is computed live —
    `duplicate_operations_prevented` in particular is nonzero if the timeout
    scenarios above were run, and can be cross-checked against the
    transactions just created.

## Closing note for a research/academic audience

If presenting to a research audience, follow with:

```bash
python -m experiments.run_experiment --trials 10 --failure-rates 0.0 0.3 0.6
python -m experiments.plots
```

and open `experiments/results/comparison.png`, walking through the caveat in
`docs/research.md` about the flat baseline curve — presenting that caveat
unprompted is more credible than waiting to be asked about it.
