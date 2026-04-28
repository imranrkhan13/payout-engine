import csv
import uuid
import logging
from datetime import timedelta
from io import StringIO

from django.db import transaction, IntegrityError
from django.db.models import Sum, Q, Count, Avg
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import (
    Merchant, BankAccount, LedgerEntry, IdempotencyKey,
    Payout, WebhookEndpoint, WebhookDelivery
)
from .tasks import process_payout, deliver_webhook

logger = logging.getLogger(__name__)


@api_view(['GET'])
def merchant_list(request):
    merchants = Merchant.objects.all().order_by('name')
    data = [{'id': str(m.id), 'name': m.name, 'email': m.email} for m in merchants]
    return Response(data)


@api_view(['GET'])
def merchant_detail(request, merchant_id):
    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found'}, status=404)

    balance = merchant.get_balance_summary()
    bank_accounts = list(merchant.bank_accounts.values(
        'id', 'account_number', 'ifsc_code', 'account_holder_name', 'is_primary'
    ))
    for acc in bank_accounts:
        acc['id'] = str(acc['id'])
        acc['account_number_masked'] = '*' * (len(acc['account_number']) - 4) + acc['account_number'][-4:]

    return Response({
        'id': str(merchant.id),
        'name': merchant.name,
        'email': merchant.email,
        'balance': balance,
        'bank_accounts': bank_accounts,
    })


@api_view(['GET'])
def merchant_ledger(request, merchant_id):
    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found'}, status=404)

    entries = merchant.ledger_entries.select_related('merchant').order_by('-created_at')[:50]
    data = [{
        'id': str(e.id),
        'amount_paise': e.amount_paise,
        'entry_type': e.entry_type,
        'description': e.description,
        'reference_id': e.reference_id,
        'created_at': e.created_at.isoformat(),
    } for e in entries]
    return Response(data)


@api_view(['GET'])
def merchant_payouts(request, merchant_id):
    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found'}, status=404)

    status_filter = request.GET.get('status')
    qs = merchant.payouts.select_related('bank_account').order_by('-created_at')
    if status_filter:
        qs = qs.filter(status=status_filter)
    data = [_serialize_payout(p) for p in qs[:50]]
    return Response(data)


@api_view(['GET'])
def merchant_analytics(request, merchant_id):
    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found'}, status=404)

    payouts = merchant.payouts.all()

    status_counts = payouts.values('status').annotate(
        count=Count('id'),
        total_paise=Sum('amount_paise'),
    )
    by_status = {
        row['status']: {'count': row['count'], 'total_paise': row['total_paise'] or 0}
        for row in status_counts
    }

    total = payouts.count()
    completed = by_status.get('completed', {}).get('count', 0)
    success_rate = round((completed / total * 100), 1) if total > 0 else 0

    avg = payouts.filter(status=Payout.COMPLETED).aggregate(avg=Avg('amount_paise'))['avg'] or 0

    thirty_days_ago = timezone.now() - timedelta(days=30)
    daily = (
        payouts.filter(created_at__gte=thirty_days_ago, status=Payout.COMPLETED)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(count=Count('id'), total_paise=Sum('amount_paise'))
        .order_by('day')
    )
    daily_data = [
        {'date': row['day'].isoformat(), 'count': row['count'], 'total_paise': row['total_paise'] or 0}
        for row in daily
    ]

    return Response({
        'total_payouts': total,
        'success_rate_pct': success_rate,
        'average_payout_paise': int(avg),
        'by_status': by_status,
        'completed_volume_paise': by_status.get('completed', {}).get('total_paise', 0),
        'daily_volume': daily_data,
    })


