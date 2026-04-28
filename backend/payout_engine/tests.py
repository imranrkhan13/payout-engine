"""
Tests for the two most critical behaviors:
1. Concurrency: two simultaneous payouts cannot overdraw balance
2. Idempotency: same key returns same response, no duplicate payout
"""
import uuid
import threading
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient
from payout_engine.models import Merchant, BankAccount, LedgerEntry, Payout, IdempotencyKey
from django.utils import timezone
from datetime import timedelta


def create_merchant_with_balance(name, email, balance_paise):
    merchant = Merchant.objects.create(name=name, email=email)
    bank = BankAccount.objects.create(
        merchant=merchant,
        account_number='50100123456789',
        ifsc_code='HDFC0001234',
        account_holder_name=name,
        is_primary=True,
    )
    LedgerEntry.objects.create(
        merchant=merchant,
        amount_paise=balance_paise,
        entry_type='credit',
        description='Initial test credit',
    )
    return merchant, bank


class ConcurrencyTest(TransactionTestCase):
    """
    TransactionTestCase is used (not TestCase) because we need real DB transactions
    to test SELECT FOR UPDATE behavior. TestCase wraps everything in one transaction
    which would make locking tests meaningless.
    """

    def test_concurrent_payouts_cannot_overdraw(self):
        """
        Merchant has ₹100 (10000 paise).
        Two threads simultaneously request ₹60 payouts.
        Exactly one should succeed, one should fail with 422.
        """
        merchant, bank = create_merchant_with_balance(
            'Concurrency Test Merchant', 'concurrent@test.com', 10000
        )

        results = []
        errors = []

        def make_payout_request(thread_num):
            client = APIClient()
            try:
                response = client.post(
                    '/api/v1/payouts/',
                    data={
                        'merchant_id': str(merchant.id),
                        'amount_paise': 6000,
                        'bank_account_id': str(bank.id),
                    },
                    format='json',
                    headers={'Idempotency-Key': str(uuid.uuid4())},
                )
                results.append(response.status_code)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=make_payout_request, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(len(results), 2)

        # Exactly one should succeed (201) and one should fail (422)
        self.assertIn(201, results, "At least one payout should have succeeded")
        self.assertIn(422, results, "At least one payout should have been rejected")

        # Verify only one payout was actually created
        payouts = Payout.objects.filter(merchant=merchant)
        self.assertEqual(payouts.count(), 1)

        # Verify ledger invariant: balance never went negative
        balance = merchant.get_balance_summary()
        self.assertGreaterEqual(balance['available_paise'], 0)

        print(f"\n✓ Concurrency test passed: results={results}, payouts created={payouts.count()}")


