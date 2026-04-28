# EXPLAINER.md — Playto Payout Engine

> This document explains the engineering decisions behind the payout engine — not just *what* was built, but *why* each decision was made, what breaks without it, and what tradeoffs were accepted. It is written for an engineer who will maintain or extend this system in production.

---

## 1. The Ledger

### What problem this solves

Every financial system needs to answer one question with complete certainty: **how much money does this merchant have?**

The naive answer is to store a `balance` field on the merchant row and update it whenever money moves. This is what most engineers build first. It fails in three distinct ways in production:

**Failure 1 — No audit trail.**
If the balance says ₹4,230 and a merchant disputes it, you have a number but no story. You cannot reconstruct which transactions produced that number. Every regulated financial system — and every system that will ever be audited — requires a complete, immutable record of every money movement.

**Failure 2 — Concurrent corruption.**
Two payouts processing simultaneously both read `balance = ₹10,000`, both subtract ₹6,000, both write ₹4,000. One subtraction is silently lost. This is a standard lost-update anomaly. It happens reliably under load.

**Failure 3 — Bugs are permanent.**
If any code path writes the wrong value to `balance`, it is gone. There is no baseline to compare against, no trail to replay, no way to determine whether the corruption happened one transaction ago or ten thousand.

### The approach: append-only double-entry ledger

```python
class LedgerEntry(models.Model):
    merchant      = models.ForeignKey(Merchant, on_delete=models.PROTECT)
    amount_paise  = models.BigIntegerField()   # always positive
    entry_type    = models.CharField(...)      # 'credit' or 'debit'
    description   = models.CharField(...)
    reference_id  = models.CharField(...)      # links to payout ID, payment ID
    created_at    = models.DateTimeField(auto_now_add=True)
    # No update. No delete. Ever.
```

Credits and debits are always positive integers. Direction is captured in `entry_type`, not sign. This is double-entry bookkeeping — the same model used by Stripe, Razorpay, and every bank that has existed since the 15th century. The reason it has survived that long is that it is correct.

**Balance is derived, never stored:**

```python
# payout_engine/models.py — Merchant.get_balance_summary()
result = self.ledger_entries.aggregate(
    total_credits=Sum('amount_paise', filter=Q(entry_type='credit')),
    total_debits=Sum('amount_paise',  filter=Q(entry_type='debit')),
)
available = (result['total_credits'] or 0) - (result['total_debits'] or 0)
```

Generated SQL:
```sql
SELECT
  SUM(amount_paise) FILTER (WHERE entry_type = 'credit') AS total_credits,
  SUM(amount_paise) FILTER (WHERE entry_type = 'debit')  AS total_debits
FROM payout_engine_ledgerentry
WHERE merchant_id = %s;
```

This is one round trip. The aggregation happens at the database level, not in Python.

### Why not store balance directly?

The alternative — `UPDATE merchant SET balance = balance - amount` — breaks the audit requirement immediately. Beyond that, it concentrates all write contention on a single row per merchant, making it a bottleneck under concurrent load. The ledger distributes writes across new rows and reads across aggregations that PostgreSQL handles efficiently with appropriate indexing.

### The invariant

```sql
-- This query must always equal what the dashboard displays.
-- If it ever does not, there is a bug. This is verifiable at any time.
SELECT
  SUM(amount_paise) FILTER (WHERE entry_type = 'credit') -
  SUM(amount_paise) FILTER (WHERE entry_type = 'debit')
FROM payout_engine_ledgerentry
WHERE merchant_id = X;
```

This invariant is checked in the test suite. If it ever drifts, the ledger has been corrupted — which means code somewhere is writing to it incorrectly, and we know about it before a merchant does.

### Why paise, not rupees

Floating-point arithmetic is non-associative. `0.1 + 0.2` in IEEE 754 is `0.30000000000000004`. Across millions of transactions, this drift becomes real money. It is also a compliance failure in audited systems. Storing amounts as integers (paise) eliminates the problem entirely. ₹99.99 is stored as 9999. The UI divides by 100 for display only. `BigIntegerField` maps to PostgreSQL's `BIGINT`, which can store values up to 9,223,372,036,854,775,807 — sufficient for any realistic payment amount.

---

## 2. The Lock

### What problem this solves

