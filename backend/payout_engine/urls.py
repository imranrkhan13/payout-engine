from django.urls import path
from . import views

urlpatterns = [
    path('merchants/', views.merchant_list, name='merchant-list'),
    path('merchants/<uuid:merchant_id>/', views.merchant_detail, name='merchant-detail'),
    path('merchants/<uuid:merchant_id>/ledger/', views.merchant_ledger, name='merchant-ledger'),
    path('merchants/<uuid:merchant_id>/ledger/export/', views.export_ledger_csv, name='ledger-export'),
    path('merchants/<uuid:merchant_id>/payouts/', views.merchant_payouts, name='merchant-payouts'),
    path('merchants/<uuid:merchant_id>/analytics/', views.merchant_analytics, name='merchant-analytics'),
    path('merchants/<uuid:merchant_id>/bank-accounts/', views.add_bank_account, name='add-bank-account'),
    path('merchants/<uuid:merchant_id>/bank-accounts/<uuid:account_id>/set-primary/', views.set_primary_bank_account, name='set-primary-bank-account'),
    path('merchants/<uuid:merchant_id>/webhooks/', views.webhook_endpoints, name='webhook-endpoints'),
    path('merchants/<uuid:merchant_id>/webhooks/<uuid:endpoint_id>/', views.delete_webhook_endpoint, name='delete-webhook-endpoint'),
    path('merchants/<uuid:merchant_id>/webhook-deliveries/', views.webhook_deliveries, name='webhook-deliveries'),
    path('payouts/', views.create_payout, name='create-payout'),
    path('payouts/<uuid:payout_id>/', views.payout_detail, name='payout-detail'),
    path('payouts/<uuid:payout_id>/cancel/', views.cancel_payout, name='cancel-payout'),
    path('summary/', views.platform_summary, name='platform-summary'),
]
