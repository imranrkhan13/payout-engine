from django.contrib import admin
from .models import Merchant, BankAccount, LedgerEntry, IdempotencyKey, Payout, WebhookEndpoint, WebhookDelivery

@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'created_at']

@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ['merchant', 'account_holder_name', 'account_number', 'ifsc_code', 'is_primary']

@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ['merchant', 'entry_type', 'amount_paise', 'description', 'created_at']
    list_filter = ['entry_type', 'merchant']

@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ['merchant', 'amount_paise', 'status', 'attempt_count', 'note', 'created_at']
    list_filter = ['status', 'merchant']

@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ['key', 'merchant', 'response_status', 'created_at', 'expires_at']

@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    list_display = ['merchant', 'url', 'is_active', 'created_at']
    list_filter = ['is_active', 'merchant']

@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ['event', 'endpoint', 'status', 'http_status', 'attempt_count', 'created_at']
    list_filter = ['status', 'event']