A merchant has ₹10,000. Two withdrawal requests for ₹6,000 each arrive simultaneously — from the same user clicking twice, from a retry, or from two browser tabs. Without coordination, both can succeed. ₹12,000 is paid out from a ₹10,000 balance. This is not a theoretical risk. It is the most common financial bug in systems that do not handle it explicitly.

The pattern is called **TOCTOU: Time-of-Check to Time-of-Use**. There is a window between reading the balance and creating the payout. Any concurrent request that reads the balance before the first request commits will see the same pre-deduction value and proceed incorrectly.

### Why Python-level locking does not work

A Django application in production runs as multiple worker processes — Gunicorn typically spawns 4 to 8. A `threading.Lock()` or any in-process synchronization mechanism only protects within a single process. It has no visibility into other processes on the same machine, and no visibility into other machines in a multi-server deployment.

```
Worker Process 1:                    Worker Process 2:
─────────────────                    ─────────────────
Read balance = ₹10,000               Read balance = ₹10,000  ← before P1 commits
Check: 10,000 ≥ 6,000  ✓             Check: 10,000 ≥ 6,000  ✓
Create ₹6,000 payout   ✓             Create ₹6,000 payout   ✓  ← overdraw
```

The only lock that works across all processes and all machines is one that lives in **PostgreSQL** — the single shared piece of state the entire system reads and writes. `SELECT FOR UPDATE` is that lock.

### The implementation

```python
# payout_engine/views.py — create_payout()

with transaction.atomic():
    # Acquire an exclusive row-level lock on all ledger entries
    # for this merchant. Any concurrent transaction attempting
    # the same will block here until this transaction commits or rolls back.
    locked_entries = LedgerEntry.objects.select_for_update().filter(merchant=merchant)

    agg = locked_entries.aggregate(
        total_credits=Sum('amount_paise', filter=Q(entry_type='credit')),
        total_debits=Sum('amount_paise',  filter=Q(entry_type='debit')),
    )
    total_credits = agg['total_credits'] or 0
    total_debits  = agg['total_debits']  or 0

    # Held funds: pending and processing payouts reduce available balance.
    # These rows are also locked to prevent a separate race on held amounts.
    held = Payout.objects.select_for_update().filter(
        merchant=merchant,
        status__in=['pending', 'processing']
    ).aggregate(held=Sum('amount_paise'))['held'] or 0

    available_balance = total_credits - total_debits - held

    if available_balance < amount_paise:
        return Response({'error': 'Insufficient balance'}, status=422)

    # Payout creation is inside the same atomic block.
    # The lock does not release until after this line commits.
    payout = Payout.objects.create(
        merchant=merchant,
        bank_account=bank_account,
        amount_paise=amount_paise,
        status='pending',
    )
# ← Transaction commits here. Lock releases here. Not before.
```

PostgreSQL translates `select_for_update()` to:

```sql
SELECT * FROM payout_engine_ledgerentry
WHERE merchant_id = %s
FOR UPDATE;
```

`FOR UPDATE` places an exclusive lock on every matched row. A second transaction issuing the same query is suspended at the database engine level until the first transaction commits.

### What actually happens under concurrent load — step by step

```
State: merchant balance = ₹10,000
Two requests arrive simultaneously: both request ₹6,000

Request A (Worker 1):                Request B (Worker 2):
─────────────────────                ─────────────────────
transaction.atomic() starts          transaction.atomic() starts
select_for_update() → LOCK GRANTED   select_for_update() → BLOCKED ⏸
                                     (waiting at database level)
aggregate: credits=10000, debits=0
held = 0
available = 10,000
10,000 ≥ 6,000  ✓
Payout.create(amount=6000)
transaction COMMITS ✓                UNBLOCKED ▶
lock releases                        aggregate: credits=10000, debits=0
                                     held = 6,000  ← A's pending payout
                                     available = 10,000 - 0 - 6,000 = 4,000
                                     4,000 < 6,000  ✗
                                     return 422 Insufficient balance ✓
```

One succeeds. One is rejected cleanly. Balance is never overdrawn. This behavior is verified by the concurrency test, which uses real OS threads against a real PostgreSQL instance.

### Why held funds are also locked

Locking only the ledger entries is not sufficient. Held funds — the sum of pending and processing payout amounts — must also be included in the available balance calculation. Without locking the payout rows as well, two concurrent requests could each see `held = 0`, both compute `available = full balance`, and both proceed. The `select_for_update()` on the Payout queryset closes this second race condition.