@api_view(['GET'])
def export_ledger_csv(request, merchant_id):
    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found'}, status=404)

    entries = merchant.ledger_entries.order_by('-created_at')
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Type', 'Amount (INR)', 'Amount (Paise)', 'Description', 'Reference ID'])
    for e in entries:
        writer.writerow([
            e.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            e.entry_type,
            f'{e.amount_paise / 100:.2f}',
            e.amount_paise,
            e.description,
            e.reference_id,
        ])

    response = HttpResponse(output.getvalue(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="ledger_{merchant.name}_{timezone.now().date()}.csv"'
    return response


@api_view(['POST'])
def add_bank_account(request, merchant_id):
    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found'}, status=404)

    account_number = request.data.get('account_number', '').strip()
    ifsc_code = request.data.get('ifsc_code', '').strip().upper()
    account_holder_name = request.data.get('account_holder_name', '').strip()
    is_primary = request.data.get('is_primary', False)

    if not all([account_number, ifsc_code, account_holder_name]):
        return Response({'error': 'account_number, ifsc_code, and account_holder_name are required'}, status=400)

    if not (8 <= len(account_number) <= 20):
        return Response({'error': 'account_number must be 8-20 digits'}, status=400)

    if len(ifsc_code) != 11:
        return Response({'error': 'ifsc_code must be exactly 11 characters'}, status=400)

    with transaction.atomic():
        if is_primary:
            merchant.bank_accounts.filter(is_primary=True).update(is_primary=False)

        bank = BankAccount.objects.create(
            merchant=merchant,
            account_number=account_number,
            ifsc_code=ifsc_code,
            account_holder_name=account_holder_name,
            is_primary=is_primary,
        )

    return Response({
        'id': str(bank.id),
        'account_number_masked': '*' * (len(account_number) - 4) + account_number[-4:],
        'ifsc_code': bank.ifsc_code,
        'account_holder_name': bank.account_holder_name,
        'is_primary': bank.is_primary,
        'created_at': bank.created_at.isoformat(),
    }, status=201)


@api_view(['POST'])
def set_primary_bank_account(request, merchant_id, account_id):
    try:
        merchant = Merchant.objects.get(id=merchant_id)
        bank = BankAccount.objects.get(id=account_id, merchant=merchant)
    except (Merchant.DoesNotExist, BankAccount.DoesNotExist):
        return Response({'error': 'Not found'}, status=404)

    with transaction.atomic():
        merchant.bank_accounts.update(is_primary=False)
        bank.is_primary = True
        bank.save()

    return Response({'success': True, 'primary_account_id': str(bank.id)})


@api_view(['POST'])
def create_payout(request):
    idempotency_key = request.headers.get('Idempotency-Key', '').strip()
    if not idempotency_key:
        return Response({'error': 'Idempotency-Key header is required'}, status=400)

    try:
        uuid.UUID(idempotency_key)
    except ValueError:
        return Response({'error': 'Idempotency-Key must be a valid UUID'}, status=400)

    merchant_id = request.data.get('merchant_id')
    amount_paise = request.data.get('amount_paise')
    bank_account_id = request.data.get('bank_account_id')
    note = request.data.get('note', '').strip()[:255]

    if not all([merchant_id, amount_paise, bank_account_id]):
        return Response({'error': 'merchant_id, amount_paise, and bank_account_id are required'}, status=400)

    try:
        amount_paise = int(amount_paise)
    except (TypeError, ValueError):
        return Response({'error': 'amount_paise must be an integer'}, status=400)

    if amount_paise <= 0:
        return Response({'error': 'amount_paise must be positive'}, status=400)

    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found'}, status=404)

    try:
        existing = IdempotencyKey.objects.get(key=idempotency_key, merchant=merchant)
        if not existing.is_expired():
            return Response(existing.response_body, status=existing.response_status)
        else:
            existing.delete()
    except IdempotencyKey.DoesNotExist:
        pass

    try:
        bank_account = BankAccount.objects.get(id=bank_account_id, merchant=merchant)
    except BankAccount.DoesNotExist:
        return Response({'error': 'Bank account not found or does not belong to this merchant'}, status=404)

    try:
        with transaction.atomic():
            locked_entries = LedgerEntry.objects.select_for_update().filter(merchant=merchant)
            agg = locked_entries.aggregate(
                total_credits=Sum('amount_paise', filter=Q(entry_type='credit')),
                total_debits=Sum('amount_paise', filter=Q(entry_type='debit')),
            )
            total_credits = agg['total_credits'] or 0
            total_debits = agg['total_debits'] or 0

            held = Payout.objects.select_for_update().filter(
                merchant=merchant,
                status__in=[Payout.PENDING, Payout.PROCESSING]
            ).aggregate(held=Sum('amount_paise'))['held'] or 0

            available_balance = total_credits - total_debits - held

            if available_balance < amount_paise:
                response_body = {
                    'error': 'Insufficient balance',
                    'available_paise': available_balance,
                    'requested_paise': amount_paise,
                }
                response_status_code = 422
                _save_idempotency_key(idempotency_key, merchant, response_body, response_status_code)
                return Response(response_body, status=response_status_code)

            payout = Payout.objects.create(
                merchant=merchant,
                bank_account=bank_account,
                amount_paise=amount_paise,
                status=Payout.PENDING,
                note=note,
            )
            response_body = _serialize_payout(payout)
            response_status_code = 201
            _save_idempotency_key(idempotency_key, merchant, response_body, response_status_code)

    except IntegrityError:
        try:
            existing = IdempotencyKey.objects.get(key=idempotency_key, merchant=merchant)
            return Response(existing.response_body, status=existing.response_status)
        except IdempotencyKey.DoesNotExist:
            return Response({'error': 'Concurrent request conflict. Please retry.'}, status=409)

    process_payout.delay(str(payout.id))
    logger.info(f"Payout {payout.id} created and queued.")
    return Response(response_body, status=response_status_code)


@api_view(['POST'])
def cancel_payout(request, payout_id):
    try:
        merchant_id = request.data.get('merchant_id')
        with transaction.atomic():
            payout = Payout.objects.select_for_update().get(id=payout_id)

            if merchant_id and str(payout.merchant_id) != str(merchant_id):
                return Response({'error': 'Payout does not belong to this merchant'}, status=403)

            if not payout.can_transition_to(Payout.CANCELLED):
                return Response({
                    'error': f'Cannot cancel a {payout.status} payout. Only pending payouts can be cancelled.',
                    'current_status': payout.status,
                }, status=409)

            payout.transition_to(Payout.CANCELLED, failure_reason='Cancelled by merchant.')
            payout.save()

            LedgerEntry.objects.create(
                merchant=payout.merchant,
                amount_paise=payout.amount_paise,
                entry_type='credit',
                description=f'Payout cancellation refund (ref: {str(payout.id)[:8]})',
                reference_id=str(payout.id),
            )

    except Payout.DoesNotExist:
        return Response({'error': 'Payout not found'}, status=404)

    return Response(_serialize_payout(payout))


@api_view(['GET'])
def payout_detail(request, payout_id):
    try:
        payout = Payout.objects.select_related('bank_account', 'merchant').get(id=payout_id)
    except Payout.DoesNotExist:
        return Response({'error': 'Payout not found'}, status=404)
    return Response(_serialize_payout(payout))


@api_view(['GET', 'POST'])
def webhook_endpoints(request, merchant_id):
    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found'}, status=404)

    if request.method == 'GET':
        endpoints = merchant.webhook_endpoints.all()
        return Response([_serialize_webhook_endpoint(e) for e in endpoints])

    url = request.data.get('url', '').strip()
    secret = request.data.get('secret', '').strip()
    events = request.data.get('events', ['payout.completed', 'payout.failed', 'payout.cancelled'])

    if not url:
        return Response({'error': 'url is required'}, status=400)

    endpoint = WebhookEndpoint.objects.create(
        merchant=merchant, url=url, secret=secret, events=events, is_active=True,
    )
    return Response(_serialize_webhook_endpoint(endpoint), status=201)


@api_view(['DELETE'])
def delete_webhook_endpoint(request, merchant_id, endpoint_id):
    try:
        merchant = Merchant.objects.get(id=merchant_id)
        endpoint = WebhookEndpoint.objects.get(id=endpoint_id, merchant=merchant)
    except (Merchant.DoesNotExist, WebhookEndpoint.DoesNotExist):
        return Response({'error': 'Not found'}, status=404)
    endpoint.delete()
    return Response({'success': True})


@api_view(['GET'])
def webhook_deliveries(request, merchant_id):
    try:
        merchant = Merchant.objects.get(id=merchant_id)
    except Merchant.DoesNotExist:
        return Response({'error': 'Merchant not found'}, status=404)

    deliveries = WebhookDelivery.objects.filter(
        endpoint__merchant=merchant
    ).select_related('endpoint', 'payout').order_by('-created_at')[:30]

    data = [{
        'id': str(d.id),
        'event': d.event,
        'payout_id': str(d.payout_id),
        'endpoint_url': d.endpoint.url,
        'status': d.status,
        'http_status': d.http_status,
        'attempt_count': d.attempt_count,
        'created_at': d.created_at.isoformat(),
    } for d in deliveries]
    return Response(data)


@api_view(['GET'])
def platform_summary(request):
    total_merchants = Merchant.objects.count()
    total_payouts = Payout.objects.count()
    completed = Payout.objects.filter(status=Payout.COMPLETED).aggregate(
        count=Count('id'), volume=Sum('amount_paise')
    )
    pending_count = Payout.objects.filter(status__in=[Payout.PENDING, Payout.PROCESSING]).count()

    return Response({
        'total_merchants': total_merchants,
        'total_payouts': total_payouts,
        'completed_payouts': completed['count'] or 0,
        'completed_volume_paise': completed['volume'] or 0,
        'pending_processing_count': pending_count,
    })


def _serialize_payout(payout):
    return {
        'id': str(payout.id),
        'merchant_id': str(payout.merchant_id),
        'amount_paise': payout.amount_paise,
        'status': payout.status,
        'failure_reason': payout.failure_reason,
        'note': getattr(payout, 'note', ''),
        'attempt_count': payout.attempt_count,
        'bank_account_id': str(payout.bank_account_id),
        'created_at': payout.created_at.isoformat(),
        'updated_at': payout.updated_at.isoformat(),
    }


def _serialize_webhook_endpoint(endpoint):
    return {
        'id': str(endpoint.id),
        'url': endpoint.url,
        'is_active': endpoint.is_active,
        'events': endpoint.events,
        'has_secret': bool(endpoint.secret),
        'created_at': endpoint.created_at.isoformat(),
    }


def _save_idempotency_key(key, merchant, response_body, response_status):
    IdempotencyKey.objects.create(
        key=key,
        merchant=merchant,
        response_body=response_body,
        response_status=response_status,
        expires_at=timezone.now() + timedelta(hours=24),
    )
