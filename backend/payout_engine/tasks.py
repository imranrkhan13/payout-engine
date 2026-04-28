import json
import random
import logging
import requests
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)

STUCK_THRESHOLD_SECONDS = 30
MAX_ATTEMPTS = 3


@shared_task(bind=True, max_retries=3)
def process_payout(self, payout_id):
    from payout_engine.models import Payout, LedgerEntry

    try:
        with transaction.atomic():
            payout = Payout.objects.select_for_update().get(id=payout_id)
            if payout.status != Payout.PENDING:
                logger.info(f"Payout {payout_id} is not pending (status={payout.status}), skipping.")
                return
            payout.attempt_count += 1
            payout.transition_to(Payout.PROCESSING)
            payout.save()

        outcome = _simulate_bank_response()
        logger.info(f"Payout {payout_id} bank response: {outcome}")

        if outcome == 'hang':
            logger.warning(f"Payout {payout_id} is hanging. Will be retried by beat task.")
            return

        with transaction.atomic():
            payout = Payout.objects.select_for_update().get(id=payout_id)
            if payout.status != Payout.PROCESSING:
                logger.warning(f"Payout {payout_id} status changed during processing, aborting.")
                return

            if outcome == 'success':
                payout.transition_to(Payout.COMPLETED)
                payout.save()
                LedgerEntry.objects.create(
                    merchant=payout.merchant,
                    amount_paise=payout.amount_paise,
                    entry_type='debit',
                    description=f'Payout to bank account ending {payout.bank_account.account_number[-4:]}',
                    reference_id=str(payout.id),
                )
                logger.info(f"Payout {payout_id} completed.")
                # Fire webhook after commit
                _fire_webhooks_for_payout(payout, 'payout.completed')

            elif outcome == 'failure':
                _fail_payout_and_release_funds(payout, reason='Bank declined the transfer.')
                _fire_webhooks_for_payout(payout, 'payout.failed')

    except Payout.DoesNotExist:
        logger.error(f"Payout {payout_id} not found.")
    except ValueError as e:
        logger.error(f"State machine violation for payout {payout_id}: {e}")
    except Exception as exc:
        logger.exception(f"Unexpected error processing payout {payout_id}: {exc}")
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)


@shared_task
def retry_stuck_payouts():
    from payout_engine.models import Payout

    stuck_cutoff = timezone.now() - timedelta(seconds=STUCK_THRESHOLD_SECONDS)
    with transaction.atomic():
        stuck_payouts = Payout.objects.filter(
            status=Payout.PROCESSING,
            processing_started_at__lt=stuck_cutoff,
        ).select_for_update(skip_locked=True)

        for payout in stuck_payouts:
            logger.warning(f"Found stuck payout {payout.id} (attempts={payout.attempt_count})")
            if payout.attempt_count >= MAX_ATTEMPTS:
                _fail_payout_and_release_funds(
                    payout,
                    reason=f'Max retry attempts ({MAX_ATTEMPTS}) exceeded. Payout timed out.'
                )
                _fire_webhooks_for_payout(payout, 'payout.failed')
            else:
                payout.status = Payout.PENDING
                payout.processing_started_at = None
                payout.save()
                process_payout.delay(str(payout.id))


@shared_task(bind=True, max_retries=5)
def deliver_webhook(self, delivery_id):
    """
    Attempts to deliver a single WebhookDelivery.
    Retries with exponential backoff on failure.
    """
    from payout_engine.models import WebhookDelivery

    try:
        delivery = WebhookDelivery.objects.select_related('endpoint').get(id=delivery_id)
    except WebhookDelivery.DoesNotExist:
        return

    if delivery.status == WebhookDelivery.SUCCESS:
        return

    delivery.attempt_count += 1
    delivery.save(update_fields=['attempt_count'])

    try:
        payload_bytes = json.dumps(delivery.payload).encode('utf-8')
        headers = {
            'Content-Type': 'application/json',
            'X-Playto-Event': delivery.event,
            'X-Playto-Delivery': str(delivery.id),
        }
        if delivery.endpoint.secret:
            headers['X-Playto-Signature'] = delivery.endpoint.sign_payload(payload_bytes)

        resp = requests.post(
            delivery.endpoint.url,
            data=payload_bytes,
            headers=headers,
            timeout=10,
        )
        delivery.http_status = resp.status_code
        delivery.response_body = resp.text[:500]

        if 200 <= resp.status_code < 300:
            delivery.status = WebhookDelivery.SUCCESS
            delivery.save()
            logger.info(f"Webhook delivered: {delivery.event} → {delivery.endpoint.url}")
        else:
            raise Exception(f"Non-2xx response: {resp.status_code}")

    except Exception as exc:
        delivery.status = WebhookDelivery.FAILED
        delivery.save()
        countdown = 2 ** self.request.retries * 30  # 30s, 60s, 120s, 240s, 480s
        logger.warning(f"Webhook delivery {delivery_id} failed. Retry in {countdown}s: {exc}")
        raise self.retry(exc=exc, countdown=countdown)


def _simulate_bank_response():
    r = random.random()
    if r < 0.70:
        return 'success'
    elif r < 0.90:
        return 'failure'
    else:
        return 'hang'


def _fail_payout_and_release_funds(payout, reason):
    from payout_engine.models import LedgerEntry
    with transaction.atomic():
        payout.transition_to(payout.FAILED, failure_reason=reason)
        payout.save()
        LedgerEntry.objects.create(
            merchant=payout.merchant,
            amount_paise=payout.amount_paise,
            entry_type='credit',
            description=f'Payout refund: {reason}',
            reference_id=str(payout.id),
        )
        logger.info(f"Payout {payout.id} failed. Rs.{payout.amount_paise/100:.2f} returned to merchant.")


def _fire_webhooks_for_payout(payout, event):
    """Schedule webhook deliveries for all active endpoints subscribed to this event."""
    from payout_engine.models import WebhookEndpoint, WebhookDelivery

    endpoints = WebhookEndpoint.objects.filter(
        merchant=payout.merchant,
        is_active=True,
    )
    payload = {
        'event': event,
        'payout_id': str(payout.id),
        'amount_paise': payout.amount_paise,
        'status': payout.status,
        'merchant_id': str(payout.merchant_id),
        'timestamp': timezone.now().isoformat(),
    }
    for endpoint in endpoints:
        if event in (endpoint.events or []):
            delivery = WebhookDelivery.objects.create(
                endpoint=endpoint,
                payout=payout,
                event=event,
                payload=payload,
            )
            deliver_webhook.delay(str(delivery.id))
