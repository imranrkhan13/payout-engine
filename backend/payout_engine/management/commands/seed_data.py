"""
Management command: python manage.py seed_data
Seeds 3 merchants with bank accounts and credit history.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from payout_engine.models import Merchant, BankAccount, LedgerEntry, Payout, IdempotencyKey


class Command(BaseCommand):
    help = 'Seed the database with test merchants, bank accounts, and credit history'

    def handle(self, *args, **kwargs):
        self.stdout.write('Seeding data...')

        with transaction.atomic():
            # Clear existing data
            Payout.objects.all().delete()
            IdempotencyKey.objects.all().delete()
            LedgerEntry.objects.all().delete()
            BankAccount.objects.all().delete()
            Merchant.objects.all().delete()

            merchants_data = [
                {
                    'name': 'Arjun Sharma Design Co.',
                    'email': 'arjun@sharmadesign.in',
                    'bank': {
                        'account_number': '50100123456789',
                        'ifsc_code': 'HDFC0001234',
                        'account_holder_name': 'Arjun Sharma',
                    },
                    'credits': [
                        (250000, 'Payment from Acme Corp USA - Invoice #1001'),
                        (175000, 'Payment from TechStart Berlin - Invoice #1002'),
                        (320000, 'Payment from Maple Digital Canada - Invoice #1003'),
                    ],
                },
                {
                    'name': 'Priya Freelance Tech',
                    'email': 'priya@freelancetech.in',
                    'bank': {
                        'account_number': '40200987654321',
                        'ifsc_code': 'ICIC0005678',
                        'account_holder_name': 'Priya Nair',
                    },
                    'credits': [
                        (500000, 'Payment from SkyBridge LLC USA - Invoice #2001'),
                        (280000, 'Payment from Nordic Apps Sweden - Invoice #2002'),
                    ],
                },
                {
                    'name': 'Velocity Agency Mumbai',
                    'email': 'accounts@velocityagency.in',
                    'bank': {
                        'account_number': '60300456789012',
                        'ifsc_code': 'SBIN0009012',
                        'account_holder_name': 'Velocity Agency Pvt Ltd',
                    },
                    'credits': [
                        (1000000, 'Payment from Global Ventures UK - Invoice #3001'),
                        (750000, 'Payment from OceanBlue Media AUS - Invoice #3002'),
                        (420000, 'Payment from Sunrise Studios USA - Invoice #3003'),
                        (380000, 'Payment from DataFlow GmbH Germany - Invoice #3004'),
                    ],
                },
            ]

            for m_data in merchants_data:
                merchant = Merchant.objects.create(
                    name=m_data['name'],
                    email=m_data['email'],
                )
                BankAccount.objects.create(
                    merchant=merchant,
                    is_primary=True,
                    **m_data['bank'],
                )
                for amount, description in m_data['credits']:
                    LedgerEntry.objects.create(
                        merchant=merchant,
                        amount_paise=amount,
                        entry_type='credit',
                        description=description,
                        reference_id=f'sim_pay_{merchant.id}',
                    )

                balance = merchant.get_balance_summary()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✓ {merchant.name} — Balance: ₹{balance['available_paise'] / 100:,.2f}"
                    )
                )

        self.stdout.write(self.style.SUCCESS('\nSeeding complete!'))
