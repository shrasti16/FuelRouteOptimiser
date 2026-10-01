import time

from django.core.management.base import BaseCommand

from routing.models import FuelStation
from routing.services import get_station_location


class Command(BaseCommand):
    help = "Geocode fuel stations and populate their Location records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of stations to process.",
        )

        parser.add_argument(
            "--delay",
            type=float,
            default=0.2,
            help="Delay between geocoding requests in seconds.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        delay = options["delay"]

        queryset = (
            FuelStation.objects
            .filter(location__isnull=True)
            .order_by("id")
        )

        if limit is not None:
            queryset = queryset[:limit]

        stations = list(queryset)
        total = len(stations)

        self.stdout.write(
            self.style.NOTICE(
                f"Stations to process: {total}"
            )
        )

        success = 0
        failed = 0

        for index, station in enumerate(stations, start=1):
            self.stdout.write(
                f"[{index}/{total}] "
                f"{station.truckstop_name} - "
                f"{station.city}, {station.state}"
            )

            try:
                location = get_station_location(station)

                if location:
                    success += 1

                    self.stdout.write(
                        self.style.SUCCESS(
                            "  Location saved."
                        )
                    )
                else:
                    failed += 1

                    self.stdout.write(
                        self.style.WARNING(
                            "  Location not found."
                        )
                    )

            except Exception as exc:
                failed += 1

                self.stdout.write(
                    self.style.ERROR(
                        f"  Failed: {exc}"
                    )
                )

            if delay > 0:
                time.sleep(delay)

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "Station location population completed."
            )
        )

        self.stdout.write(
            f"Successful: {success}"
        )

        self.stdout.write(
            f"Failed: {failed}"
        )