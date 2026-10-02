# FuelRouteOptimiser

A Django REST API for finding a fuel-efficient route between two locations in the USA.

The API takes a start location and destination, calculates the route, finds fuel stations along the route, and calculates the fuel cost based on the available fuel prices.

## Tech Used

* Python
* Django
* Django REST Framework
* PostgreSQL
* Redis
* Geoapify API

## What it does

The API:

* Takes start and destination addresses
* Gets the coordinates of the locations
* Calculates the driving route
* Finds fuel stations near the route
* Calculates the fuel required for the trip
* Selects fuel stops based on fuel price and distance
* Supports multiple fuel stops for longer routes
* Calculates the total fuel cost
* Returns the route geometry along with the fuel-stop details

The vehicle range is limited to 500 miles and the default fuel efficiency is 10 MPG.

## Project Structure


FuelRouteOptimiser/
│
├── config/
├── routing/
│   ├── management/
│   ├── models.py
│   ├── services.py
│   ├── views.py
│   ├── urls.py
│   └── redis_client.py
│
├── fuel-prices-for-be-assessment.csv
├── manage.py
├── requirements.txt
├── .env
└── .gitignore


## Setup

Clone the repository:


git clone https://github.com/shrasti16/FuelRouteOptimiser.git
cd FuelRouteOptimiser


Create a virtual environment:

python -m venv fuel_root_optimiser_venv

Activate it on Windows:

fuel_root_optimiser_venv\Scripts\activate

Install the packages:

pip install -r requirements.txt


Create a `.env` file in the project root:


SECRET_KEY=your-secret-key

GEOAPIFY_API_KEY=your-geoapify-api-key

DB_NAME=fuelroute
DB_USER=postgres
DB_PASSWORD=your-postgres-password
DB_HOST=localhost
DB_PORT=5432

REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0


Run migrations:


python manage.py migrate

Start the server:

python manage.py runserver

The API will run at:

http://127.0.0.1:8000/


## API

### Calculate Route

POST /api/route/calculate/

Example request:


{
    "start_address": "New York, NY, USA",
    "destination_address": "Chicago, IL, USA",
    "fuel_efficiency_mpg": 10,
    "tank_capacity_gallons": 100,
    "starting_fuel_gallons": 20
}

`fuel_efficiency_mpg`, `tank_capacity_gallons` and `starting_fuel_gallons` are optional.

Default fuel efficiency is 10 MPG.

## Fuel Stop Calculation

The vehicle can travel a maximum of 500 miles on a full tank.

For a longer route, the application finds fuel stations along the route and calculates where fuel is needed.

At each stage, reachable stations are considered and the fuel price is used to select a cost-effective option.

The application can return multiple fuel stops when required.

## Database

Fuel station information from the provided CSV is stored in PostgreSQL.

There are two main models:

### Location

Stores:

* latitude
* longitude
* formatted location
* location key

### FuelStation

Stores:

* truck stop ID
* station name
* address
* city
* state
* fuel price
* related location

The location is kept separately so that the same location data can be reused.

## Redis

Redis is used to cache data that would otherwise require another Geoapify request.

The application caches:

* Geocoding results
* Route results
* Driving distance results
* API responses

This helps reduce repeated calls to the external API and improves response time for repeated requests.

## Fuel Station Locations

Fuel station coordinates can be populated using:

python manage.py populate_station_locations

For testing a smaller number of stations:

python manage.py populate_station_locations --limit 100

A small delay can also be added between requests:

python manage.py populate_station_locations --delay 0.5


## Geoapify

Geoapify is used for:

* Geocoding
* Route calculation
* Route geometry
* Driving distance

The API key is provided through the `.env` file.

## Notes

The fuel-stop selection currently uses a greedy approach. It chooses a cost-effective reachable station at each stage instead of solving a global optimization problem.

The project is built mainly for the given assessment requirements.

## Repository

https://github.com/shrasti16/FuelRouteOptimiser
