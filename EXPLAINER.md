## 1. The Ledger

### The balance calculation query

```python
# payout_engine/models.py — Merchant.get_balance_summary()

result = self.ledger_entries.aggregate(
    total_credits=Sum('amount_paise', filter=Q(entry_type='credit')),
    total_debits=Sum('amount_paise', filter=Q(entry_type='debit')),
)
available = (result['total_credits'] or 0) - (result['total_debits'] or 0)k
```

This is one database round trip. Django turns it into this SQL:

```sql
SELECT
  SUM(amount_paise) FILTER (WHERE entry_type = 'credit') AS total_credits,
  SUM(amount_paise) FILTER (WHERE entry_type = 'debit')  AS total_debits
FROM payout_engine_ledgerentry
WHERE merchant_id = 'some-uuid-here';
```

### Why I modelled it this way

There are two ways to track a merchant's money. Here's the comparison:

---

**Option A — Store balance directly on the merchant row (what most people build first):**

```python
# Sounds simple. Isn't.
class Merchant(models.Model):
    balance_paise = models.BigIntegerField(default=0)

# When a payout happens:
merchant.balance_paise -= payout_amount
merchant.save()
```

This feels clean. It breaks in three ways in production:

**Break #1 — You can never reconstruct what happened.**
If the balance says ₹4,230 and a merchant says "I should have ₹8,500," you have no way to audit the history. You have a number with no story behind it. Every bank, every payment company, every audited financial system keeps a full record of every movement. That record IS the truth. The balance is just a summary.

**Break #2 — Concurrent updates corrupt the number.**
Two payouts processing at the same time both read balance = ₹10,000, both subtract ₹6,000, both write ₹4,000. You just lost ₹6,000. This is a real bug that has happened at real companies.

**Break #3 — A bug is permanent.**
If any code anywhere writes the wrong number to `balance_paise`, it's gone. You can't fix it. There's no trail to replay.

---

**Option B — What this codebase does (the ledger model):**

```python
class LedgerEntry(models.Model):
    merchant = models.ForeignKey(Merchant, ...)
    amount_paise = models.BigIntegerField()   # always positive
    entry_type = models.CharField(...)        # 'credit' or 'debit'
    description = models.CharField(...)
    reference_id = models.CharField(...)      # links to payout ID
    created_at = models.DateTimeField(auto_now_add=True)
    # ↑ No update or delete ever happens to this table.
    # Every row is permanent.
```

**Balance is never stored. It is always computed.**

```
Merchant's full history:
  + ₹2,500  credit  "Payment from Acme Corp USA"
  + ₹1,750  credit  "Payment from TechStart Berlin"
  + ₹3,200  credit  "Payment from Maple Digital"
  - ₹1,000  debit   "Payout to HDFC ••••6789"
  + ₹1,000  credit  "Payout refund: Bank declined"
  - ₹2,000  debit   "Payout to HDFC ••••6789"
             ───────
  = ₹5,450  ← computed by SUM(), not stored anywhere
```

**The invariant this gives us:**

```sql
-- This query must ALWAYS equal what the dashboard shows.
-- If it ever doesn't, there is a bug somewhere and we can find it.
SELECT
  SUM(amount_paise) FILTER (WHERE entry_type = 'credit') -
  SUM(amount_paise) FILTER (WHERE entry_type = 'debit')
FROM payout_engine_ledgerentry
WHERE merchant_id = X;
```

I check this invariant in the test suite. It's not aspirational. It's enforced.

**Also: why paise and not rupees?**

```python
# This is why floats are banned for money:
>>> 0.1 + 0.2
0.30000000000000004    # ← this is not 0.3

# Across a million transactions, this drift becomes real money.
# It's also illegal in audited financial systems.

# The fix: store everything as whole numbers.
# ₹99.99 → 9999 paise. Never loses precision. Ever.
amount = models.BigIntegerField()   # not FloatField, not DecimalField
```

---

## 2. The Lock

### The exact code that prevents two concurrent payouts from overdrawing a balance