class IdempotencyTest(TestCase):

    def setUp(self):
        self.merchant, self.bank = create_merchant_with_balance(
            'Idempotency Test Merchant', 'idempotent@test.com', 100000
        )
        self.client = APIClient()
        self.idempotency_key = str(uuid.uuid4())

    def _make_payout(self, key=None, amount=5000):
        return self.client.post(
            '/api/v1/payouts/',
            data={
                'merchant_id': str(self.merchant.id),
                'amount_paise': amount,
                'bank_account_id': str(self.bank.id),
            },
            format='json',
            headers={'Idempotency-Key': key or self.idempotency_key},
        )

    def test_same_key_returns_same_response(self):
        """Second call with same key must return identical response."""
        r1 = self._make_payout()
        r2 = self._make_payout()

        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r1.data['id'], r2.data['id'])  # Same payout object

        # Only one payout should exist
        self.assertEqual(Payout.objects.filter(merchant=self.merchant).count(), 1)
        print(f"\n✓ Idempotency test passed: both calls returned payout {r1.data['id']}")

    def test_different_keys_create_different_payouts(self):
        """Different keys should create separate payouts."""
        r1 = self._make_payout(key=str(uuid.uuid4()))
        r2 = self._make_payout(key=str(uuid.uuid4()))

        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r1.data['id'], r2.data['id'])
        self.assertEqual(Payout.objects.filter(merchant=self.merchant).count(), 2)

    def test_expired_key_allows_new_payout(self):
        """An expired idempotency key should be treated as new."""
        # Create a key that's already expired
        IdempotencyKey.objects.create(
            key=self.idempotency_key,
            merchant=self.merchant,
            response_body={'id': 'old-payout-id', 'status': 'pending'},
            response_status=201,
            expires_at=timezone.now() - timedelta(hours=1),  # already expired
        )

        r = self._make_payout()
        # Should create a new payout, not return the expired cached response
        self.assertEqual(r.status_code, 201)
        self.assertNotEqual(r.data.get('id'), 'old-payout-id')

    def test_missing_idempotency_key_returns_400(self):
        """Request without Idempotency-Key header should be rejected."""
        r = self.client.post(
            '/api/v1/payouts/',
            data={
                'merchant_id': str(self.merchant.id),
                'amount_paise': 5000,
                'bank_account_id': str(self.bank.id),
            },
            format='json',
        )
        self.assertEqual(r.status_code, 400)

    def test_key_scoped_per_merchant(self):
        """Same key used by different merchants should work independently."""
        merchant2, bank2 = create_merchant_with_balance('Merchant 2', 'm2@test.com', 50000)
        shared_key = str(uuid.uuid4())

        r1 = self._make_payout(key=shared_key)
        r2 = self.client.post(
            '/api/v1/payouts/',
            data={
                'merchant_id': str(merchant2.id),
                'amount_paise': 5000,
                'bank_account_id': str(bank2.id),
            },
            format='json',
            headers={'Idempotency-Key': shared_key},
        )

        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        # Different payouts created for different merchants with same key
        self.assertNotEqual(r1.data['id'], r2.data['id'])


class StateMachineTest(TestCase):

    def setUp(self):
        self.merchant, self.bank = create_merchant_with_balance(
            'State Machine Merchant', 'statemachine@test.com', 100000
        )

    def test_illegal_transition_raises(self):
        """completed → pending must be rejected."""
        payout = Payout.objects.create(
            merchant=self.merchant,
            bank_account=self.bank,
            amount_paise=5000,
            status=Payout.COMPLETED,
        )
        with self.assertRaises(ValueError):
            payout.transition_to(Payout.PENDING)

    def test_failed_to_completed_raises(self):
        """failed → completed must be rejected."""
        payout = Payout.objects.create(
            merchant=self.merchant,
            bank_account=self.bank,
            amount_paise=5000,
            status=Payout.FAILED,
        )
        with self.assertRaises(ValueError):
            payout.transition_to(Payout.COMPLETED)

    def test_legal_transitions_succeed(self):
        """pending → processing → completed should work."""
        payout = Payout.objects.create(
            merchant=self.merchant,
            bank_account=self.bank,
            amount_paise=5000,
            status=Payout.PENDING,
        )
        payout.transition_to(Payout.PROCESSING)
        self.assertEqual(payout.status, Payout.PROCESSING)
        payout.transition_to(Payout.COMPLETED)
        self.assertEqual(payout.status, Payout.COMPLETED)


class LedgerInvariantTest(TestCase):

    def test_balance_equals_credits_minus_debits(self):
        """The ledger invariant: sum(credits) - sum(debits) == available balance."""
        merchant, bank = create_merchant_with_balance('Invariant Merchant', 'inv@test.com', 50000)

        LedgerEntry.objects.create(
            merchant=merchant, amount_paise=10000, entry_type='debit',
            description='Test debit'
        )

        balance = merchant.get_balance_summary()
        self.assertEqual(balance['available_paise'], 40000)
        self.assertEqual(balance['total_credits_paise'], 50000)
        self.assertEqual(balance['total_debits_paise'], 10000)