### Edge case: lock wait timeout

Under extreme concurrency, requests may queue behind the lock. PostgreSQL's default `lock_timeout` is unset (infinite wait). For production, set a timeout in `settings.py`:

```python
DATABASES = {
    'default': {
        ...
        'OPTIONS': {'options': '-c lock_timeout=5000'},  # 5 seconds
    }
}
```

A timed-out lock raises `django.db.utils.OperationalError`, which should be caught and returned as a 503 with a `Retry-After` header.

---

## 3. The Idempotency

### Why idempotency is not optional in payment systems

Networks fail. Clients time out. Load balancers retry. A merchant clicks "withdraw." The request reaches the server, the payout is created, but the TCP connection drops before the response is delivered. The merchant sees an error. They click again.

Without idempotency, this creates two payouts from one intended action. The merchant is paid twice. The merchant's available balance is debited twice. Depending on the merchant's balance and the processing timeline, this may or may not trigger an insufficient funds error on the second payout — making the outcome non-deterministic and difficult to debug.

Idempotency guarantees that **no matter how many times a request is retried, the outcome is identical to processing it exactly once**. This is not a convenience feature. It is a correctness requirement for any API that moves money.

### Implementation

Every payout request must include a client-generated UUID in the `Idempotency-Key` header. The server stores the key and the full response on first processing. On repeat calls, it replays the stored response without creating any new records.

```python
class IdempotencyKey(models.Model):
    key             = models.CharField(max_length=255)
    merchant        = models.ForeignKey(Merchant, ...)
    response_body   = models.JSONField()       # exact response, stored verbatim
    response_status = models.IntegerField()    # exact HTTP status code
    expires_at      = models.DateTimeField()   # 24 hours from creation

    class Meta:
        unique_together = [['key', 'merchant']]
        # PostgreSQL enforces this as a unique index.
        # Two rows with the same (key, merchant) cannot exist.
```

On each request, before any balance logic:

```python
try:
    existing = IdempotencyKey.objects.get(key=idempotency_key, merchant=merchant)
    if not existing.is_expired():
        # Replay the original response exactly.
        # No database writes. No payout creation. No side effects.
        return Response(existing.response_body, status=existing.response_status)
    else:
        existing.delete()   # Expired — treat as a fresh request
except IdempotencyKey.DoesNotExist:
    pass    # First occurrence — proceed normally
```

The key is saved **inside the same `transaction.atomic()` block** that creates the payout:

```python
with transaction.atomic():
    # ... SELECT FOR UPDATE, balance check ...
    payout = Payout.objects.create(...)
    IdempotencyKey.objects.create(
        key=idempotency_key,
        merchant=merchant,
        response_body=serialize(payout),
        response_status=201,
        expires_at=now() + timedelta(hours=24),
    )
# Both committed together, or neither committed.
```

This atomicity ensures there is no state where a payout exists but its idempotency key does not, or vice versa. If the worker crashes after the payout is created but before the key is saved, the transaction rolls back and the entire operation is as if it never happened.

### The simultaneous duplicate request case

The standard idempotency check handles sequential retries. The harder case is two requests with the same key arriving **before either has committed**:

```
Request A: key "abc-123" → not found in DB → enters transaction.atomic()
Request B: key "abc-123" → not found in DB → enters transaction.atomic()
                           (A hasn't committed yet, so B sees nothing)

Request A: creates payout → inserts IdempotencyKey "abc-123" → commits ✓
Request B: tries to insert IdempotencyKey "abc-123" → IntegrityError ✗
           unique_together constraint fires at the database level
           Django rolls back B's entire transaction
```

This is caught explicitly:

```python
except IntegrityError:
    # The unique constraint fired. Request A won the race.
    # Fetch A's committed response and return it to B's caller.
    try:
        existing = IdempotencyKey.objects.get(key=idempotency_key, merchant=merchant)
        return Response(existing.response_body, status=existing.response_status)
    except IdempotencyKey.DoesNotExist:
        # Extremely rare: A committed and the key was immediately expired/deleted.
        return Response({'error': 'Concurrent conflict. Please retry.'}, status=409)
```