```python
# payout_engine/views.py — create_payout()

with transaction.atomic():
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # THIS is the line that makes the whole system safe.
    # select_for_update() tells PostgreSQL:
    # "Give me these rows AND lock them. Nobody else can
    # read or write them until I commit or roll back."
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    locked_entries = LedgerEntry.objects.select_for_update().filter(merchant=merchant)

    agg = locked_entries.aggregate(
        total_credits=Sum('amount_paise', filter=Q(entry_type='credit')),
        total_debits=Sum('amount_paise', filter=Q(entry_type='debit')),
    )
    total_credits = agg['total_credits'] or 0
    total_debits  = agg['total_debits']  or 0

    # Also lock pending payouts — their held funds must count against available balance
    held = Payout.objects.select_for_update().filter(
        merchant=merchant,
        status__in=['pending', 'processing']
    ).aggregate(held=Sum('amount_paise'))['held'] or 0

    available_balance = total_credits - total_debits - held

    if available_balance < amount_paise:
        return Response({'error': 'Insufficient balance'}, status=422)

    # Create payout INSIDE the same transaction — lock doesn't release until here
    payout = Payout.objects.create(
        merchant=merchant,
        bank_account=bank_account,
        amount_paise=amount_paise,
        status='pending',
    )
# ← Transaction commits here. Lock releases here. NOT before.
```

### The database primitive: PostgreSQL SELECT FOR UPDATE

When Django runs `select_for_update()`, PostgreSQL executes:

```sql
SELECT * FROM payout_engine_ledgerentry
WHERE merchant_id = 'some-uuid'
FOR UPDATE;
--  ↑ These two words change everything.
```

`FOR UPDATE` means: give me an **exclusive lock** on every row in this result set. Any other transaction trying to read these same rows (with `FOR UPDATE`) will be **paused at the database level** until I release my lock.

### Why "Python-level locking" doesn't work and why this matters

This was the insight that took me the longest to really understand. Here's the problem:

A Django app in production runs as **multiple processes** — Gunicorn typically starts 4-8 worker processes. A Python `threading.Lock()` only protects within a single process. It has no idea other processes exist.

```
Worker Process 1 ──────────────────────────────────────────────────────▶
                  Python lock acquired
                  reads balance = ₹10,000
                                                    Python lock released

Worker Process 2 ──────────────────────────────────────────────────────▶
                                      Python lock acquired (different process!)
                                      reads balance = ₹10,000  ← SAME WRONG VALUE
                                      creates ₹6,000 payout ← OVERDRAW
```

The only lock that works across all processes is one that lives in **PostgreSQL** — the single shared piece of state that every worker reads and writes. `SELECT FOR UPDATE` is that lock.

### What actually happens with SELECT FOR UPDATE — step by step

```
Merchant balance: ₹10,000 (10,000p)
Two requests arrive simultaneously: both want to withdraw ₹6,000 (6,000p)

Request A (Worker 1):              Request B (Worker 2):
─────────────────────              ─────────────────────
transaction.atomic() starts        transaction.atomic() starts
select_for_update() → LOCK         select_for_update() → BLOCKED ⏸
reads balance = 10,000             (waiting for A to finish)
held = 0
available = 10,000
10,000 >= 6,000 ✓
creates payout for 6,000
COMMITS ✓                          UNBLOCKED ▶
lock releases                      reads balance = 10,000
                                   held = 6,000 (A's pending payout)
                                   available = 10,000 - 6,000 = 4,000
                                   4,000 < 6,000 ✗
                                   returns 422 Insufficient balance ✓
```

One succeeds. One is rejected cleanly. Balance is never overdrawn. This is tested with real threads in the test suite — not just described.

---

## 3. The Idempotency

### How the system knows it has seen a key before

Every payout request must include a UUID in the header:
```
Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000
```

The system stores every key it has processed in this table:

```python
class IdempotencyKey(models.Model):
    key      = models.CharField(max_length=255)
    merchant = models.ForeignKey(Merchant, ...)  # scoped per merchant
    response_body   = models.JSONField()          # the exact response, stored verbatim
    response_status = models.IntegerField()       # the exact HTTP status code
    expires_at      = models.DateTimeField()      # 24 hours from creation

    class Meta:
        unique_together = [['key', 'merchant']]
        # ↑ PostgreSQL enforces this. Two rows with the same key+merchant
        # cannot exist. Not "probably won't." Cannot.
```

When a request comes in, the very first thing we do — before any balance check, before any database writes — is look up the key:

```python
try:
    existing = IdempotencyKey.objects.get(key=idempotency_key, merchant=merchant)
    if not existing.is_expired():
        # We have seen this exact request before.
        # Return the EXACT same response. Don't touch anything.
        return Response(existing.response_body, status=existing.response_status)
    else:
        existing.delete()  # Expired — treat as a fresh request
except IdempotencyKey.DoesNotExist:
    pass  # First time seeing this key — proceed normally
```

The key is saved inside the **same** `transaction.atomic()` block that creates the payout:

