from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('payout_engine', '0001_initial'),
    ]

    operations = [
        # Add cancelled status + note field to Payout
        migrations.AddField(
            model_name='payout',
            name='note',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name='payout',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('processing', 'Processing'),
                    ('completed', 'Completed'),
                    ('failed', 'Failed'),
                    ('cancelled', 'Cancelled'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
        # WebhookEndpoint model
        migrations.CreateModel(
            name='WebhookEndpoint',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('url', models.URLField(max_length=500)),
                ('secret', models.CharField(blank=True, max_length=64)),
                ('is_active', models.BooleanField(default=True)),
                ('events', models.JSONField(default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='webhook_endpoints', to='payout_engine.merchant')),
            ],
        ),
        # WebhookDelivery model
        migrations.CreateModel(
            name='WebhookDelivery',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('event', models.CharField(max_length=50)),
                ('payload', models.JSONField()),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('success', 'Success'), ('failed', 'Failed')], default='pending', max_length=10)),
                ('http_status', models.IntegerField(blank=True, null=True)),
                ('response_body', models.TextField(blank=True)),
                ('attempt_count', models.IntegerField(default=0)),
                ('next_retry_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('endpoint', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='deliveries', to='payout_engine.webhookendpoint')),
                ('payout', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='webhook_deliveries', to='payout_engine.payout')),
            ],
        ),
    ]
