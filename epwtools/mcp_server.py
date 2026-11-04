"""
EPW.tools MCP Server
Exposes EPW generation tools to Claude Desktop, Cursor, and other MCP clients.

Usage:
  EPWTOOLS_API_KEY=epw_xxx python -m backend.mcp_server

Environment:
  EPWTOOLS_API_KEY   Your epw.tools API key (required for generation tools —
                     the backend charges credits per file; without a key every
                     generate_* call is rejected with 403).
  EPWTOOLS_API_BASE  Backend base URL (default: https://epw.tools/api;
                     point it elsewhere only for a self-hosted backend).

Auth model (review #76):
  The FastMCP runtime here speaks **stdio only** — `mcp.run()` reads/writes
  JSON-RPC over the launched process's stdin/stdout, not a network socket.
  There is no listener for an attacker to reach. Authorisation is therefore
  inherited from the parent process: anyone who can spawn this script with
  the configured `EPWTOOLS_API_KEY` can invoke any tool and burn credits
  against that key.

  Operational guidance:
    • Treat `EPWTOOLS_API_KEY` like any other production secret — never
      commit it, and never ship a config that hard-codes one.
    • The Claude Desktop / Cursor / Continue config files that point at
      this entry-point are the trust boundary: protect them with normal
      file-system permissions (chmod 600).
    • Do **not** expose this module behind an HTTP/SSE transport without
      first adding a request-level authenticator (e.g. a bearer token
      compared via `hmac.compare_digest`); the backend already rate-limits
      and key-scopes generation, but the MCP layer would still need its
      own auth gate before it could be safely network-reachable.

Requires:
  pip install mcp   (or: pip install "mcp[cli]")
"""

import json
import os
import requests
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    raise ImportError(
        "The 'mcp' package is required to run the EPW.tools MCP server.\n"
        "Easiest (no install): uvx --from 'epwtools[mcp]' epw-tools-mcp\n"
        "Or: pip install 'epwtools[mcp]'  then  epw-tools-mcp"
    )

# Base URL of the epw.tools backend. Defaults to the public production API so a
# pip/uvx install works out of the box; override for a local/dev backend.
_BASE = os.environ.get("EPWTOOLS_API_BASE", "https://epw.tools/api").rstrip("/")

# API key — generation endpoints require one and charge credits per file.
_API_KEY = os.environ.get("EPWTOOLS_API_KEY", "")


def _headers() -> dict:
    try:
        from epwtools import __version__ as _v
    except Exception:  # noqa: BLE001
        _v = "0"
    h = {"User-Agent": f"epwtools-mcp/{_v}"}
    if _API_KEY:
        h["X-API-Key"] = _API_KEY
    return h


def _require_key() -> None:
    if not _API_KEY:
        raise RuntimeError(
            "EPWTOOLS_API_KEY is not set. Generation tools require an API key "
            "(each file costs credits). Get one at https://epw.tools and set it "
            "in your MCP client config, e.g. \"env\": {\"EPWTOOLS_API_KEY\": \"epw_xxx\"}."
        )

mcp = FastMCP(
    name="EPW.tools",
    instructions=(
        "EPW.tools generates EnergyPlus Weather (EPW) files and companion DDY/STAT "
        "files for any location worldwide. Tools cover: individual AMY, multi-year, "
        "TMY (Sandia/FS method), short-horizon forecast (next 1–15 days, for MPC/DR/"
        "operational sims), future climate (CMIP6 morphing), UHI adjustment, "
        "wildfire smoke overlay, and extreme event injection, plus design conditions in two separate "
        "modes: the ASHRAE Handbook method (replicated, validated) and EPW.tools Enhanced (site-specific, "
        "current climate, with 90 % ranges).\n\n"
        "Working with OpenStudio-MCP (NLR's openstudio-mcp server) or any local EnergyPlus workflow: pass "
        "save_to=<a folder that server can read, e.g. its /inputs mount> to a generate_* tool. The EPW, DDY and "
        "STAT are saved there under one base name, which is what OpenStudio-MCP's change_building_location "
        "expects; then call change_building_location with the saved .epw path. To size with EPW.tools Enhanced "
        "design days instead, call get_design_conditions(mode='enhanced', save_to=<same folder>, "
        "ddy_name=<the EPW base name>) before change_building_location; it replaces that DDY."
    ),
)