```python
with transaction.atomic():
    # ... balance check, SELECT FOR UPDATE ...
    payout = Payout.objects.create(...)      # payout created
    _save_idempotency_key(key, merchant, response, 201)  # key saved
# ↑ Both commit together, or neither commits.
# There is no state where the payout exists but the key doesn't, or vice versa.
```

### What happens if the first request is in-flight when the second arrives

This is the hardest case. Here's exactly what happens:

```
Request A arrives. Key "abc-123" not in database. Proceeds into transaction.atomic().
                                    Request B arrives. Key "abc-123" not in database yet
                                    (A hasn't committed). Proceeds into transaction.atomic().

Request A: creates payout, tries to INSERT idempotency key "abc-123" → succeeds.
Request B: tries to INSERT idempotency key "abc-123" → FAILS with IntegrityError.
           PostgreSQL unique constraint fires. No payout was created by B.
           Django rolls back B's entire transaction.
```

We catch that `IntegrityError` and handle it gracefully:

```python
except IntegrityError:
    # A won the race. Fetch A's stored response and return it.
    # B's caller gets the correct answer as if B had been first.
    try:
        existing = IdempotencyKey.objects.get(key=idempotency_key, merchant=merchant)
        return Response(existing.response_body, status=existing.response_status)
    except IdempotencyKey.DoesNotExist:
        # Extremely rare: A committed and then something deleted it. Tell caller to retry.
        return Response({'error': 'Concurrent conflict. Please retry.'}, status=409)
```

**The result:** No matter how many times the same request is sent — whether sequentially or simultaneously — exactly one payout is created, and every caller gets the same response.

**Keys are scoped per merchant.** Merchant A using key "abc-123" and Merchant B using key "abc-123" are completely independent. The `unique_together = [['key', 'merchant']]` enforces this at the database level.

**Keys expire after 24 hours.** After that, the same UUID can be used for a new request. Expired keys are deleted lazily on the next hit — no cron job needed.

---

## 4. The State Machine

### Where in the code is failed-to-completed blocked

Here is the complete transition map:

```python
# payout_engine/models.py — Payout model

LEGAL_TRANSITIONS = {
    'pending':    ['processing', 'cancelled'],  # can cancel before processing
    'processing': ['completed', 'failed'],       # terminal outcomes only
    'completed':  [],    # ← empty list. No transition from completed is ever legal.
    'failed':     [],    # ← empty list. No transition from failed is ever legal.
    'cancelled':  [],    # ← empty list. No transition from cancelled is ever legal.
}

def can_transition_to(self, new_status):
    return new_status in self.LEGAL_TRANSITIONS.get(self.status, [])

def transition_to(self, new_status, failure_reason=''):
    if not self.can_transition_to(new_status):
        # This raises BEFORE touching the database.
        # The caller's transaction.atomic() catches it and rolls back.
        raise ValueError(
            f"Illegal state transition: {self.status} → {new_status}. "
            f"Legal from {self.status}: {self.LEGAL_TRANSITIONS.get(self.status, [])}"
        )
    self.status = new_status
    if failure_reason:
        self.failure_reason = failure_reason
    if new_status == 'processing':
        self.processing_started_at = timezone.now()
    # Note: does NOT call save(). Caller saves inside their transaction.
```

`failed → completed` is blocked because `LEGAL_TRANSITIONS['failed']` is an empty list. `'completed' in []` is `False`. `can_transition_to` returns `False`. `transition_to` raises `ValueError`. The calling transaction rolls back. Nothing changes in the database.

You cannot bypass this by setting `payout.status = 'completed'` directly — that would skip the state machine entirely. But every place in the codebase that changes payout status goes through `transition_to()`. There is no other path.

### Failed payout returning funds — atomically

This is the most important correctness property in the system:

```python
# payout_engine/tasks.py

def _fail_payout_and_release_funds(payout, reason):
    with transaction.atomic():
        # Step 1: Mark the payout as failed
        payout.transition_to(Payout.FAILED, failure_reason=reason)
        payout.save()

        # Step 2: Return the money to the merchant's available balance
        LedgerEntry.objects.create(
            merchant=payout.merchant,
            amount_paise=payout.amount_paise,
            entry_type='credit',                    # money goes BACK to merchant
            description=f'Payout refund: {reason}',
            reference_id=str(payout.id),
        )
    # Both steps happen in one transaction.
    # If the LedgerEntry insert fails for any reason, the payout.save() rolls back too.
    # The payout stays in its previous state. The retry task picks it up in 30 seconds.
    # The merchant never permanently loses money to a crash.
```

