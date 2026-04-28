from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Merchant',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('email', models.EmailField(unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='BankAccount',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('account_number', models.CharField(max_length=20)),
                ('ifsc_code', models.CharField(max_length=11)),
                ('account_holder_name', models.CharField(max_length=255)),
                ('is_primary', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bank_accounts', to='payout_engine.merchant')),
            ],
        ),
        migrations.CreateModel(
            name='LedgerEntry',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('amount_paise', models.BigIntegerField()),
                ('entry_type', models.CharField(choices=[('credit', 'Credit'), ('debit', 'Debit')], max_length=10)),
                ('description', models.CharField(max_length=500)),
                ('reference_id', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ledger_entries', to='payout_engine.merchant')),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='IdempotencyKey',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('key', models.CharField(max_length=255)),
                ('response_body', models.JSONField()),
                ('response_status', models.IntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField()),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='idempotency_keys', to='payout_engine.merchant')),
            ],
            options={'unique_together': {('key', 'merchant')}},
        ),
        migrations.AddIndex(
            model_name='idempotencykey',
            index=models.Index(fields=['key', 'merchant'], name='payout_engi_key_35a123_idx'),
        ),
        migrations.CreateModel(
            name='Payout',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('amount_paise', models.BigIntegerField()),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('processing', 'Processing'), ('completed', 'Completed'), ('failed', 'Failed')], default='pending', max_length=20)),
                ('failure_reason', models.TextField(blank=True)),
                ('attempt_count', models.IntegerField(default=0)),
                ('processing_started_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('bank_account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payouts', to='payout_engine.bankaccount')),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payouts', to='payout_engine.merchant')),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
