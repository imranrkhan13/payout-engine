from django.apps import AppConfig

class PayoutEngineConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'payout_engine'

    def ready(self):
        import os

        # prevent double execution
        if os.environ.get('RUN_MAIN') != 'true':
            return

        from django.db.utils import OperationalError

        try:
            from .models import Merchant

            if Merchant.objects.count() == 0:
                from .management.commands.seed_data import Command
                Command().handle()

        except OperationalError:
            pass