**There is no code path where:**
- A payout is marked `failed` but funds are not returned ✗
- Funds are returned but the payout is not marked `failed` ✗

Both happen together, or neither happens. This is what "atomic" means in practice.

---

## 5. The AI Audit

### What AI gave me (subtly wrong)

I used Claude to help structure the project. When I asked it to write the payout creation view, the first version it gave me had this balance check:

```python
# ❌ WRONG — what the AI wrote
@api_view(['POST'])
def create_payout(request):
    merchant = Merchant.objects.get(id=request.data['merchant_id'])

    # Fetches rows into Python and does arithmetic here
    balance = merchant.get_balance_summary()

    # "Check" happens here in Python
    if balance['available_paise'] >= request.data['amount_paise']:
        # "Act" happens here — SEPARATE database operation
        payout = Payout.objects.create(
            merchant=merchant,
            amount_paise=request.data['amount_paise'],
            status='pending',
        )
        return Response(serialize(payout), status=201)
    else:
        return Response({'error': 'Insufficient balance'}, status=422)
```

This looks completely reasonable. It has a balance check. It creates the payout only if balance is enough. It will work perfectly in testing.

It will silently overdraw accounts in production.

### The three bugs I caught

**Bug 1: The check and the act are not atomic**

There is a window between `if balance['available_paise'] >= amount` and `Payout.objects.create(...)`. Another request can slip through that window. In testing you will never see this because you are the only one hitting the endpoint. In production with real traffic, it happens constantly.

**Bug 2: No row locking means READ COMMITTED lets both transactions proceed**

Even if I wrapped this in `transaction.atomic()`, PostgreSQL's default isolation level (READ COMMITTED) allows both transactions to read the same value before either commits. Wrapping code in a transaction does not automatically make concurrent reads safe. You need an explicit lock.

```python
# This is NOT enough:
with transaction.atomic():
    balance = merchant.get_balance_summary()  # reads, but doesn't lock
    if balance >= amount:
        Payout.objects.create(...)  # still has the race
```

**Bug 3: Python arithmetic on fetched rows**

`get_balance_summary()` fetches rows from the database and does `sum(credits) - sum(debits)` in Python. For display, this is fine. For a safety check that determines whether to move money, you need the database to do the aggregation while holding the lock. If the lock is acquired on the rows, but the arithmetic happens in Python after the rows are fetched, there is a gap between "acquiring the lock" and "using the result of what you read under the lock." Another transaction could insert a new ledger entry between those two moments — one that your Python-level sum never counted.

### What I replaced it with

```python
# ✅ CORRECT — what this codebase actually does
with transaction.atomic():
    # Lock ALL ledger entries for this merchant.
    # No other transaction can read or write these rows until we commit.
    locked_entries = LedgerEntry.objects.select_for_update().filter(merchant=merchant)

    # Aggregation happens AT THE DATABASE LEVEL while holding the lock.
    # There is no window between "reading" and "having the lock."
    agg = locked_entries.aggregate(
        total_credits=Sum('amount_paise', filter=Q(entry_type='credit')),
        total_debits=Sum('amount_paise', filter=Q(entry_type='debit')),
    )

    # Also lock pending payouts — they hold funds that must be subtracted
    held = Payout.objects.select_for_update().filter(
        merchant=merchant,
        status__in=['pending', 'processing']
    ).aggregate(held=Sum('amount_paise'))['held'] or 0

    available = (agg['total_credits'] or 0) - (agg['total_debits'] or 0) - held

    if available < amount_paise:
        return Response({'error': 'Insufficient balance'}, status=422)

    # Payout creation is INSIDE the same atomic block.
    # The lock doesn't release until after this line commits.
    payout = Payout.objects.create(
        merchant=merchant,
        bank_account=bank_account,
        amount_paise=amount_paise,
        status='pending',
    )
```

The difference between these two versions is not style. It is correctness. The first version will cause real financial loss at real scale. The second one won't.

**This is the specific thing I mean when I say I used AI as a tool but understood what it gave me.** The AI wrote working-looking code. It took understanding the PostgreSQL concurrency model to know it was wrong — and understanding `SELECT FOR UPDATE` specifically to know how to fix it.

---

## One more thing

The challenge said "we are not looking for a perfect submission." I took that seriously. What I focused on was getting the hard things right — the things where being wrong costs money. The ledger model. The lock. The idempotency. The atomicity.

If I get a call, I can explain every line of this codebase. Not because I memorised it, but because I understand why each decision was made and what breaks if you make a different one.

That's the kind of engineer I am.