No duplicate payout is created. Both callers receive a consistent response. The PostgreSQL constraint is the enforcement mechanism — not application logic.

### Keys are scoped per merchant

`unique_together = [['key', 'merchant']]` means Merchant A using key `abc-123` and Merchant B using key `abc-123` are independent. This is intentional. Merchants generate their own keys (typically `crypto.randomUUID()` in the client). Global uniqueness would make key collisions a failure mode. Per-merchant scope eliminates it.

### Keys expire after 24 hours

After 24 hours, the same UUID may be reused. Expired keys are deleted lazily on the next request with that key — no scheduled cleanup job is required. This is a deliberate tradeoff: the system pays a small cost on the first request after expiry (one extra DELETE) rather than running a periodic cleanup task.

### Database-based vs Redis-based idempotency: a comparison

An alternative approach uses Redis for idempotency, often implemented via a Lua script that atomically checks and sets a key:

```lua
-- Redis Lua: atomic check-and-set
local existing = redis.call('GET', KEYS[1])
if existing then return existing end
redis.call('SET', KEYS[1], ARGV[1], 'EX', 86400)
return nil
```

**Redis approach advantages:**
- Lower latency on cache hits (in-memory vs disk)
- No additional PostgreSQL write per request
- Naturally distributed across a Redis cluster

**Redis approach disadvantages:**
- Redis is not durable by default. A Redis restart before `fsync` loses all pending keys. A payout in-flight loses its idempotency record. The duplicate can now succeed.
- Redis and PostgreSQL can diverge. If the payout commits but the Redis write fails, the key is missing. The next retry creates a duplicate.
- Operational complexity: Redis requires its own backup, monitoring, and failover strategy.

**This implementation uses PostgreSQL** because durability is not negotiable in a payment system. The payout record and its idempotency key commit in the same transaction. They either both exist or neither exists. There is no divergence possible. The latency cost — one additional indexed write per new request — is acceptable and measurable.

### Scaling consideration

Under high traffic, the `unique_together` index on `(key, merchant)` becomes the hot path. PostgreSQL B-tree indexes handle millions of point lookups efficiently. The practical limit before requiring partitioning is in the hundreds of millions of rows — far beyond what this system will reach in the near term. If the idempotency table does become a bottleneck, the appropriate response is time-based partitioning (partition by `expires_at` week), which allows old partitions to be dropped as a bulk operation rather than row-by-row deletion.

---

## 4. The State Machine

### What problem this solves

A payout progresses through stages. Without a formal state machine, any code can write any status to any payout at any time. This leads to:

- A failed payout being reprocessed and paid out a second time
- A completed payout being marked failed, triggering a refund of money that was already sent
- A payout stuck in processing forever because the worker crashed and nothing transitions it out

A state machine eliminates these outcomes by making illegal transitions structurally impossible — not "we check for this" but "the code path does not exist."

### Implementation

```python
# payout_engine/models.py — Payout model

LEGAL_TRANSITIONS = {
    'pending':    ['processing', 'cancelled'],
    'processing': ['completed', 'failed'],
    'completed':  [],   # terminal — no transitions permitted
    'failed':     [],   # terminal — no transitions permitted
    'cancelled':  [],   # terminal — no transitions permitted
}

def transition_to(self, new_status, failure_reason=''):
    if new_status not in self.LEGAL_TRANSITIONS.get(self.status, []):
        raise ValueError(
            f"Illegal state transition: {self.status} → {new_status}. "
            f"Legal from '{self.status}': {self.LEGAL_TRANSITIONS.get(self.status, [])}"
        )
    self.status = new_status
    if failure_reason:
        self.failure_reason = failure_reason
    if new_status == 'processing':
        self.processing_started_at = timezone.now()
    # Does NOT call save(). Caller is responsible for saving inside their transaction.
```

`LEGAL_TRANSITIONS['failed']` is an empty list. `'completed' in []` is `False`. `transition_to('completed')` on a failed payout raises `ValueError` before any database operation. The calling `transaction.atomic()` catches the exception and rolls back.

Every status change in the codebase goes through `transition_to()`. There is no `payout.status = 'completed'` anywhere. If a future developer writes one, it bypasses the machine — which is why code review and the test suite exist.

### The cancellation window

