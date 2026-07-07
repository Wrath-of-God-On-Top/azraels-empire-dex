import os

from django.core.management.base import BaseCommand

from settings.models import Settings


class Command(BaseCommand):
    help = "Sets the bot token from the BALLSDEXBOT_TOKEN environment variable, if present."

    def handle(self, *args, **options):
        token = os.environ.get("BALLSDEXBOT_TOKEN")
        if not token:
            self.stdout.write(self.style.WARNING("BALLSDEXBOT_TOKEN not set, skipping."))
            return

        settings_obj = Settings.objects.first()
        if settings_obj is None:
            self.stdout.write(self.style.ERROR("No Settings row found - run migrations first."))
            return

        if settings_obj.bot_token != token:
            settings_obj.bot_token = token
            settings_obj.save()
            self.stdout.write(self.style.SUCCESS("Bot token updated."))
        else:
            self.stdout.write("Bot token already up to date.")
