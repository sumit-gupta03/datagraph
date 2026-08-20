"""Demo service: an API endpoint reading the warehouse fact table."""


def fetch_bookings(customer_key: str):
    """Pretend to query prod.analytics.fact_booking."""
    return [{"customer_key": customer_key, "amount": 120.0}]


def bookings_endpoint(customer_key: str):
    """GET /bookings — returns bookings for a customer."""
    return {"data": fetch_bookings(customer_key)}
