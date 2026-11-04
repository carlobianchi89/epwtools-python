"""
epw.tools CLI
================
Command-line interface that wraps the epw.tools REST API.

Usage examples
--------------
    # Generate a single AMY EPW for Denver 2022
    epwtools generate --lat 39.74 --lon -104.98 --year 2022 --name Denver

    # Generate a TMY for New York
    epwtools tmy --lat 40.71 --lon -74.01 --start 2005 --end 2024 --name NewYork

    # Export an EPW to multiple formats
    epwtools export myfile.epw --format xlsx
    epwtools export myfile.epw --format bundle

    # Ground temperature profile
    epwtools ground-temps myfile.epw

    # Stormwater IDF curves
    epwtools idf myfile.epw

    # Parametric insurance triggers
    epwtools insurance myfile.epw --heat 38 --freeze -10 --wind 20

    # Status of a running job
    epwtools status <job_id>

Authentication
--------------
Generation and report commands require a free epw.tools API key (register
at https://epw.tools — 2 free credits, 4 with an academic email, no card needed). Provide it, in
order of precedence:

    epwtools --api-key epw_xxx generate ...   # explicit flag (any command)
    export EPWTOOLS_API_KEY=epw_xxx           # environment variable
    epwtools configure --api-key epw_xxx      # saved to ~/.epwtools/config.json

The API base URL defaults to https://epw.tools/api (production). Point at a
local dev backend with the WEATHERPROOF_API_URL environment variable or the
per-command --api-url flag, e.g. --api-url http://localhost:8000/api.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

app = typer.Typer(
    name="epwtools",
    help="Generate and export EnergyPlus weather files from the command line.",
    add_completion=False,
)
console = Console()

# Roadmap R2-2 — this key is populated once per invocation by the app
# callback below (global --api-key / EPWTOOLS_API_KEY / saved config), then
# read by every command via _headers(). Reset unconditionally on each
# callback run (not just when truthy) so CliRunner-based tests never leak
# state between invocations in the same process.
_API_KEY: Optional[str] = None

_CONFIG_PATH = Path.home() / ".epwtools" / "config.json"


def _load_config_key() -> Optional[str]:
    """Read a previously-saved API key from ~/.epwtools/config.json.
    Silently returns None on any error — a missing/corrupt config file must
    never crash the CLI; the user just falls through to no auth (and gets
    the backend's own 403 registration_required message)."""
    try:
        data = json.loads(_CONFIG_PATH.read_text())
        key = data.get("api_key")
        return key if isinstance(key, str) and key else None
    except Exception:
        return None


def _cli_version() -> str:
    try:
        from epwtools import __version__
        return __version__
    except Exception:  # noqa: BLE001
        return "0"


def _headers(explicit_key: Optional[str] = None) -> dict:
    """Auth header for API calls — every request-making command must pass
    this through ``headers=``. Falls back to the key resolved by the global
    --api-key / EPWTOOLS_API_KEY / config-file chain."""
    key = explicit_key or _API_KEY
    h = {"User-Agent": f"weatherproof-cli/{_cli_version()}"}
    if key:
        h["X-API-Key"] = key
    return h


@app.callback()
def main(
    api_key: Optional[str] = typer.Option(
        None, "--api-key", envvar="EPWTOOLS_API_KEY",
        help="Your epw.tools API key. Falls back to EPWTOOLS_API_KEY, then "
             "~/.epwtools/config.json (see 'epwtools configure').",
    ),
):
    """Generate and export EnergyPlus weather files from the command line."""
    global _API_KEY
    _API_KEY = api_key or _load_config_key()


@app.command()
def configure(
    api_key: str = typer.Option(
        ..., "--api-key", prompt="epw.tools API key", hide_input=True,
        help="Your epw.tools API key — register free at https://epw.tools",
    ),
):
    """Save your API key so you don't have to pass --api-key every time."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps({"api_key": api_key}, indent=2))
    try:
        os.chmod(_CONFIG_PATH, 0o600)  # owner-only — this is a bearer credential
    except OSError:
        pass
    console.print(f"[green]✓[/green] Saved API key to {_CONFIG_PATH}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api(url_override: Optional[str] = None) -> str:
    base = url_override or os.environ.get("WEATHERPROOF_API_URL", "https://epw.tools/api")
    return base.rstrip("/")


def _poll(job_id: str, base_url: str, quiet: bool = False) -> dict:
    """Block until job is complete or failed; stream logs to console."""
    seen = 0
    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  transient=True, console=console) as prog:
        task = prog.add_task(f"[cyan]Job {job_id[:8]}…", total=None)
        while True:
            resp = requests.get(f"{base_url}/jobs/{job_id}", headers=_headers(), timeout=10)
            resp.raise_for_status()
            data = resp.json()
            logs = data.get("logs", [])
            for log_line in logs[seen:]:
                if not quiet:
                    prog.print(f"  [dim]{log_line}[/dim]")
                seen = len(logs)
            if data["status"] in ("complete", "failed", "cancelled"):
                return data
            time.sleep(1)
            prog.advance(task)


def _handle_result(data: dict, job_id: str, base_url: str, out_dir: Path):
    """Print result summary and download EPW + companion files."""
    result = data.get("result", {})
    if data["status"] == "failed":
        console.print(f"[red bold]✗ Job failed:[/red bold] {result.get('error', 'unknown error')}")
        raise typer.Exit(1)
    if data["status"] == "cancelled":
        console.print(f"[yellow bold]■ Job cancelled[/yellow bold]  job_id={job_id}")
        raise typer.Exit(1)

    console.print(f"[green bold]✓ Complete[/green bold]  job_id={job_id}")

    epw_file = result.get("epw_file")
    if not epw_file:
        console.print("[yellow]No EPW file in result.[/yellow]")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    for key in ("epw_file", "ddy_file", "stat_file"):
        fname = result.get(key)
        if not fname:
            continue
        dl_url = f"{base_url}/jobs/{job_id}/download/{fname}"
        r = requests.get(dl_url, headers=_headers(), timeout=60)
        r.raise_for_status()
        out_path = out_dir / fname
        out_path.write_bytes(r.content)
        console.print(f"  [green]↓[/green] {out_path}")

    # Summary table
    table = Table(show_header=False, box=None, padding=(0, 1))
    for key, val in [
        ("HDD", result.get("hdd")),
        ("CDD", result.get("cdd")),
        ("Climate Zone", result.get("climate_zone")),
        ("Station WMO", result.get("wmo")),
    ]:
        if val is not None:
            table.add_row(f"[dim]{key}[/dim]", str(val))
    console.print(table)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command()
def generate(
    lat:      float = typer.Option(..., help="Latitude  (-90 to 90)"),
    lon:      float = typer.Option(..., help="Longitude (-180 to 180)"),
    year:     int   = typer.Option(..., help="Year (1990–2030)"),
    name:     str   = typer.Option("Location", "--name", "-n", help="Location name"),
    source:   str   = typer.Option("merra2", help="Data source: merra2 | open-meteo (ERA5) | hrrr (US) | satellite | cweeds (Canada)"),
    edition:  str   = typer.Option("2025", "--edition", "-e", help="ASHRAE edition"),
    out_dir:  Path  = typer.Option(Path("."), "--out", "-o", help="Output directory"),
    leap_day: str   = typer.Option("remove", "--leap-day", help="Leap years: remove 29 Feb (8760 h, default) or keep it (8784 h, true weekdays in EnergyPlus)"),
    api_url:  Optional[str] = typer.Option(None, "--api-url"),
    min_quality: Optional[float] = typer.Option(
        None,
        "--min-quality",
        help=(
            "E8: minimum acceptable QC score in [0,1]; if the generated EPW "
            "scores below this threshold the CLI exits non-zero so callers "
            "can fail fast in CI pipelines."
        ),
    ),
):
    """Generate a single-year AMY EPW file."""
    base = _api(api_url)
    payload = {"lat": lat, "lon": lon, "year": year, "location_name": name,
               "file_type": "AMY", "data_source": {"era5": "open-meteo"}.get(source, source), "dc_edition": edition,
               "leap_day": leap_day}
    console.print(f"[bold]Generating EPW[/bold] {name} ({lat}, {lon}) {year}…")
    r = requests.post(f"{base}/generate/individual", json=payload, headers=_headers(), timeout=15)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    data = _poll(job_id, base)
    _handle_result(data, job_id, base, out_dir)
    if min_quality is not None:
        # Quality score may live under data["quality_score"] or
        # data["result"]["quality_score"] depending on backend version.
        q = data.get("quality_score")
        if q is None:
            q = (data.get("result") or {}).get("quality_score")
        try:
            q_float = float(q) if q is not None else None
        except (TypeError, ValueError):
            q_float = None
        if q_float is None:
            console.print(
                "[yellow]--min-quality requested but the backend did not "
                "return a quality_score; skipping the gate.[/yellow]"
            )
        elif q_float < float(min_quality):
            console.print(
                f"[red]Quality {q_float:.3f} below --min-quality "
                f"{float(min_quality):.3f}[/red]"
            )
            raise typer.Exit(code=2)


@app.command()
def forecast(
    lat:      float = typer.Option(..., help="Latitude  (-90 to 90)"),
    lon:      float = typer.Option(..., help="Longitude (-180 to 180)"),
    name:     str   = typer.Option("Location", "--name", "-n", help="Location name"),
    days:     int   = typer.Option(7, "--days", "-d", help="Forecast horizon in days (1–15)"),
    edition:  str   = typer.Option("2025", "--edition", "-e", help="ASHRAE edition"),
    out_dir:  Path  = typer.Option(Path("."), "--out", "-o", help="Output directory"),
    api_url:  Optional[str] = typer.Option(None, "--api-url"),
):
    """Generate a short-horizon forecast EPW (next 1–15 days) for operational sims.

    Produces a PARTIAL EPW covering only the forecast window — set your
    EnergyPlus RunPeriod to the dates in the file's DATA PERIODS header.
    """
    base = _api(api_url)
    payload = {"lat": lat, "lon": lon, "location_name": name,
               "forecast_days": days, "dc_edition": edition}
    console.print(f"[bold]Generating forecast EPW[/bold] {name} ({lat}, {lon}) — next {days} days…")
    r = requests.post(f"{base}/generate/forecast", json=payload, headers=_headers(), timeout=15)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    data = _poll(job_id, base)
    _handle_result(data, job_id, base, out_dir)


@app.command()
def tmy(
    lat:      float = typer.Option(..., help="Latitude"),
    lon:      float = typer.Option(..., help="Longitude"),
    start:    int   = typer.Option(..., help="Start year (2000–2024)"),
    end:      int   = typer.Option(..., help="End year (2000–2024)"),
    name:     str   = typer.Option("Location", "--name", "-n"),
    edition:  str   = typer.Option("2025", "--edition", "-e"),
    use_case: str   = typer.Option(
        "default", "--use-case", "-u",
        help="Weight preset: default | pv | daylighting | ventilation | hvac",
    ),
    tmy_method: str = typer.Option(
        "nrel", "--method", "-m",
        help="Selection method: nrel (NREL TMY3 style, default) | "
             "onebuilding (OneBuilding TMYx style, ISO 15927-4)",
    ),
    source:   str = typer.Option(
        "merra2", "--source", "-s",
        help="Data source: merra2 (reanalysis, default) | "
             "hybrid (ISD station met + reanalysis solar) | meteostat",
    ),
    out_dir:  Path  = typer.Option(Path("."), "--out", "-o"),
    api_url:  Optional[str] = typer.Option(None, "--api-url"),
):
    """Generate a Typical Meteorological Year (TMY) EPW file."""
    base = _api(api_url)
    payload = {"lat": lat, "lon": lon, "start_year": start, "end_year": end,
               "location_name": name, "dc_edition": edition, "weights": {},
               "use_case": use_case, "tmy_method": tmy_method,
               "data_source": source}
    console.print(f"[bold]Generating TMY[/bold] {name} ({lat}, {lon}) {start}–{end} "
                  f"[use_case={use_case}, method={tmy_method}]…")
    r = requests.post(f"{base}/generate/tmy", json=payload, headers=_headers(), timeout=15)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    data = _poll(job_id, base)
    _handle_result(data, job_id, base, out_dir)


@app.command()
def xmy(
    lat:      float = typer.Option(..., help="Latitude"),
    lon:      float = typer.Option(..., help="Longitude"),
    start:    int   = typer.Option(..., help="Start year (2000–2024)"),
    end:      int   = typer.Option(..., help="End year (2000–2024)"),
    mode:     str   = typer.Option("hot", help="Extreme mode: hot|cold|solar|solar_low|heatwave|wind"),
    name:     str   = typer.Option("Location", "--name", "-n"),
    edition:  str   = typer.Option("2025", "--edition", "-e"),
    out_dir:  Path  = typer.Option(Path("."), "--out", "-o"),
    api_url:  Optional[str] = typer.Option(None, "--api-url"),
):
    """Generate an Extreme Meteorological Year (XMY) EPW file.

    Assembles a year from the most extreme month for each calendar position
    across the historical record. Modes:
      hot   — maximises dry-bulb temperature (peak cooling load)
      cold  — minimises dry-bulb temperature (peak heating load)
      solar — maximises GHI (solar worst-case)
      solar_low — minimises GHI (conservative PV)
      heatwave  — the whole real year holding the worst heatwave (Annex 80)
      wind  — maximises wind speed
    """
    valid_modes = ("hot", "cold", "solar", "solar_low", "solar_p90", "heatwave", "moisture", "wind")
    if mode not in valid_modes:
        console.print(f"[red]Invalid mode '{mode}'. Choose from: {', '.join(valid_modes)}[/red]")
        raise typer.Exit(1)
    base = _api(api_url)
    payload = {"lat": lat, "lon": lon, "start_year": start, "end_year": end,
               "mode": mode, "location_name": name, "dc_edition": edition}
    console.print(f"[bold]Generating XMY ({mode})[/bold] {name} ({lat}, {lon}) {start}–{end}…")
    r = requests.post(f"{base}/generate/xmy", json=payload, headers=_headers(), timeout=15)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    data = _poll(job_id, base)
    _handle_result(data, job_id, base, out_dir)


@app.command()
def future(
    lat:      float = typer.Option(...),
    lon:      float = typer.Option(...),
    name:     str   = typer.Option("Location", "--name", "-n"),
    scenario: str   = typer.Option("ssp245", help="SSP scenario: ssp126|ssp245|ssp370|ssp585"),
    horizon:  int   = typer.Option(2050,  help="Target year: any decade 2030–2100"),
    model:    str   = typer.Option("MRI-ESM2-0"),
    base_year:int   = typer.Option(2010),
    out_dir:  Path  = typer.Option(Path("."), "--out", "-o"),
    api_url:  Optional[str] = typer.Option(None, "--api-url"),
):
    """Generate a future climate EPW (CMIP6 morphed)."""
    base = _api(api_url)
    payload = {"lat": lat, "lon": lon, "location_name": name, "base_year": base_year,
               "scenario": scenario, "horizon": horizon, "model": model}
    console.print(f"[bold]Future climate EPW[/bold] {name} {scenario}/{horizon}…")
    r = requests.post(f"{base}/generate/future-climate", json=payload, headers=_headers(), timeout=15)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    data = _poll(job_id, base)
    _handle_result(data, job_id, base, out_dir)


@app.command()
def export(
    epw_file: Path = typer.Argument(..., help="EPW file to convert"),
    fmt:      str  = typer.Option("xlsx", "--format", "-f",
                                  help="wea|clm|pvsyst|json|xlsx|parquet|netcdf|"
                                       "modelica|geojson|try|iwec2|wyec2|bundle"),
    out_dir:  Path = typer.Option(Path("."), "--out", "-o"),
    api_url:  Optional[str] = typer.Option(None, "--api-url"),
):
    """Convert an EPW file to another format."""
    if not epw_file.exists():
        console.print(f"[red]File not found: {epw_file}[/red]")
        raise typer.Exit(1)
    base = _api(api_url)
    console.print(f"[bold]Exporting[/bold] {epw_file.name} → {fmt}…")
    with open(epw_file, "rb") as fh:
        r = requests.post(
            f"{base}/tools/export-format/{fmt}",
            files={"file": (epw_file.name, fh, "application/octet-stream")},
            headers=_headers(),
            timeout=120,
        )
    r.raise_for_status()
    cd = r.headers.get("Content-Disposition", "")
    ext_map = {"wea":".wea","clm":".clm","pvsyst":".met","json":".json","xlsx":".xlsx",
               "parquet":".parquet","netcdf":".nc","modelica":".mos","geojson":".geojson",
               "try":"_TRY.dat","iwec2":".iwec2","wyec2":".wyec2","bundle":"_bundle.zip"}
    ext = ext_map.get(fmt, f".{fmt}")
    stem = epw_file.stem
    out_path = out_dir / f"{stem}{ext}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(r.content)
    console.print(f"  [green]↓[/green] {out_path}")


@app.command(name="ground-temps")
def ground_temps(
    epw_file: Path = typer.Argument(..., help="EPW file"),
    api_url:  Optional[str] = typer.Option(None, "--api-url"),
):
    """Compute Kusuda-Achenbach ground temperature profiles."""
    if not epw_file.exists():
        console.print(f"[red]File not found: {epw_file}[/red]")
        raise typer.Exit(1)
    base = _api(api_url)
    with open(epw_file, "rb") as fh:
        r = requests.post(
            f"{base}/tools/ground-temps",
            files={"reference_epw": (epw_file.name, fh)},
            headers=_headers(),
            timeout=60,
        )
    r.raise_for_status()
    data = r.json()

    console.print(f"\n[bold]Ground Temperatures[/bold] — {data['location']['name']}")
    console.print(f"Annual avg air temp: [cyan]{data['annual_avg_air_temp_c']} °C[/cyan]\n")

    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    table = Table(*["Depth"] + months, show_header=True)
    for d in data["depths"]:
        row = [f"{d['depth_m']} m"] + [str(t) for t in d["monthly"]]
        table.add_row(*row)
    console.print(table)


@app.command()
def idf(
    epw_file: Path = typer.Argument(..., help="EPW file"),
    api_url:  Optional[str] = typer.Option(None, "--api-url"),
):
    """Compute Intensity-Duration-Frequency (IDF) stormwater curves."""
    if not epw_file.exists():
        console.print(f"[red]File not found: {epw_file}[/red]")
        raise typer.Exit(1)
    base = _api(api_url)
    with open(epw_file, "rb") as fh:
        r = requests.post(
            f"{base}/reports/stormwater",
            files={"reference_epw": (epw_file.name, fh)},
            headers=_headers(),
            timeout=60,
        )
    r.raise_for_status()
    data = r.json()

    console.print(f"\n[bold]IDF Curves[/bold] — {data['location']['name']} ({data['years_of_data']} yrs)")
    if data.get("warning"):
        console.print(f"[yellow]⚠ {data['warning']}[/yellow]")

    rps = [f"{rp}yr" for rp in data["return_periods_yr"]]
    table = Table(*["Duration"] + rps, title="Intensity (mm/h)")
    for dur in data["durations_h"]:
        row = [f"{dur}h"] + [str(data["idf_mm_per_h"].get(f"{dur}h", {}).get(rp, "—")) for rp in rps]
        table.add_row(*row)
    console.print(table)


@app.command()
def insurance(
    epw_file:  Path  = typer.Argument(..., help="EPW file"),
    heat:      float = typer.Option(35.0, help="Heat threshold °C"),
    freeze:    float = typer.Option(-5.0, help="Freeze threshold °C"),
    wind:      float = typer.Option(15.0, help="Wind threshold m/s"),
    precip:    float = typer.Option(50.0, help="24h precipitation threshold mm"),
    api_url:   Optional[str] = typer.Option(None, "--api-url"),
):
    """Compute parametric insurance trigger statistics."""
    if not epw_file.exists():
        console.print(f"[red]File not found: {epw_file}[/red]")
        raise typer.Exit(1)
    base = _api(api_url)
    with open(epw_file, "rb") as fh:
        r = requests.post(
            f"{base}/reports/insurance",
            data={"heat_threshold_c": heat, "freeze_threshold_c": freeze,
                  "wind_threshold_ms": wind, "precip_24h_threshold_mm": precip},
            files={"reference_epw": (epw_file.name, fh)},
            headers=_headers(),
            timeout=60,
        )
    r.raise_for_status()
    data = r.json()

    console.print(f"\n[bold]Insurance Triggers[/bold] — {data['location']['name']} ({data['years_of_data']} yrs)")

    table = Table("Trigger", "Label", "P50 hrs", "P90 hrs", "Mean hrs", "Max hrs")
    for key in ("heat", "freeze", "wind", "heavy_rain"):
        t = data.get(key, {})
        ann = t.get("annual", {})
        table.add_row(key, t.get("label",""), str(ann.get("p50","")), str(ann.get("p90","")),
                      str(ann.get("mean","")), str(ann.get("max","")))
    console.print(table)
    console.print(f"HDD: [cyan]{data['hdd']['annual_total']}[/cyan] °C·d   "
                  f"CDD: [red]{data['cdd']['annual_total']}[/red] °C·d")


@app.command()
def status(
    job_id:  str  = typer.Argument(..., help="Job ID to query"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
):
    """Check the status of a submitted job."""
    base = _api(api_url)
    r = requests.get(f"{base}/jobs/{job_id}", headers=_headers(), timeout=10)
    r.raise_for_status()
    data = r.json()
    console.print_json(json.dumps(data, indent=2))


@app.command()
def batch(
    csv_file:  Path  = typer.Argument(..., help="CSV with columns: latitude,longitude,location_name,year"),
    out_dir:   Path  = typer.Option(Path("."), "--out", "-o"),
    api_url:   Optional[str] = typer.Option(None, "--api-url"),
):
    """Submit a CSV batch of EPW generation requests."""
    if not csv_file.exists():
        console.print(f"[red]File not found: {csv_file}[/red]")
        raise typer.Exit(1)
    base = _api(api_url)
    with open(csv_file, "rb") as fh:
        r = requests.post(
            f"{base}/generate/csv-batch",
            files={"file": (csv_file.name, fh, "text/csv")},
            headers=_headers(),
            timeout=30,
        )
    r.raise_for_status()
    job_id = r.json()["job_id"]
    console.print(f"[bold]Batch submitted[/bold] job_id={job_id} — {r.json()['row_count']} rows")
    data = _poll(job_id, base)
    result = data.get("result", {})
    console.print(f"Done: {result.get('ok')}/{result.get('total')} succeeded")


@app.command(name="compliance-package")
def compliance_package(
    job_id:  str  = typer.Argument(..., help="Job UUID returned by generate / tmy / future"),
    out_dir: Path = typer.Option(Path("."), "--out", "-o", help="Output directory"),
    api_key: Optional[str] = typer.Option(None, "--api-key", envvar="EPWTOOLS_API_KEY"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
):
    """Download the full compliance package for a completed job.

    The ZIP contains the EPW, DDY, ASHRAE compliance JSON, branded PDF report,
    and a README with provenance metadata.

    Examples:

        epwtools compliance-package 3e1a0b2c-... --out ./output
    """
    base = _api(api_url)
    headers = _headers(api_key)  # per-command flag wins, then the global chain
    console.print(f"[bold]Downloading compliance package[/bold] {job_id[:8]}…")
    r = requests.get(
        f"{base}/jobs/{job_id}/compliance-package",
        headers=headers,
        timeout=120,
    )
    r.raise_for_status()
    out_dir.mkdir(parents=True, exist_ok=True)
    cd = r.headers.get("Content-Disposition", "")
    fname = f"{job_id[:8]}_compliance_package.zip"
    if "filename=" in cd:
        import re as _re
        m = _re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd)
        if m:
            fname = m.group(1).strip()
    path = out_dir / fname
    path.write_bytes(r.content)
    console.print(f"  [green]↓[/green] {path}  ({len(r.content)//1024} KB)")



@app.command("mcp")
def mcp_server():
    """Run the EPW.tools MCP server over stdio, for AI assistants.

    Needs the extra: pip install 'epwtools[mcp]'. Same as epw-tools-mcp; this
    form exists because MCP registries launch `<package> <args>`."""
    from .mcp_server import main as _mcp_main
    _mcp_main()

if __name__ == "__main__":
    app()
