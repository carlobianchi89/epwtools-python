# epwtools

[![carlobianchi89/epwtools-python MCP server](https://glama.ai/mcp/servers/carlobianchi89/epwtools-python/badges/score.svg)](https://glama.ai/mcp/servers/carlobianchi89/epwtools-python)

Python client, command-line tool and MCP server for **[EPW.tools](https://epw.tools)**: EnergyPlus weather files
(EPW, with DDY design days and STAT summaries) for any location on Earth.

- **Actual years** from 1990 to now, for any coordinates, with or without a nearby weather station
- **Typical years** (TMY), extreme and heatwave years
- **Future climate** for 2030 to 2100 under SSP scenarios
- **Design conditions** (ASHRAE method) and short-term **forecast** weather files

The weather data are built and validated by EPW.tools ([methodology](https://epw.tools/methodology),
[validation](https://epw.tools/validation)); this package is the open-source way to call them from Python, the
terminal or an AI assistant.

## Install

```bash
pip install epwtools            # client + CLI
pip install 'epwtools[mcp]'     # + MCP server for AI assistants
```

Get a free API key at [epw.tools](https://epw.tools) (API tab); the first files are free.

```bash
export EPWTOOLS_API_KEY=your-key
```

## Python

```python
from epwtools import EPWClient

client = EPWClient()                       # reads EPWTOOLS_API_KEY

# actual year 2023 for a site in Denver
epw = client.generate(lat=39.74, lon=-104.99, year=2023, name="Denver", out_dir="weather")

# typical year built from 2005-2024
tmy = client.tmy(lat=45.46, lon=9.19, start=2005, end=2024, name="Milan", out_dir="weather")

# design conditions (ASHRAE method)
dc = client.design_conditions(lat=51.51, lon=-0.13)
```

## Command line

```bash
epwtools configure                          # store your key
epwtools generate --lat 39.74 --lon -104.99 --year 2023 --name Denver
epwtools tmy --lat 45.46 --lon 9.19 --start 2005 --end 2024 --name Milan
epwtools future --lat 40.71 --lon -74.01 --scenario ssp245 --horizon 2050 --name NYC
epwtools --help
```

## AI assistants (MCP)

Let Claude, ChatGPT and other MCP-capable assistants make weather files for you:

```bash
uvx --from 'epwtools[mcp]' epw-tools-mcp
```

or use the hosted server, listed in the official MCP Registry. Tools include `generate_epw`, `generate_tmy`,
`generate_future_climate`, `generate_forecast`, `get_design_conditions` and `get_nearest_stations`.
Setup guide: [epw.tools/ai-weather-files](https://epw.tools/ai-weather-files).

## Also on EPW.tools (web)

[EPW to CSV / Excel converter](https://epw.tools/epw-to-csv) ·
[Manual J design temperatures by address](https://epw.tools/manual-j-design-temperatures) ·
[Construction weather days](https://epw.tools/construction-weather-days) ·
[Data-centre site climate](https://epw.tools/data-center-weather-data) ·
[API licence for software vendors](https://epw.tools/api-licensing)

## Licence

The client, CLI and MCP server in this repository are MIT-licensed. Use of the EPW.tools service and its data is
governed by the EPW.tools terms of service (linked in the footer of [epw.tools](https://epw.tools)).
