import uuid
from django.db import models
from django.db.models import Sum, Q
from django.utils import timezone
import hmac, hashlib


class Merchant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def get_balance_summary(self):
        """
        Balance is DERIVED from ledger entries, never stored separately.
        This is the invariant: credits - debits = available balance.
        We use DB-level aggregation, not Python arithmetic on fetched rows.
        """
        result = self.ledger_entries.aggregate(
            total_credits=Sum('amount_paise', filter=Q(entry_type='credit')),
            total_debits=Sum('amount_paise', filter=Q(entry_type='debit')),
        )
        total_credits = result['total_credits'] or 0
        total_debits = result['total_debits'] or 0
        available = total_credits - total_debits

        # Held balance = sum of paise held in pending payouts
        held = self.payouts.filter(
            status__in=['pending', 'processing']
        ).aggregate(held=Sum('amount_paise'))['held'] or 0

        return {
            'available_paise': available,
            'held_paise': held,
            'total_credits_paise': total_credits,
            'total_debits_paise': total_debits,
        }

    def __str__(self):
        return self.name


class BankAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name='bank_accounts')
    account_number = models.CharField(max_length=20)
    ifsc_code = models.CharField(max_length=11)
    account_holder_name = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.account_holder_name} - {self.account_number[-4:]}"


class LedgerEntry(models.Model):
    """
    Immutable ledger. Credits and debits are always positive integers.
    Direction is captured in entry_type. Never delete or update entries.
    """
    ENTRY_TYPES = [('credit', 'Credit'), ('debit', 'Debit')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='ledger_entries')
    amount_paise = models.BigIntegerField()  # Always positive. NEVER float.
    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPES)
    description = models.CharField(max_length=500)
    reference_id = models.CharField(max_length=255, blank=True)  # payout ID, payment ID, etc.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        sign = '+' if self.entry_type == 'credit' else '-'
        return f"{sign}₹{self.amount_paise / 100:.2f} | {self.merchant.name}"


class IdempotencyKey(models.Model):
    """
    Stores idempotency keys per merchant. Scoped: same key from different merchants
    are treated as different. Expires after 24 hours.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=255)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name='idempotency_keys')
    # Store the full response so we can replay it exactly
    response_body = models.JSONField()
    response_status = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        unique_together = [['key', 'merchant']]  # scoped per merchant
        indexes = [models.Index(fields=['key', 'merchant'])]

    def is_expired(self):
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"{self.key} | {self.merchant.name}"


class Payout(models.Model):
    """
    State machine: pending -> processing -> completed | failed
    Backwards transitions are illegal and enforced in the model.
    """
    PENDING = 'pending'
    PROCESSING = 'processing'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'

    STATUS_CHOICES = [
        (PENDING, 'Pending'),
        (PROCESSING, 'Processing'),
        (COMPLETED, 'Completed'),
        (FAILED, 'Failed'),
        (CANCELLED, 'Cancelled'),
    ]

    # Legal transitions: what statuses each status can transition TO
    LEGAL_TRANSITIONS = {
        PENDING: [PROCESSING, CANCELLED],   # can cancel before processing starts
        PROCESSING: [COMPLETED, FAILED],
        COMPLETED: [],   # terminal
        FAILED: [],      # terminal
        CANCELLED: [],   # terminal
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='payouts')
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='payouts')
    amount_paise = models.BigIntegerField()  # Always positive. NEVER float.
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)
    failure_reason = models.TextField(blank=True)
    note = models.CharField(max_length=255, blank=True)  # merchant-supplied note/reference
    attempt_count = models.IntegerField(default=0)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def can_transition_to(self, new_status):
        return new_status in self.LEGAL_TRANSITIONS.get(self.status, [])

    def transition_to(self, new_status, failure_reason=''):
        """
        Enforces the state machine. Raises ValueError on illegal transition.
        Does NOT save — caller is responsible for saving within a transaction.
        """
        if not self.can_transition_to(new_status):
            raise ValueError(
                f"Illegal state transition: {self.status} → {new_status}. "
                f"Legal transitions from {self.status}: {self.LEGAL_TRANSITIONS.get(self.status, [])}"
            )
        self.status = new_status
        if failure_reason:
            self.failure_reason = failure_reason
        if new_status == self.PROCESSING:
            self.processing_started_at = timezone.now()

    def __str__(self):
        return f"Payout {self.id} | {self.merchant.name} | ₹{self.amount_paise / 100:.2f} | {self.status}"


# ─── New: Webhook models ──────────────────────────────────────────────────────

class WebhookEndpoint(models.Model):
    """
    A URL the merchant wants us to POST to when payout status changes.
    Each merchant can have multiple webhook endpoints.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name='webhook_endpoints')
    url = models.URLField(max_length=500)
    secret = models.CharField(max_length=64, blank=True)   # HMAC signing secret
    is_active = models.BooleanField(default=True)
    events = models.JSONField(default=list)  # e.g. ["payout.completed", "payout.failed"]
    created_at = models.DateTimeField(auto_now_add=True)

    def sign_payload(self, payload_bytes):
        """Return HMAC-SHA256 signature of the payload using this endpoint's secret."""
        if not self.secret:
            return ''
        return hmac.new(
            self.secret.encode(), payload_bytes, hashlib.sha256
        ).hexdigest()

    def __str__(self):
        return f"{self.merchant.name} → {self.url}"


class WebhookDelivery(models.Model):
    """
    One delivery attempt for one event to one endpoint.
    Tracks success/failure and response for debugging.
    """
    PENDING   = 'pending'
    SUCCESS   = 'success'
    FAILED    = 'failed'

    STATUS_CHOICES = [(PENDING, 'Pending'), (SUCCESS, 'Success'), (FAILED, 'Failed')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name='deliveries')
    payout = models.ForeignKey(Payout, on_delete=models.CASCADE, related_name='webhook_deliveries')
    event = models.CharField(max_length=50)          # e.g. "payout.completed"
    payload = models.JSONField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    http_status = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    attempt_count = models.IntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.event} → {self.endpoint.url} [{self.status}]"
