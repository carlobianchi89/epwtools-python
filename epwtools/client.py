"""
epwtools.client
===============
Thin Python client for the epw.tools REST API.

Quick start
-----------
    from epwtools import EPWClient

    client = EPWClient(api_key="your-key-here")

    # Single-year AMY
    epw_path = client.generate(lat=40.71, lon=-74.01, year=2023, name="NewYork")

    # Typical Meteorological Year
    epw_path = client.tmy(lat=51.50, lon=-0.12, start=2005, end=2024, name="London")

    # Future climate (CMIP6 morphed)
    epw_path = client.future(lat=48.85, lon=2.35, scenario="ssp245", horizon=2050, name="Paris")

    # Portfolio climate stress-test (no credits consumed)
    table = client.portfolio_stress_test([
        {"lat": 40.71, "lon": -74.01, "label": "New York HQ"},
        {"lat": 51.50, "lon": -0.12,  "label": "London Office"},
    ], scenario="ssp245", horizon=2050)

The API base URL defaults to https://epw.tools/api and can be overridden
with the ``EPWTOOLS_API_URL`` environment variable or the ``api_url`` constructor
argument.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

from ._namespaces import (
    _DataAPI,
    _EPWAnalysisAPI,
    _JobsAPI,
    _ReportsAPI,
    _ToolsAPI,
)

_DEFAULT_BASE = "https://epw.tools/api"

# "era5" was documented here until 0.5.1 but the API never accepted it (422). ERA5 is served as "open-meteo".
_SOURCE_ALIASES = {"era5": "open-meteo"}


def _source(s: str) -> str:
    return _SOURCE_ALIASES.get((s or "merra2").lower(), (s or "merra2").lower())
_POLL_INTERVAL = 2   # seconds between status polls
_POLL_TIMEOUT  = 600 # max seconds to wait for a job


class EPWToolsError(Exception):
    """Raised when the epw.tools API returns an error."""


def _sdk_version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:  # noqa: BLE001
        return "0"


class EPWClient:
    """Programmatic client for the epw.tools REST API.

    Parameters
    ----------
    api_key:
        Your epw.tools API key (obtain at https://epw.tools → API tab).
    api_url:
        Override the base API URL (useful for self-hosted instances).
    timeout:
        Per-request HTTP timeout in seconds.
    """

    def __init__(
        self,
        api_key: str = "",
        api_url: str | None = None,
        timeout: int = 30,
    ):
        if not _HAS_REQUESTS:
            raise ImportError(
                "epwtools requires the 'requests' package. "
                "Install it with: pip install requests"
            )
        self.api_key = api_key or os.environ.get("EPWTOOLS_API_KEY", "")
        self.base    = (api_url or os.environ.get("EPWTOOLS_API_URL", _DEFAULT_BASE)).rstrip("/")
        self.timeout = timeout
        self._session = _requests.Session()
        self._session.headers["User-Agent"] = f"epwtools-python/{_sdk_version()}"
        if self.api_key:
            self._session.headers["X-API-Key"] = self.api_key

        # Endpoint-namespace shortcuts. See ``epwtools/_namespaces.py``.
        # Use these when you want to reach a specific REST endpoint that
        # the high-level ``generate()`` / ``tmy()`` / ``future()`` family
        # doesn't expose directly.
        self.jobs    = _JobsAPI(self)
        self.tools   = _ToolsAPI(self)
        self.epw     = _EPWAnalysisAPI(self)
        self.reports = _ReportsAPI(self)
        self.data    = _DataAPI(self)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get(self, path: str, **kwargs) -> Any:
        r = self._session.get(f"{self.base}{path}", timeout=self.timeout, **kwargs)
        self._raise(r)
        return r.json()

    def _post(self, path: str, json: dict | None = None, files=None, data=None) -> Any:
        r = self._session.post(
            f"{self.base}{path}",
            json=json, files=files, data=data,
            timeout=self.timeout,
        )
        self._raise(r)
        return r.json()

    @staticmethod
    def _raise(r: "_requests.Response"):
        if not r.ok:
            try:
                detail = r.json().get("detail", r.text[:200])
            except Exception:
                detail = r.text[:200]
            raise EPWToolsError(f"HTTP {r.status_code}: {detail}")

    @staticmethod
    def _safe_filename(name: str, fallback: str) -> str:
        """Reduce a server-supplied filename to a bare, safe basename.

        Filenames arrive from response JSON and Content-Disposition headers;
        without this, a compromised or spoofed endpoint returning
        ``../../.bashrc`` would write outside out_dir (CWE-22).
        """
        base = Path(str(name).replace("\\", "/")).name
        base = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*')
        if not base or base in (".", ".."):
            return fallback
        return base

    def _poll(
        self,
        job_id: str,
        progress_cb: Callable[[str], None] | None = None,
    ) -> dict:
        """Block until job completes or fails. Returns the full job dict."""
        seen_logs = 0
        deadline  = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            job = self._get(f"/jobs/{job_id}")
            logs: list[str] = job.get("logs", [])
            for line in logs[seen_logs:]:
                if progress_cb:
                    progress_cb(line)
            seen_logs = len(logs)
            if job["status"] == "complete":
                return job
            if job["status"] == "failed":
                err = (job.get("result") or {}).get("error", "unknown error")
                raise EPWToolsError(f"Job {job_id} failed: {err}")
            time.sleep(_POLL_INTERVAL)
        raise EPWToolsError(f"Job {job_id} timed out after {_POLL_TIMEOUT}s")

    def _download_result(
        self,
        job: dict,
        out_dir: Path,
        extensions: tuple[str, ...] = ("epw", "ddy", "stat"),
    ) -> Path:
        """Download output files; return the path to the EPW."""
        result   = job.get("result", {})
        epw_path = None
        out_dir  = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        job_id   = job["job_id"] if "job_id" in job else job.get("id", "")

        for ext in extensions:
            fname = result.get(f"{ext}_file")
            if not fname:
                continue
            url  = f"{self.base}/jobs/{job_id}/download/{fname}"
            resp = self._session.get(url, timeout=120)
            self._raise(resp)
            path = out_dir / self._safe_filename(fname, f"output.{ext}")
            path.write_bytes(resp.content)
            if ext == "epw":
                epw_path = path

        if epw_path is None:
            raise EPWToolsError("Job completed but no EPW file was returned.")
        return epw_path

    # ── Public API ────────────────────────────────────────────────────────────

    def generate(
        self,
        lat:         float,
        lon:         float,
        year:        int,
        name:        str  = "Location",
        source:      str  = "merra2",
        dc_edition:  str  = "2025",
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
        leap_day:    str  = "remove",
    ) -> Path:
        """Generate a single-year Actual Meteorological Year (AMY) EPW file.

        Parameters
        ----------
        lat, lon: Coordinates (decimal degrees).
        year: Calendar year to generate (1990–present).
        name: Location name embedded in the EPW header.
        source: ``"merra2"`` (Standard, global, default), ``"open-meteo"`` (ERA5 via Open-Meteo, global),
            ``"hrrr"`` (3 km, US only), ``"satellite"`` (satellite solar; Europe, Africa, South America, Asia,
            Australia) or ``"cweeds"`` (Canadian station solar). ``"era5"`` is accepted as ``"open-meteo"``.
        dc_edition: ASHRAE design conditions edition (``"2025"`` recommended).
        out_dir: Directory to write the EPW (and DDY, STAT) files.
        progress_cb: Optional callback ``fn(log_line: str)`` for streaming progress.
        leap_day: Leap years only. ``"remove"`` (default) drops 29 February: 8760 hours, read by every tool, but
            EnergyPlus weekdays from 1 March fall one day early. ``"keep"`` writes the true calendar (8784 hours).

        Returns
        -------
        Path to the downloaded EPW file.
        """
        payload = {
            "lat": lat, "lon": lon, "year": year,
            "location_name": name, "file_type": "AMY",
            "data_source": _source(source), "dc_edition": dc_edition, "leap_day": leap_day,
        }
        resp   = self._post("/generate/individual", json=payload)
        job    = self._poll(resp["job_id"], progress_cb)
        job["job_id"] = resp["job_id"]
        return self._download_result(job, Path(out_dir))

    def tmy(
        self,
        lat:         float,
        lon:         float,
        start:       int,
        end:         int,
        name:        str  = "Location",
        dc_edition:  str  = "2025",
        weights:     dict | None = None,
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
    ) -> Path:
        """Generate a Typical Meteorological Year (TMY) EPW file.

        Parameters
        ----------
        start, end: Inclusive year range for the TMY optimisation (e.g. 2005, 2024).
        weights: Optional dict of variable weights for the Finkelstein-Schafer
            method. Keys: ``"dbt"`` (dry bulb), ``"dwpt"`` (dew point), ``"ghi"``, ``"dni"``, ``"dhi"``,
            ``"wspd"`` (wind speed), ``"pres"``; e.g. ``{"dbt": 4, "dwpt": 4, "ghi": 12, "wspd": 3}``
            (Sandia TMY3). Unknown keys are rejected. Custom weights are a paid option.

        Returns
        -------
        Path to the downloaded EPW file.
        """
        payload = {
            "lat": lat, "lon": lon, "location_name": name,
            "start_year": start, "end_year": end,
            "dc_edition": dc_edition, "weights": weights or {},
        }
        resp   = self._post("/generate/tmy", json=payload)
        job    = self._poll(resp["job_id"], progress_cb)
        job["job_id"] = resp["job_id"]
        return self._download_result(job, Path(out_dir))

    def xmy(
        self,
        lat:         float,
        lon:         float,
        start:       int,
        end:         int,
        mode:        str  = "hot",
        name:        str  = "Location",
        dc_edition:  str  = "2025",
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
    ) -> Path:
        """Generate an Extreme Meteorological Year (XMY) EPW file.

        Parameters
        ----------
        mode: ``"hot"`` | ``"cold"`` | ``"solar"`` | ``"wind"``

        Returns
        -------
        Path to the downloaded EPW file.
        """
        payload = {
            "lat": lat, "lon": lon, "location_name": name,
            "start_year": start, "end_year": end,
            "mode": mode, "dc_edition": dc_edition,
        }
        resp   = self._post("/generate/xmy", json=payload)
        job    = self._poll(resp["job_id"], progress_cb)
        job["job_id"] = resp["job_id"]
        return self._download_result(job, Path(out_dir))

    def future(
        self,
        lat:         float,
        lon:         float,
        scenario:    str  = "ssp245",
        horizon:     int  = 2050,
        model:       str  = "MRI-ESM2-0",
        base_year:   int  = 2010,
        name:        str  = "Location",
        dc_edition:  str  = "2025",
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
    ) -> Path:
        """Generate a future climate EPW via CMIP6 morphing (Belcher 2008).

        Parameters
        ----------
        scenario: SSP scenario — ``"ssp126"`` | ``"ssp245"`` | ``"ssp370"`` | ``"ssp585"``.
        horizon: Target year — any decade from ``2030`` to ``2100``.
        model: CMIP6 model name (default ``"MRI-ESM2-0"``).

        Returns
        -------
        Path to the downloaded EPW file.
        """
        payload = {
            "lat": lat, "lon": lon, "location_name": name,
            "base_year": base_year, "scenario": scenario,
            "horizon": horizon, "model": model, "dc_edition": dc_edition,
        }
        resp   = self._post("/generate/future-climate", json=payload)
        job    = self._poll(resp["job_id"], progress_cb)
        job["job_id"] = resp["job_id"]
        return self._download_result(job, Path(out_dir))

    def batch(
        self,
        locations: list[dict],
        year:        int | None = None,
        gen_type:    str = "individual",
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
    ) -> list[Path]:
        """Submit a batch of locations for parallel EPW generation.

        Parameters
        ----------
        locations: List of dicts with keys ``lat``, ``lon``, ``name``
            (and ``year`` for individual generation).
        gen_type: ``"individual"`` | ``"tmy"`` | ``"future"``.

        Returns
        -------
        List of paths to downloaded EPW files (one per location).
        """
        rows = []
        for loc in locations:
            row: dict = {
                "lat":  loc["lat"], "lon": loc["lon"],
                "location_name": loc.get("name", loc.get("label", "Location")),
                "year": loc.get("year", year or 2023),
                "gen_type": gen_type,
            }
            rows.append(row)

        resp    = self._post("/batch/generate", json={"locations": rows})
        job_ids = [j["job_id"] for j in resp.get("jobs", [])]
        paths   = []
        for jid in job_ids:
            try:
                job = self._poll(jid, progress_cb)
                job["job_id"] = jid
                path = self._download_result(job, Path(out_dir))
                paths.append(path)
            except EPWToolsError as e:
                if progress_cb:
                    progress_cb(f"[WARN] {jid[:8]}: {e}")
        return paths

    def portfolio_stress_test(
        self,
        sites:    list[dict],
        scenario: str = "ssp245",
        horizon:  int = 2050,
        model:    str = "MRI-ESM2-0",
    ) -> list[dict]:
        """Lightweight CMIP6 climate stress-test for a portfolio of locations.

        No credits consumed — uses pre-cached CMIP6 monthly deltas.

        Parameters
        ----------
        sites: List of dicts with keys ``lat``, ``lon``, ``label``.
        scenario: SSP scenario (``"ssp126"`` | ``"ssp245"`` | ``"ssp370"`` | ``"ssp585"``).
        horizon: Target year (any decade from ``2030`` to ``2100``).

        Returns
        -------
        List of per-site result dicts with keys:
            ``label``, ``lat``, ``lon``, ``climate_zone``, ``climate_zone_2100``,
            ``cz_risk``, ``annual_delta_c``, ``peak_cooling_delta``,
            ``hdd_baseline``, ``hdd_projected``, ``cdd_baseline``, ``cdd_projected``.
        """
        resp = self._post(
            "/portfolio/stress-test",
            json={"sites": sites, "scenario": scenario, "horizon": horizon, "model": model},
        )
        return resp.get("sites", [])

    def design_conditions(
        self,
        lat:     float,
        lon:     float,
        edition: str = "2025",
    ) -> dict:
        """Return the nearest ASHRAE design conditions for a location.

        Parameters
        ----------
        lat, lon: Coordinates (decimal degrees).
        edition: ASHRAE Handbook of Fundamentals edition year.

        Returns
        -------
        Dict with ``dc_line``, ``station_name``, ``station_country``,
        ``station_lat``, ``station_lon``, ``distance_km``.
        """
        return self._get(
            f"/design-conditions?lat={lat}&lon={lon}&edition={edition}"
        )

    def compliance(self, job_id: str) -> dict:
        """Return structured ASHRAE 90.1 / 169 compliance data for a completed job.

        Parameters
        ----------
        job_id: UUID returned by :meth:`generate`, :meth:`tmy`, etc.

        Returns
        -------
        Dict with ``compliance``, ``hdd``, ``cdd``, ``climate_zone`` etc.
        """
        return self._get(f"/jobs/{job_id}/compliance")

    def download_compliance_package(self, job_id: str, out_dir: Path | str = ".") -> Path:
        """Download the full compliance ZIP package for a completed job.

        The ZIP contains the EPW, DDY, ASHRAE compliance JSON, branded PDF report,
        and a README with provenance metadata — ready for PE submission.

        Parameters
        ----------
        job_id: UUID of a completed job.
        out_dir: Directory to save the ZIP.

        Returns
        -------
        Path to the downloaded ZIP file.
        """
        url  = f"{self.base}/jobs/{job_id}/compliance-package"
        resp = self._session.get(url, timeout=120)
        self._raise(resp)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Parse filename from Content-Disposition header
        cd = resp.headers.get("Content-Disposition", "")
        fname = "compliance_package.zip"
        if "filename=" in cd:
            import re as _re
            m = _re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd)
            if m:
                fname = m.group(1).strip()

        path = out_dir / self._safe_filename(fname, "compliance_package.zip")
        path.write_bytes(resp.content)
        return path

    def export(
        self,
        epw_path: Path | str,
        fmt:     str  = "xlsx",
        out_dir: Path | str = ".",
    ) -> Path:
        """Convert an EPW file to another format.

        Parameters
        ----------
        epw_path: Path to an existing ``.epw`` file.
        fmt: Target format — one of ``wea``, ``clm``, ``pvsyst``, ``json``,
            ``xlsx``, ``parquet``, ``netcdf``, ``modelica``, ``geojson``,
            ``try``, ``iwec2``, ``wyec2``, ``tmy3``, ``hdf5``, ``bundle``.

        Returns
        -------
        Path to the converted file.
        """
        epw_path = Path(epw_path)
        if not epw_path.exists():
            raise FileNotFoundError(f"EPW file not found: {epw_path}")

        with epw_path.open("rb") as fh:
            resp = self._session.post(
                f"{self.base}/tools/export-format/{fmt}",
                files={"file": (epw_path.name, fh, "application/octet-stream")},
                timeout=120,
            )
        self._raise(resp)

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        cd   = resp.headers.get("Content-Disposition", "")
        stem = epw_path.stem
        ext  = {"wea": ".wea", "clm": ".clm", "pvsyst": ".met", "json": ".json",
                "xlsx": ".xlsx", "parquet": ".parquet", "netcdf": ".nc",
                "modelica": ".mos", "geojson": ".geojson", "try": ".try",
                "iwec2": ".epw", "wyec2": ".epw", "tmy3": ".tmy", "hdf5": ".h5",
                "bundle": ".zip"}.get(fmt, ".bin")

        fname = f"{stem}{ext}"
        path  = out_dir / fname
        path.write_bytes(resp.content)
        return path

    def status(self) -> dict:
        """Return API key status (credits remaining, tier, plan)."""
        return self._get("/keys/status")

    # ── Server-Sent Events: real-time progress streaming ────────────────────

    def stream(self, job_id: str) -> Iterator[dict]:
        """Yield progress events from ``/api/jobs/{id}/stream``.

        This is the streaming alternative to ``_poll()``: instead of
        polling every ``_POLL_INTERVAL`` seconds, the server pushes
        log lines and status changes as they happen, with sub-second
        latency.  Best for interactive CLIs and dashboards.

        Yields a dict for each event with at least ``{"type": ...}``
        and tool-specific keys.  Common types:

        - ``{"type": "log",      "line": "..."}``
        - ``{"type": "progress", "pct":  0–100}``
        - ``{"type": "status",   "status": "complete"|"failed"}``

        The generator terminates when the job reaches a terminal status
        or the connection drops.  On ``failed``, the final yielded event
        contains ``{"error": "..."}``; the caller may choose to raise.

        Example::

            client = EPWClient(api_key="...")
            resp   = client._post("/generate/individual", json={...})
            for ev in client.stream(resp["job_id"]):
                if ev["type"] == "log":
                    print("  ", ev["line"])
                elif ev["type"] == "status" and ev["status"] == "complete":
                    break
        """
        url = f"{self.base}/jobs/{job_id}/stream"
        # ``stream=True`` keeps the underlying socket open and reads
        # line-by-line; we intentionally use a long read-timeout because
        # the server holds the connection idle between events.
        with self._session.get(url, stream=True, timeout=(10, _POLL_TIMEOUT)) as r:
            self._raise(r)
            for raw in r.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                # The SSE wire format is ``data: <json>\n\n``; strip the
                # prefix and parse defensively — a malformed line should
                # not blow up the entire stream.
                payload = raw[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    ev = {"type": "log", "line": payload}
                yield ev
                # Terminal status — stop iterating.
                if ev.get("type") == "status" and ev.get("status") in (
                    "complete", "failed", "cancelled"
                ):
                    return

    # ── Additional generation variants ──────────────────────────────────────

    def multi_year(
        self,
        lat:         float,
        lon:         float,
        start:       int,
        end:         int,
        name:        str  = "Location",
        source:      str  = "merra2",
        dc_edition:  str  = "2025",
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
    ) -> Path:
        """Generate a multi-year EPW (one row per hour for the full window).

        For energy-modelling use cases that need a long historical
        record rather than a single representative year.
        """
        payload = {
            "lat": lat, "lon": lon, "location_name": name,
            "start_year": start, "end_year": end,
            "data_source": _source(source), "dc_edition": dc_edition,
        }
        resp = self._post("/generate/multi-year", json=payload)
        job  = self._poll(resp["job_id"], progress_cb)
        job["job_id"] = resp["job_id"]
        return self._download_result(job, Path(out_dir))

    def composite(
        self,
        lat:         float,
        lon:         float,
        seasons:     dict[str, int],
        name:        str  = "Location",
        dc_edition:  str  = "2025",
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
    ) -> Path:
        """Composite-year EPW that blends specific seasons from different years.

        Parameters
        ----------
        seasons: Dict mapping ``"winter"|"spring"|"summer"|"fall"`` to a
            calendar year, e.g. ``{"winter": 2018, "summer": 2022}``.
        """
        payload = {
            "lat": lat, "lon": lon, "location_name": name,
            "seasons": seasons, "dc_edition": dc_edition,
        }
        resp = self._post("/generate/composite", json=payload)
        job  = self._poll(resp["job_id"], progress_cb)
        job["job_id"] = resp["job_id"]
        return self._download_result(job, Path(out_dir))

    def ensemble(
        self,
        lat:         float,
        lon:         float,
        scenario:    str = "ssp245",
        horizon:     int = 2050,
        models:      list[str] | None = None,
        base_year:   int = 2010,
        name:        str = "Location",
        dc_edition:  str = "2025",
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
    ) -> list[Path]:
        """Multi-GCM ensemble: generate one EPW per CMIP6 model.

        Use ``models=None`` to run all 10+ models known to the API
        (recommended for proper uncertainty quantification).
        """
        payload: dict[str, Any] = {
            "lat": lat, "lon": lon, "location_name": name,
            "scenario": scenario, "horizon": horizon,
            "base_year": base_year, "dc_edition": dc_edition,
        }
        if models is not None:
            payload["models"] = models
        resp = self._post("/generate/future-climate/ensemble", json=payload)
        # Ensemble returns a list of job_ids — poll each in turn.
        out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
        paths   = []
        for jid in [j["job_id"] for j in resp.get("jobs", [])]:
            try:
                job = self._poll(jid, progress_cb)
                job["job_id"] = jid
                paths.append(self._download_result(job, out_dir))
            except EPWToolsError as e:
                if progress_cb:
                    progress_cb(f"[WARN] {jid[:8]}: {e}")
        return paths

    def metered(
        self,
        lat:           float,
        lon:           float,
        meter_csv:     Path | str,
        meter_kind:    str = "electricity",
        name:          str = "Location",
        dc_edition:    str = "2025",
        out_dir:       Path | str = ".",
        progress_cb:   Callable[[str], None] | None = None,
    ) -> Path:
        """Build an EPW from your own meter / sensor data.

        Parameters
        ----------
        meter_csv: Path to a CSV with a ``timestamp`` column and one or
            more sensor columns (``dry_bulb``, ``dew_point``, ``ghi`` etc.).
        meter_kind: Interpretation hint for the API’s ingestion layer.
        """
        meter_csv = Path(meter_csv)
        if not meter_csv.exists():
            raise FileNotFoundError(f"Meter CSV not found: {meter_csv}")
        with meter_csv.open("rb") as fh:
            resp = self._session.post(
                f"{self.base}/generate/metered",
                data={
                    "lat": lat, "lon": lon, "location_name": name,
                    "meter_kind": meter_kind, "dc_edition": dc_edition,
                },
                files={"meter": (meter_csv.name, fh, "text/csv")},
                timeout=120,
            )
        self._raise(resp)
        data = resp.json()
        job  = self._poll(data["job_id"], progress_cb)
        job["job_id"] = data["job_id"]
        return self._download_result(job, Path(out_dir))

    def csv_batch(
        self,
        csv_path:  Path | str,
        gen_type:  str = "individual",
        out_dir:   Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
    ) -> list[Path]:
        """Submit a CSV of locations for parallel EPW generation.

        The CSV must have columns ``lat``, ``lon``, ``name`` and
        (for ``gen_type="individual"``) ``year``.
        """
        csv_path = Path(csv_path)
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")
        with csv_path.open("rb") as fh:
            resp = self._session.post(
                f"{self.base}/generate/csv-batch",
                files={"file": (csv_path.name, fh, "text/csv")},
                data={"gen_type": gen_type},
                timeout=120,
            )
        self._raise(resp)
        data = resp.json()
        out  = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
        paths = []
        for jid in [j["job_id"] for j in data.get("jobs", [])]:
            try:
                job = self._poll(jid, progress_cb)
                job["job_id"] = jid
                paths.append(self._download_result(job, out))
            except EPWToolsError as e:
                if progress_cb:
                    progress_cb(f"[WARN] {jid[:8]}: {e}")
        return paths

    # ── Validation helpers ──────────────────────────────────────────────────

    def validate_compare_files(
        self,
        epw_a:    Path | str,
        epw_b:    Path | str,
    ) -> dict:
        """Compare two EPW files head-to-head; returns delta statistics."""
        a, b = Path(epw_a), Path(epw_b)
        with a.open("rb") as fh1, b.open("rb") as fh2:
            resp = self._session.post(
                f"{self.base}/validate/compare-files",
                files={
                    "epw_a": (a.name, fh1, "application/octet-stream"),
                    "epw_b": (b.name, fh2, "application/octet-stream"),
                },
                timeout=120,
            )
        self._raise(resp)
        return resp.json()