`'cancelled'` is legal only from `'pending'`. A payout that has entered `'processing'` cannot be cancelled. This reflects the physical reality: once the bank API call has been initiated, there is no reliable way to recall it. The cancellation window is the period between payout creation and worker pickup — typically under 30 seconds in normal operation.

### Partial failure: worker crash mid-processing

Consider: a Celery worker picks up a payout, transitions it to `'processing'`, and then crashes before completing the bank call. The payout is now stuck in `'processing'` indefinitely.

This is handled by `retry_stuck_payouts`, a Celery Beat task that runs every 30 seconds:

```python
@shared_task
def retry_stuck_payouts():
    stuck_cutoff = now() - timedelta(seconds=30)
    stuck = Payout.objects.filter(
        status='processing',
        processing_started_at__lt=stuck_cutoff,
    ).select_for_update(skip_locked=True)

    for payout in stuck:
        if payout.attempt_count >= MAX_ATTEMPTS:   # 3
            _fail_payout_and_release_funds(payout, 'Timed out after 3 attempts')
        else:
            payout.status = 'pending'
            payout.processing_started_at = None
            payout.save()
            process_payout.delay(str(payout.id))
```

`skip_locked=True` is important: if multiple beat workers run concurrently (which should not happen in normal operation but can during deploys or restarts), each worker skips rows locked by another. No payout is processed twice by the retry task.

### The atomic fail-and-refund

```python
def _fail_payout_and_release_funds(payout, reason):
    with transaction.atomic():
        payout.transition_to('failed', failure_reason=reason)
        payout.save()
        LedgerEntry.objects.create(
            merchant=payout.merchant,
            amount_paise=payout.amount_paise,
            entry_type='credit',                     # return funds to merchant
            description=f'Payout refund: {reason}',
            reference_id=str(payout.id),
        )
```

The status change and the refund credit are a single transaction. If the `LedgerEntry` insert fails for any reason — disk full, connection dropped, constraint violation — the `payout.save()` rolls back as well. The payout remains in its previous state. The retry task will pick it up again.

There is no state in which:
- A payout is marked `failed` but its funds have not been returned ✗
- The funds are returned but the payout is not marked `failed` ✗

Both happen together or neither happens. This is the atomic guarantee.

---

## 5. The AI Audit

The first draft of the payout creation logic that an AI assistant produced:

```python
# ❌ WRONG — the AI's initial output
@api_view(['POST'])
def create_payout(request):
    merchant = Merchant.objects.get(id=request.data['merchant_id'])

    balance = merchant.get_balance_summary()

    if balance['available_paise'] >= request.data['amount_paise']:
        payout = Payout.objects.create(
            merchant=merchant,
            amount_paise=request.data['amount_paise'],
            status='pending',
        )
        return Response(serialize(payout), status=201)
    else:
        return Response({'error': 'Insufficient balance'}, status=422)
```

This code passes all unit tests. It fails in production.

**Bug 1: No transaction boundary.**
The balance check and the payout creation are two separate database operations. Between them, another concurrent request can slip through the gap, read the same pre-deduction balance, and create a second payout. This is the TOCTOU race described in Section 2.

**Bug 2: No row locking.**
Even wrapping this in `transaction.atomic()` does not fix it. PostgreSQL's default isolation level is `READ COMMITTED`. Under this level, two concurrent transactions can both read the same committed data simultaneously. `transaction.atomic()` alone does not serialize access — it only guarantees atomicity of the operations *within* it. Serialization requires `SELECT FOR UPDATE`.

**Bug 3: Aggregation in Python on stale data.**
`get_balance_summary()` fetches rows from PostgreSQL and computes the sum in Python. For display, this is acceptable. For a safety check that determines whether money moves, it is not — because the row data can change between the moment it was fetched and the moment the payout is created. The aggregation must happen at the database level, inside the lock, so the numbers cannot change between the read and the write.

**The replacement:**

```python
# ✅ CORRECT — what this codebase implements
with transaction.atomic():
    locked_entries = LedgerEntry.objects.select_for_update().filter(merchant=merchant)
    agg = locked_entries.aggregate(
        total_credits=Sum('amount_paise', filter=Q(entry_type='credit')),
        total_debits=Sum('amount_paise',  filter=Q(entry_type='debit')),
    )
    held = Payout.objects.select_for_update().filter(
        merchant=merchant, status__in=['pending', 'processing']
    ).aggregate(held=Sum('amount_paise'))['held'] or 0

    available = (agg['total_credits'] or 0) - (agg['total_debits'] or 0) - held

    if available < amount_paise:
        return Response({'error': 'Insufficient balance'}, status=422)

    payout = Payout.objects.create(
        merchant=merchant,
        bank_account=bank_account,
        amount_paise=amount_paise,
        status='pending',
    )
```

