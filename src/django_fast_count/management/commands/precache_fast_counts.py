import time
from django.core.management.base import BaseCommand
from django.apps import apps
from django_fast_count.managers import FastCountModelManager

class Command(BaseCommand):
    help = "Precaches counts for models using FastCountModelManager."

    def handle(self, *args, **options):
        start_time = time.time()
        self.stdout.write("Starting fast count precaching...")
        processed_managers = 0
        processed_models = set()

        all_models = apps.get_models()

        for model in all_models:
            # Use _meta.managers_map which is safer for finding all managers
            managers = getattr(model._meta, "managers_map", {})
            if not managers and hasattr(model, "objects"): # Fallback for simpler cases
                managers = {"objects": model.objects}

            found_fast_manager_on_model = False

            for manager_name, manager_instance in managers.items():
                if isinstance(manager_instance, FastCountModelManager):
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
                        results = manager_instance.precache_counts(manager_name=manager_name)
                        self.stdout.write(f"  Precached counts for {len(results)} querysets:")
                        for key, result in results.items():
                            if isinstance(result, int):
                                self.stdout.write(f"    - Hash {key[:8]}...: {result}")
                            else:
                                self.stdout.write(self.style.WARNING(f"    - Hash {key[:8]}...: {result}"))
                    except Exception as e:
                        self.stderr.write(
                            self.style.ERROR(
                                f"  Error precaching for {model._meta.app_label}.{model.__name__} "
                                f"('{manager_name}'): {e}"
                            )
                        )

            if found_fast_manager_on_model:
                processed_models.add(f"{model._meta.app_label}.{model.__name__}")

        end_time = time.time()
        duration = end_time - start_time

        self.stdout.write("-" * 30)
        if processed_managers > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully processed {processed_managers} FastCountModelManager instances "
                    f"across {len(processed_models)} models in {duration:.2f} seconds."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "No models found using FastCountModelManager. No counts were precached."
                )
            )
