from django.db import models


class Location(models.Model):
    location_key = models.CharField(max_length=255, unique=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=7)
    longitude = models.DecimalField(max_digits=10, decimal_places=7)
    formatted_name = models.CharField(max_length=500, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.formatted_name or self.location_key

class FuelStation(models.Model):
    opis_truckstop_id = models.CharField(
        max_length=100,
        unique=True,
    )

    truckstop_name = models.CharField(max_length=255)

    address = models.CharField(max_length=500)

    city = models.CharField(max_length=100)

    state = models.CharField(max_length=50)

    rack_id = models.CharField(max_length=100)

    retail_price = models.DecimalField(
        max_digits=6,
        decimal_places=3,
    )
    
    location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fuel_stations",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.truckstop_name} - {self.city}, {self.state}"