# ── Helper ────────────────────────────────────────────────────────────────────

def _post(path: str, payload: dict) -> dict:
    _require_key()
    r = requests.post(f"{_BASE}{path}", json=payload, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def _poll(job_id: str, max_wait_s: int = 600) -> dict:
    """Block until the job reaches complete/failed, then return the result dict."""
    import time
    for _ in range(max_wait_s):
        # Job status is owner-gated — the key must travel with the poll too
        r = requests.get(f"{_BASE}/jobs/{job_id}", headers=_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        if data["status"] in ("complete", "failed"):
            return data
        time.sleep(1)
    return {"status": "timeout", "job_id": job_id}


def _download_url(job_id: str, filename: Optional[str]) -> Optional[str]:
    """Build a download URL the MCP client can fetch directly. Downloads are
    owner-gated; a plain URL carries no headers, so the key rides as a query
    parameter (it is the caller's own key, returned only to the caller)."""
    if not filename:
        return None
    url = f"{_BASE}/jobs/{job_id}/download/{filename}"
    return f"{url}?key={_API_KEY}" if _API_KEY else url


def _save_job_files(job_id: str, epw_name: Optional[str], save_to: Optional[str]) -> Optional[dict]:
    """Download the job's EPW and its same-named DDY and STAT into save_to. Returns {ext: local path}.

    OpenStudio-MCP's change_building_location (and the OpenStudio ChangeBuildingLocation measure) need the
    .epw, .stat and .ddy side by side with one base name; job outputs already share it."""
    if not save_to or not epw_name:
        return None
    import pathlib
    folder = pathlib.Path(os.path.expanduser(save_to)).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stem = os.path.basename(epw_name).rsplit(".", 1)[0]
    r = requests.get(f"{_BASE}/jobs/{job_id}/files", headers=_headers(), timeout=30)
    r.raise_for_status()
    available = set(r.json().get("files", []))
    saved = {}
    for ext in ("epw", "ddy", "stat"):
        name = f"{stem}.{ext}"
        if name not in available:
            continue
        d = requests.get(f"{_BASE}/jobs/{job_id}/download/{name}", headers=_headers(), timeout=120)
        d.raise_for_status()
        (folder / name).write_bytes(d.content)
        saved[ext] = str(folder / name)
    return saved


def _result_with_files(job_id: str, result: dict, extra: dict, save_to: Optional[str]) -> str:
    epw = result.get("result", {}).get("epw_file")
    out = {"job_id": job_id, "status": result["status"], "epw_file": epw,
           "download_url": _download_url(job_id, epw), **extra, "logs": result.get("logs", [])[-5:]}
    if save_to and result.get("status") == "complete":
        try:
            out["saved_files"] = _save_job_files(job_id, epw, save_to)
            if out["saved_files"] and {"epw", "ddy", "stat"} <= set(out["saved_files"]):
                out["next_step"] = ("Ready for OpenStudio-MCP: change_building_location(weather_file="
                                    f"\"{out['saved_files']['epw']}\")")
        except Exception as e:  # noqa: BLE001 — the job itself succeeded; report the save problem
            out["save_error"] = str(e)
    return json.dumps(out, indent=2)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def generate_epw(
    lat: float,
    lon: float,
    year: int,
    location_name: str = "MyLocation",
    data_source: str = "merra2",
    dc_edition: str = "2025",
    save_to: Optional[str] = None,
    leap_day: str = "remove",
) -> str:
    """
    Generate a single-year AMY EPW file (+ DDY + STAT) for any location worldwide.

    Args:
        lat: Latitude in decimal degrees (-90 to 90).
        lon: Longitude in decimal degrees (-180 to 180).
        year: Calendar year, 1990 to last year (complete years only).
        location_name: Name used in output filenames and EPW header.
        data_source: "merra2" (Standard, global, default), "open-meteo" (ERA5 via Open-Meteo, global), "hrrr" (3 km, US only), "satellite" (satellite solar: Europe, Africa, S. America, Asia, Australia) or "cweeds" (Canada station solar).
        dc_edition: ASHRAE design conditions edition: "2025", "2021", "2017", "2013", "2009", or "2005".
        save_to: Optional local folder; the EPW, DDY and STAT are saved there under one base name
                 (ready for OpenStudio-MCP's change_building_location).
        leap_day: Leap years only. "remove" (default) drops 29 February (8760 hours; EnergyPlus weekdays from
                  1 March then fall one day early). "keep" writes the true calendar (8784 hours).

    Returns:
        JSON string with job_id, status, and download links for EPW/DDY/STAT files.
    """
    resp = _post("/generate/individual", {
        "lat": lat, "lon": lon, "year": year,
        "location_name": location_name,
        "data_source": {"era5": "open-meteo"}.get(data_source, data_source),
        "dc_edition": dc_edition,
        "leap_day": leap_day,
    })
    job_id = resp["job_id"]
    result = _poll(job_id)
    r = result.get("result", {})
    return _result_with_files(job_id, result, {"hdd": r.get("hdd"), "cdd": r.get("cdd"),
                                                "climate_zone": r.get("climate_zone")}, save_to)


@mcp.tool()
def generate_forecast(
    lat: float,
    lon: float,
    location_name: str = "MyLocation",
    forecast_days: int = 7,
    dc_edition: str = "2025",
    save_to: Optional[str] = None,
) -> str:
    """
    Generate a short-horizon forecast EPW: the next 1–15 days of weather as a
    simulation-ready (partial) EPW, for model-predictive control, demand-response
    and other operational building-energy simulations.

    Unlike the other tools this returns a partial-year EPW covering only the
    forecast window; set your EnergyPlus RunPeriod to the dates in the file's
    DATA PERIODS header. US locations use a high-resolution model automatically.

    Args:
        lat: Latitude in decimal degrees (-90 to 90).
        lon: Longitude in decimal degrees (-180 to 180).
        location_name: Name used in output filenames and EPW header.
        forecast_days: Horizon in days (1–15). Default 7.
        dc_edition: ASHRAE design conditions edition.
        save_to: Optional local folder to save the EPW (and DDY/STAT if produced).

    Returns:
        JSON string with job_id, status, and a download link for the forecast EPW.
    """
    resp = _post("/generate/forecast", {
        "lat": lat, "lon": lon,
        "location_name": location_name,
        "forecast_days": forecast_days,
        "dc_edition": dc_edition,
    })
    job_id = resp["job_id"]
    result = _poll(job_id)
    r = result.get("result", {})
    return _result_with_files(job_id, result, {"forecast_days": r.get("forecast_days"),
                                                "partial": r.get("partial")}, save_to)


@mcp.tool()
def generate_tmy(
    lat: float,
    lon: float,
    location_name: str = "MyLocation",
    start_year: int = 2004,
    end_year: int = 2023,
    dc_edition: str = "2025",
    weights: Optional[dict] = None,
    save_to: Optional[str] = None,
) -> str:
    """
    Generate a custom Typical Meteorological Year (TMY) EPW using the
    Sandia/Finkelstein-Schafer method over a multi-year MERRA-2 record.

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.
        location_name: Name used in output filenames.
        start_year: First year of the averaging period (2000–2024, min span 5 yr).
        end_year: Last year of the averaging period (max span 25 yr).
        dc_edition: ASHRAE design conditions edition.
        weights: Optional dict of variable weights, e.g.:
                 {"dbt": 4, "dwpt": 4, "ghi": 12, "wspd": 3}.
                 Defaults to Sandia TMY3 standard weights.
        save_to: Optional local folder; EPW, DDY and STAT saved there under one base name.

    Returns:
        JSON string with job_id, status, and download link for the TMY EPW.
    """
    resp = _post("/generate/tmy", {
        "lat": lat, "lon": lon,
        "location_name": location_name,
        "start_year": start_year,
        "end_year": end_year,
        "dc_edition": dc_edition,
        "weights": weights or {},
    })
    job_id = resp["job_id"]
    result = _poll(job_id, max_wait_s=900)
    return _result_with_files(job_id, result, {}, save_to)


@mcp.tool()
def generate_future_climate(
    lat: float,
    lon: float,
    location_name: str = "MyLocation",
    scenario: str = "ssp245",
    horizon: int = 2050,
    base_year: int = 2010,
    model: str = "MRI-ESM2-0",
    dc_edition: str = "2025",
    save_to: Optional[str] = None,
) -> str:
    """
    Generate a future climate EPW using CMIP6 Belcher morphing applied to a
    MERRA-2 baseline year.

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.
        location_name: Name for output files.
        scenario: SSP scenario — "ssp126", "ssp245", "ssp370", or "ssp585".
        horizon: Future time horizon — any decade from 2030 to 2100 (2030, 2040, ..., 2100).
        base_year: MERRA-2 baseline year (1985–2014).
        model: CMIP6 GCM name, e.g. "MRI-ESM2-0", "GFDL-ESM4", "IPSL-CM6A-LR".
        dc_edition: ASHRAE design conditions edition.
        save_to: Optional local folder; EPW, DDY and STAT saved there under one base name.

    Returns:
        JSON string with job_id, status, and download link.
    """
    resp = _post("/generate/future-climate", {
        "lat": lat, "lon": lon, "location_name": location_name,
        "scenario": scenario, "horizon": horizon,
        "base_year": base_year, "model": model, "dc_edition": dc_edition,
    })
    job_id = resp["job_id"]
    result = _poll(job_id, max_wait_s=1800)
    return _result_with_files(job_id, result, {}, save_to)


@mcp.tool()
def get_design_conditions(
    lat: float,
    lon: float,
    mode: str = "ashrae_method",
    edition: str = "2025",
    current_climate: bool = True,
    location_name: Optional[str] = None,
    save_to: Optional[str] = None,
    ddy_name: Optional[str] = None,
) -> str:
    """
    Heating and cooling design conditions for any location, in one of two modes that are never mixed.
    Values are free (no API key). The ASHRAE-method DDY is free; saving the Enhanced DDY costs 1 credit and
    needs EPWTOOLS_API_KEY.

    mode="ashrae_method": the ASHRAE Handbook (Ch. 14) method replicated by EPW.tools at the nearest ASHRAE
        station over the chosen edition's period (validated ~0.2 C from the published tables). Use where a
        specification or code asks for ASHRAE design conditions.
    mode="enhanced": EPW.tools Enhanced, NOT the ASHRAE method: values at the site itself (23-50 % closer
        than the nearest station in a leave-one-station-out test), moved to the coming decade's climate
        (current_climate=True), each with a tested 90 % range, plus design heat waves and cold spells. Use for
        sizing decisions and risk.

    Args:
        lat, lon: Site coordinates in decimal degrees.
        mode: "ashrae_method" or "enhanced".
        edition: ASHRAE edition for mode="ashrae_method": 2025, 2021, 2017, 2013, 2009, 2005.
        current_climate: Enhanced only; False keeps the 1999-2023 climate.
        location_name: Name written into the DDY.
        save_to: Optional local folder to save the DDY (Enhanced: 1 credit, needs EPWTOOLS_API_KEY).
        ddy_name: Base name for the saved DDY. Use the base name of an EPW saved by a generate_* tool to
                  replace its DDY, so OpenStudio-MCP's change_building_location loads these design days.

    Returns:
        JSON with the station, values (deg C), accuracy or 90 % ranges, and the saved DDY path.
    """
    if mode not in ("ashrae_method", "enhanced"):
        raise ValueError('mode must be "ashrae_method" or "enhanced"')
    params = {"lat": lat, "lon": lon}
    if location_name:
        params["location_name"] = location_name
    if mode == "ashrae_method":
        params["edition"] = edition
        r = requests.get(f"{_BASE}/design-conditions/ashrae-method", params=params, timeout=60)
    else:
        params["current_climate"] = str(bool(current_climate)).lower()
        r = requests.get(f"{_BASE}/design-conditions/enhanced", params=params, timeout=120)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:300]
        return json.dumps({"mode": mode, "error": detail, "status_code": r.status_code}, indent=2)
    data = r.json()
    ddy, fname = data.pop("ddy_content", None), data.pop("ddy_filename", None)
    if mode == "enhanced" and save_to:
        # The Enhanced DDY is the paid part (1 credit); the values above are free.
        if not _API_KEY:
            data["ddy_note"] = ("The Enhanced DDY costs 1 credit: set EPWTOOLS_API_KEY to save it. Values are free.")
        else:
            body = {"lat": lat, "lon": lon, "current_climate": bool(current_climate)}
            if location_name:
                body["location_name"] = location_name
            d = requests.post(f"{_BASE}/design-conditions/enhanced/ddy", json=body, headers=_headers(), timeout=120)
            if d.status_code == 200:
                ddy, fname = d.json()["ddy_content"], d.json()["ddy_filename"]
                data["credits_charged"] = d.json().get("credits_charged")
            else:
                try:
                    data["ddy_note"] = str(d.json().get("detail"))[:300]
                except ValueError:
                    data["ddy_note"] = d.text[:300]
    if mode == "enhanced":
        data.pop("evidence", None)
        data["values"] = {k: {"value": (f["current_climate"] if current_climate and f.get("current_climate")
                                        is not None else f["site"]), "range90": f.get("range90")}
                          for k, f in data.pop("fields", {}).items()}
    data.pop("ddy", None)
    if save_to and ddy:
        import pathlib
        folder = pathlib.Path(os.path.expanduser(save_to)).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        name = (os.path.basename(ddy_name).rsplit(".", 1)[0] + ".ddy") if ddy_name else os.path.basename(fname)
        (folder / name).write_text(ddy, encoding="utf-8")
        data["saved_ddy"] = str(folder / name)
    data["mode_note"] = ("ASHRAE Handbook method, replicated by EPW.tools" if mode == "ashrae_method"
                         else "EPW.tools Enhanced: not the ASHRAE method")
    return json.dumps(data, indent=2)


@mcp.tool()
def list_cmip6_models() -> str:
    """Return the list of available CMIP6 GCM models for future climate generation."""
    r = requests.get(f"{_BASE}/cmip6/models", timeout=10)
    r.raise_for_status()
    return json.dumps(r.json(), indent=2)


@mcp.tool()
def get_nearest_stations(lat: float, lon: float, n: int = 5) -> str:
    """
    Return the n nearest active weather stations to a given coordinate.

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.
        n: Number of stations to return (1–50).

    Returns:
        JSON array of station objects with name, WMO, lat, lon, distance_km.
    """
    r = requests.get(f"{_BASE}/stations/nearest", params={"lat": lat, "lon": lon, "n": n}, timeout=10)
    r.raise_for_status()
    return json.dumps(r.json(), indent=2)


@mcp.tool()
def get_job_result(job_id: str) -> str:
    """
    Check the status and result of any previously submitted job.

    Args:
        job_id: The UUID returned by any generate_* tool.

    Returns:
        JSON with status, logs, and result dict.
    """
    r = requests.get(f"{_BASE}/jobs/{job_id}", headers=_headers(), timeout=10)
    r.raise_for_status()
    return json.dumps(r.json(), indent=2)


# ── Entry point ───────────────────────────────────────────────────────────────

def _assert_safe_transport() -> None:
    """R4-5 — refuse to start a network-reachable MCP transport unauthenticated.

    ``mcp.run()`` defaults to stdio (no listener, safe). If an operator opts
    into an HTTP/SSE transport via ``EPWTOOLS_MCP_TRANSPORT`` and binds a
    non-loopback host, every generate_* tool becomes an open credit-spend
    proxy — so we hard-require an ``EPWTOOLS_MCP_BEARER`` in that configuration
    and fail closed if it is missing. (The bearer must be enforced per request
    by whatever HTTP layer fronts this; this guard makes the unauthenticated
    misconfiguration impossible to launch.)
    """
    transport = os.environ.get("EPWTOOLS_MCP_TRANSPORT", "stdio").strip().lower()
    host = os.environ.get("EPWTOOLS_MCP_HOST", "127.0.0.1").strip()
    if transport in ("stdio", ""):
        return
    loopback = host in ("127.0.0.1", "::1", "localhost", "")
    if not loopback and not os.environ.get("EPWTOOLS_MCP_BEARER", "").strip():
        raise SystemExit(
            "Refusing to start: MCP transport %r bound to non-loopback host %r "
            "without EPWTOOLS_MCP_BEARER set. A network-reachable MCP server "
            "would be an unauthenticated credit-spend proxy. Set a bearer token "
            "(enforced per request) or bind to 127.0.0.1 / use stdio." % (transport, host)
        )


def main() -> None:
    """Console-script entry point (``epw-tools-mcp``). Speaks MCP over stdio."""
    _assert_safe_transport()
    mcp.run()


if __name__ == "__main__":
    main()
