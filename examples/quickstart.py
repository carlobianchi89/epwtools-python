"""Download an actual-year and a typical-year EPW for one site. Run: EPWTOOLS_API_KEY=... python examples/quickstart.py"""
from epwtools import EPWClient

client = EPWClient()
print(client.generate(lat=39.74, lon=-104.99, year=2023, name="Denver", out_dir="weather"))
print(client.tmy(lat=39.74, lon=-104.99, start=2005, end=2024, name="Denver", out_dir="weather"))