The lock, the aggregation, and the write are a single atomic operation. There is no window. Concurrent requests queue at the database and execute serially. This is not a style preference — it is the difference between a system that loses money and one that does not.

---

## 6. Engineering Thinking

### Why correctness matters more than features in money systems

Features are additive. A missing feature means something does not exist yet. A correctness bug in a money system means money has moved incorrectly — and recovering from that is orders of magnitude harder than building the feature correctly the first time.

A ledger entry, once created, represents something that happened in the real world. If a debit is created for a payout that then fails to refund, a merchant has less money than they should. If a balance is displayed incorrectly and a merchant makes decisions based on it, those decisions cannot be undone. The asymmetry between "feature not built yet" and "money lost or corrupted" drives every design decision in this system toward correctness over completeness.

### Design tradeoffs

**Tradeoff 1: Ledger aggregation vs stored balance**

Aggregating balance from ledger entries on every request is more expensive than reading a single `balance` column. For a merchant with 10,000 ledger entries, the `SUM()` query is slower than a point read. This cost was accepted because the ledger provides auditability, immutability, and the ability to reconstruct state at any point in time — properties that a stored balance cannot provide. If query performance becomes a bottleneck, a materialized view or a read-through cache over the ledger is the appropriate optimization — not abandoning the ledger model.

**Tradeoff 2: PostgreSQL-based idempotency vs Redis-based**

Redis-based idempotency has lower latency on cache hits. PostgreSQL-based idempotency has stronger durability guarantees. In a payment system, losing an idempotency key means potentially allowing a duplicate payout. The latency cost of a PostgreSQL write is measured in single-digit milliseconds. The cost of a duplicate payout is measured in support tickets, reconciliation work, and merchant trust. The tradeoff is not close.

**Tradeoff 3: Pessimistic locking vs optimistic locking**

Optimistic locking (read a version number, write only if version has not changed, retry on conflict) has lower contention under low concurrency. Under high concurrency — which is precisely when correctness matters most — it generates many retries and can starve lower-priority requests. Pessimistic locking (`SELECT FOR UPDATE`) serializes access deterministically. One request waits; the other completes. No retries, no starvation, predictable behavior under load.

**Tradeoff 4: Synchronous API, asynchronous processing**

The API returns 201 immediately after creating the payout record. Actual bank processing happens in the background. This means the merchant does not wait for the bank response (which can take seconds to minutes in real systems), but it also means the payout status shown immediately after creation is `pending`, not a final outcome. The dashboard polls every 5 seconds to reflect updates. This is a standard pattern for payment systems and is explicitly documented in the API contract.

### How this system ensures data integrity under real-world conditions

**Condition: Two simultaneous requests for the same merchant**
→ `SELECT FOR UPDATE` serializes them. One succeeds, one is rejected cleanly.

**Condition: Network timeout causes client to retry**
→ Idempotency key lookup returns the original response. No duplicate created.

**Condition: Two retries arrive before the first commits**
→ `IntegrityError` on the `unique_together` constraint. The loser fetches and returns the winner's committed response.

**Condition: Worker crashes after transitioning to 'processing'**
→ `retry_stuck_payouts` detects the payout after 30 seconds. Retries up to 3 times. Fails and refunds atomically after max attempts.

**Condition: Database crash during fail-and-refund**
→ `transaction.atomic()` rolls back. Payout remains in previous state. Retry task picks it up on next cycle.

**Condition: Ledger entry inserted with wrong amount**
→ The invariant `SUM(credits) - SUM(debits) = displayed balance` fails in the test suite. The specific entry is traceable via `reference_id` to the exact operation that created it.

**Condition: Code attempts an illegal state transition**
→ `transition_to()` raises `ValueError` before any database write. The calling transaction rolls back. The payout remains in its previous valid state.

The system is not correct because it tries hard to be. It is correct because the design makes incorrect states structurally unreachable.