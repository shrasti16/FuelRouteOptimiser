import csv
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand

from routing.models import FuelStation


class Command(BaseCommand):
    help = "Import fuel station data from CSV"

    def handle(self, *args, **options):

        csv_path = Path("fuel-prices-for-be-assessment.csv")

        if not csv_path.exists():
            self.stdout.write(
                self.style.ERROR("CSV file not found.")
            )
            return

        created_count = 0
        updated_count = 0

        with open(csv_path, "r", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)

            for row in reader:
                station, created = FuelStation.objects.update_or_create(
                    opis_truckstop_id=row["OPIS Truckstop ID"],
                    defaults={
                        "truckstop_name": row["Truckstop Name"],
                        "address": row["Address"],
                        "city": row["City"],
                        "state": row["State"],
                        "rack_id": row["Rack ID"],
                        "retail_price": Decimal(row["Retail Price"]),
                    },
                )

                if created:
                    created_count += 1
                else:
                    updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Import completed. Created: {created_count}, "
                f"Updated: {updated_count}"
            )
        )