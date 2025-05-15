from django.core.management.base import BaseCommand
from django.apps import apps
from django.utils import timezone

from django_fast_count.managers import FastCountManager
from django_fast_count.models import FastCount


class Command(BaseCommand):
    help = "Precaches counts for models using FastCountManager."

    def handle(self, *args, **options):
        start_time = timezone.now()
        self.stdout.write("Starting fast count precaching...")
        processed_managers = 0
        processed_models = set()

        all_models = apps.get_models()

        for model in all_models:
            # Use _meta.managers_map which is safer for finding all managers
            managers = getattr(model._meta, "managers_map", {})
            if not managers and hasattr(model, "objects"):  # Fallback for simpler cases
                managers = {"objects": model.objects}

            found_fast_manager_on_model = False

            for manager_name, manager_instance in managers.items():
                if isinstance(manager_instance, FastCountManager):
                    found_fast_manager_on_model = True
                    processed_managers += 1
                    self.stdout.write(
                        self.style.NOTICE(
                            f"Processing: {model._meta.app_label}.{model.__name__} "
                            f"(manager: '{manager_name}')"
                        )
                    )

                    try:
                        # The manager instance needs access to its name on the model
                        results = manager_instance.precache_counts(
                            manager_name=manager_name
                        )
                        self.stdout.write(
                            f"  Precached counts for {len(results)} querysets:"
                        )
                        for key, result in results.items():
                            if isinstance(result, int):
                                self.stdout.write(f"    - Hash {key[:8]}...: {result}")
                            else:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"    - Hash {key[:8]}...: {result}"
                                    )
                                )
                    except Exception as e:
                        self.stderr.write(
                            self.style.ERROR(
                                f"  Error precaching for {model._meta.app_label}.{model.__name__} "
                                f"('{manager_name}'): {e}"
                            )
                        )

            if found_fast_manager_on_model:
                processed_models.add(f"{model._meta.app_label}.{model.__name__}")

        end_time = timezone.now()
        duration = end_time - start_time

        self.stdout.write("-" * 30)
        if processed_managers > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully processed {processed_managers} FastCountManager instances "
                    f"across {len(processed_models)} models in {duration.total_seconds():.2f} seconds."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "No models found using FastCountManager. No counts were precached."
                )
            )

        # Delete all expired FastCount entries
        self.stdout.write("-" * 30)
        expired_counts = FastCount.objects.filter(expires_at__lt=timezone.now())
        num_expired = expired_counts.count()
        if num_expired > 0:
            expired_counts.delete()
            self.stdout.write(
                self.style.SUCCESS(f"Deleted {num_expired} expired FastCount entries.")
            )
        else:
            self.stdout.write(self.style.WARNING("No FastCount entries were expired."))
