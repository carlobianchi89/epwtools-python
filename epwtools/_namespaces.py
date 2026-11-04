"""
epwtools._namespaces
====================
Endpoint groupings attached to ``EPWClient``.

This file holds the implementation of ``client.tools``, ``client.epw``,
``client.reports``, ``client.jobs``, and ``client.data`` so that
``client.py`` stays focused on the high-level "I want a weather file"
API surface (``client.generate()``, ``client.tmy()``, ``client.future()``).

Each namespace holds a back-reference to the parent client and uses its
``_get`` / ``_post`` helpers. Keep them thin: one method per endpoint,
docstrings short, no business logic. Anything more elaborate belongs in
the parent client where it can be discovered via tab-completion.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:  # pragma: no cover
    from .client import EPWClient


# ── /api/jobs/* ───────────────────────────────────────────────────────────────
class _JobsAPI:
    """Job lifecycle endpoints. Most users won't need this — the main
    ``client.generate()`` / ``client.tmy()`` etc. methods poll on your
    behalf. Use this when you want manual control (long-running jobs,
    callbacks, batch dashboards)."""

    def __init__(self, client: "EPWClient"):
        self._c = client

    def get(self, job_id: str) -> dict:
        """Return the full job dict (status, logs, result)."""
        return self._c._get(f"/jobs/{job_id}")

    def cancel(self, job_id: str) -> dict:
        """Cancel a queued or in-flight job. No-op if already complete."""
        # The backend route is ``DELETE /api/jobs/{id}``.
        r = self._c._session.delete(
            f"{self._c.base}/jobs/{job_id}", timeout=self._c.timeout
        )
        self._c._raise(r)
        return r.json() if r.content else {"job_id": job_id, "cancelled": True}

    def expires_at(self, job_id: str) -> dict:
        """Server-side TTL for the job's output files. Useful for archival
        before files are GC'd (default retention varies by tier)."""
        return self._c._get(f"/jobs/{job_id}/expires_at")

    def parsed_epw(self, job_id: str, epw_filename: str) -> dict:
        """Return the parsed-EPW JSON payload (header + 8760 rows)."""
        return self._c._get(f"/jobs/{job_id}/parsed-epw/{epw_filename}")

    def csv(self, job_id: str, epw_filename: str, out_dir: Path | str = ".") -> Path:
        """Download the EPW as CSV (8760 hourly rows + header)."""
        url  = f"{self._c.base}/jobs/{job_id}/csv/{epw_filename}"
        resp = self._c._session.get(url, timeout=120)
        self._c._raise(resp)
        out  = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        # Re-use the parent client's path-traversal guard (CWE-22).
        path = out / self._c._safe_filename(
            epw_filename.replace(".epw", ".csv"), "output.csv"
        )
        path.write_bytes(resp.content)
        return path

    def report_pdf(self, job_id: str, out_dir: Path | str = ".") -> Path:
        """Download the branded PDF report for a completed job."""
        url  = f"{self._c.base}/jobs/{job_id}/report.pdf"
        resp = self._c._session.get(url, timeout=120)
        self._c._raise(resp)
        out  = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{job_id[:8]}_report.pdf"
        path.write_bytes(resp.content)
        return path


# ── /api/tools/* ──────────────────────────────────────────────────────────────
class _ToolsAPI:
    """Post-processing tools that operate on EPW files (or coordinates).

    Most methods here take a *path to a local EPW file* and return either
    a JSON payload (``epw_diff``, ``koppen``) or a path to a transformed
    output (``uhi_adjust``, ``inject_event``, ``altitude_correct``).
    """

    def __init__(self, client: "EPWClient"):
        self._c = client

    # --- EPW-file mutations (return modified EPW path) -----------------------

    def _upload_and_return_epw(
        self,
        endpoint: str,
        epw_path: Path | str,
        out_dir: Path | str,
        extra_data: dict | None = None,
    ) -> Path:
        """Common upload-EPW-and-download-modified-EPW helper."""
        epw_path = Path(epw_path)
        if not epw_path.exists():
            raise FileNotFoundError(f"EPW file not found: {epw_path}")
        with epw_path.open("rb") as fh:
            resp = self._c._session.post(
                f"{self._c.base}{endpoint}",
                files={"file": (epw_path.name, fh, "application/octet-stream")},
                data=extra_data or {},
                timeout=120,
            )
        self._c._raise(resp)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        out_path = out / self._c._safe_filename(
            f"{epw_path.stem}_modified.epw", "modified.epw"
        )
        out_path.write_bytes(resp.content)
        return out_path

    def uhi_adjust(self, epw_path: Path | str, ucz: str = "UCZ-2",
                   out_dir: Path | str = ".") -> Path:
        """Apply Urban Heat Island morphing for a given Urban Climate Zone
        (``UCZ-1`` dense urban → ``UCZ-7`` rural)."""
        return self._upload_and_return_epw(
            "/tools/uhi-adjust", epw_path, out_dir, {"ucz": ucz}
        )

    def inject_event(self, epw_path: Path | str, event_type: str = "heatwave",
                     start_doy: int = 200, duration_days: int = 5,
                     intensity_c: float = 5.0,
                     out_dir: Path | str = ".") -> Path:
        """Overlay a synthetic extreme event on an existing EPW.

        Parameters
        ----------
        event_type: ``heatwave`` | ``coldsnap`` | ``windstorm`` | ``humidity``.
        """
        return self._upload_and_return_epw(
            "/tools/inject-event", epw_path, out_dir,
            {"event_type": event_type, "start_doy": start_doy,
             "duration_days": duration_days, "intensity_c": intensity_c},
        )

    def wildfire_smoke(self, epw_path: Path | str,
                       start_doy: int = 200, duration_days: int = 7,
                       aod: float = 1.5,
                       out_dir: Path | str = ".") -> Path:
        """Attenuate solar radiation for wildfire smoke / haze conditions.

        ``aod`` is aerosol optical depth at 550 nm (typical: 0.1 clear,
        1.0 hazy, 3.0 dense smoke)."""
        return self._upload_and_return_epw(
            "/tools/wildfire-smoke", epw_path, out_dir,
            {"start_doy": start_doy, "duration_days": duration_days, "aod": aod},
        )

    def altitude_correct(self, epw_path: Path | str, target_elev_m: float,
                         out_dir: Path | str = ".") -> Path:
        """Re-pressure / lapse-rate-correct an EPW for a different elevation."""
        return self._upload_and_return_epw(
            "/tools/altitude-correct", epw_path, out_dir,
            {"target_elev_m": target_elev_m},
        )

    def solar_calibrate(self, epw_path: Path | str,
                        out_dir: Path | str = ".") -> Path:
        """Replace MERRA-2 irradiance with NREL PSM v3 (US locations only)."""
        return self._upload_and_return_epw(
            "/tools/solar-calibrate", epw_path, out_dir,
        )

    def gap_fill(self, epw_path: Path | str,
                 out_dir: Path | str = ".") -> Path:
        """Fill missing values in a partial EPW using reanalysis backfill."""
        return self._upload_and_return_epw(
            "/tools/gap-fill", epw_path, out_dir,
        )

    def merge_epw(self, files: list[Path | str], out_dir: Path | str = ".",
                  out_name: str = "merged.epw") -> Path:
        """Merge multiple partial EPWs into one. Files must cover disjoint
        time windows or share the same WMO ID."""
        upload_files = []
        opened = []
        try:
            for p in files:
                pp = Path(p)
                fh = pp.open("rb")
                opened.append(fh)
                upload_files.append(("files", (pp.name, fh, "application/octet-stream")))
            resp = self._c._session.post(
                f"{self._c.base}/tools/merge-epw",
                files=upload_files, timeout=120,
            )
        finally:
            for fh in opened:
                fh.close()
        self._c._raise(resp)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / self._c._safe_filename(out_name, "merged.epw")
        path.write_bytes(resp.content)
        return path

    # --- EPW-file analysis (return JSON) -------------------------------------

    def epw_diff(self, baseline: Path | str, modified: Path | str) -> dict:
        """Hourly delta between two EPWs (used by reviewers to verify
        morphing was sensible)."""
        bp, mp = Path(baseline), Path(modified)
        with bp.open("rb") as fh1, mp.open("rb") as fh2:
            resp = self._c._session.post(
                f"{self._c.base}/tools/epw-diff",
                files={
                    "baseline": (bp.name, fh1, "application/octet-stream"),
                    "modified": (mp.name, fh2, "application/octet-stream"),
                },
                timeout=120,
            )
        self._c._raise(resp)
        return resp.json()

    def koppen(self, epw_path: Path | str) -> dict:
        """Return the Köppen–Geiger and ASHRAE/IECC climate-zone for an EPW."""
        epw_path = Path(epw_path)
        with epw_path.open("rb") as fh:
            resp = self._c._session.post(
                f"{self._c.base}/tools/koppen",
                files={"file": (epw_path.name, fh, "application/octet-stream")},
                timeout=60,
            )
        self._c._raise(resp)
        return resp.json()

    def solar_check(self, epw_path: Path | str) -> dict:
        """Independent solar-data sanity check (Zhang–Huang 2002)."""
        epw_path = Path(epw_path)
        with epw_path.open("rb") as fh:
            resp = self._c._session.post(
                f"{self._c.base}/tools/solar-check",
                files={"file": (epw_path.name, fh, "application/octet-stream")},
                timeout=60,
            )
        self._c._raise(resp)
        return resp.json()

    def ground_temps(self, epw_path: Path | str) -> dict:
        """Compute monthly ground-temperature profile (Kusuda 1965)."""
        epw_path = Path(epw_path)
        with epw_path.open("rb") as fh:
            resp = self._c._session.post(
                f"{self._c.base}/tools/ground-temps",
                files={"file": (epw_path.name, fh, "application/octet-stream")},
                timeout=60,
            )
        self._c._raise(resp)
        return resp.json()

    def billing_dd(self, epw_path: Path | str,
                   heating_balance: float = 18.3,
                   cooling_balance: float = 18.3) -> dict:
        """Heating / cooling / billing degree-days at given balance points."""
        epw_path = Path(epw_path)
        with epw_path.open("rb") as fh:
            resp = self._c._session.post(
                f"{self._c.base}/tools/billing-dd",
                files={"file": (epw_path.name, fh, "application/octet-stream")},
                data={"heating_balance": heating_balance,
                      "cooling_balance": cooling_balance},
                timeout=60,
            )
        self._c._raise(resp)
        return resp.json()

    def monthly_dd(self, epw_path: Path | str,
                   heating_balance: float = 18.3,
                   cooling_balance: float = 18.3) -> dict:
        """Monthly HDD / CDD breakdown."""
        epw_path = Path(epw_path)
        with epw_path.open("rb") as fh:
            resp = self._c._session.post(
                f"{self._c.base}/tools/monthly-dd",
                files={"file": (epw_path.name, fh, "application/octet-stream")},
                data={"heating_balance": heating_balance,
                      "cooling_balance": cooling_balance},
                timeout=60,
            )
        self._c._raise(resp)
        return resp.json()

    def uhi_predict(self, lat: float, lon: float) -> dict:
        """Predicted UHI ΔT for a coordinate: fetches the site's land-cover and terrain features and
        returns them with the model's night and day increments (delta_t_night, delta_t_day)."""
        return self._c._get(f"/tools/uhi/features?lat={lat}&lon={lon}")

    def ucz_types(self) -> list[dict]:
        """List of supported Urban Climate Zone classes."""
        return self._c._get("/tools/uhi/ucz-types")


# ── /api/epw/* (visualisation + analysis on a job's EPW) ──────────────────────
class _EPWAnalysisAPI:
    """EPW visualisation endpoints (heatmap, wind-rose, psychrometric, etc.).
    These operate on a *job_id + filename* pair rather than uploading raw
    EPWs — call them after :meth:`EPWClient.generate` returns."""

    def __init__(self, client: "EPWClient"):
        self._c = client

    def _q(self, endpoint: str, job_id: str, filename: str, **extra) -> dict:
        params = {"job_id": job_id, "filename": filename, **extra}
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        return self._c._get(f"{endpoint}?{qs}")

    def wind_rose(self, job_id: str, filename: str) -> dict:
        """16 directions × 6 speed bins."""
        return self._q("/epw/wind-rose", job_id, filename)

    def heatmap(self, job_id: str, filename: str, variable: str = "dry_bulb") -> dict:
        """Hourly heatmap (8760 cells = 365 days × 24 hours) for a variable."""
        return self._q("/epw/heatmap", job_id, filename, variable=variable)

    def psychro(self, job_id: str, filename: str) -> dict:
        """Psychrometric-chart points (8760 dry-bulb / humidity-ratio pairs)."""
        return self._q("/epw/psychro", job_id, filename)

    def sun_path(self, job_id: str, filename: str) -> dict:
        """Sun-path geometry (azimuth / altitude) per hour."""
        return self._q("/epw/sun-path", job_id, filename)

    def koppen(self, job_id: str, filename: str) -> dict:
        """Köppen–Geiger classification for the EPW."""
        return self._q("/epw/koppen", job_id, filename)

    def pv_yield(self, job_id: str, filename: str,
                 dc_capacity_kw: float = 1.0, tilt: float = 30,
                 azimuth: float = 180) -> dict:
        """PV simulation (annual & monthly kWh per kW DC)."""
        return self._q(
            "/epw/pv-yield", job_id, filename,
            dc_capacity_kw=dc_capacity_kw, tilt=tilt, azimuth=azimuth,
        )

    def demand(self, job_id: str, filename: str) -> dict:
        """Heating / cooling demand profile (HDD / CDD / monthly)."""
        return self._q("/epw/demand", job_id, filename)


# ── /api/reports/* ────────────────────────────────────────────────────────────
class _ReportsAPI:
    """Domain-specific PDF/JSON reports."""

    def __init__(self, client: "EPWClient"):
        self._c = client

    def _post_epw(self, endpoint: str, epw_path: Path | str,
                  extra: dict | None = None) -> dict:
        epw_path = Path(epw_path)
        with epw_path.open("rb") as fh:
            resp = self._c._session.post(
                f"{self._c.base}{endpoint}",
                files={"file": (epw_path.name, fh, "application/octet-stream")},
                data=extra or {},
                timeout=120,
            )
        self._c._raise(resp)
        return resp.json()

    def solar(self, epw_path: Path | str) -> dict:
        """Solar-design report (POA, PR, Bird clear-sky, monthly + annual)."""
        return self._post_epw("/reports/solar", epw_path)

    def viticulture(self, epw_path: Path | str) -> dict:
        """Viticulture report (GDD, Huglin, Winkler region, frost risk)."""
        return self._post_epw("/reports/viticulture", epw_path)

    def datacenter(self, epw_path: Path | str) -> dict:
        """Datacenter report (free-cooling hours, mechanical cooling, PUE est.)."""
        return self._post_epw("/reports/datacenter", epw_path)

    def stormwater(self, epw_path: Path | str) -> dict:
        """Stormwater IDF curves (intensity-duration-frequency)."""
        return self._post_epw("/reports/stormwater", epw_path)

    def insurance(self, epw_path: Path | str,
                  heat_threshold: float = 38, freeze_threshold: float = -10,
                  wind_threshold: float = 20) -> dict:
        """Parametric-insurance triggers (heat days / freeze days / wind events)."""
        return self._post_epw(
            "/reports/insurance", epw_path,
            {"heat_threshold": heat_threshold,
             "freeze_threshold": freeze_threshold,
             "wind_threshold": wind_threshold},
        )


# ── /api/cities, /api/stations, /api/cmip6, /api/qc-report ────────────────────
class _DataAPI:
    """Read-only catalog endpoints — useful for building UIs on top of
    the API or pre-flight-checking that data is available for a location."""

    def __init__(self, client: "EPWClient"):
        self._c = client

    def cities(self) -> list[dict]:
        """All ~3,000 curated cities the platform knows about."""
        return self._c._get("/cities")

    def city_instant(self, slug: str) -> dict:
        """Pre-cached instant-preview EPW for a city (no credits)."""
        return self._c._get(f"/cities/{slug}/instant")

    def stations_isd(self, lat: float, lon: float, radius_km: float = 50) -> list[dict]:
        """NOAA ISD stations within ``radius_km`` of a point."""
        return self._c._get(f"/stations/isd?lat={lat}&lon={lon}&radius_km={radius_km}")

    def stations_nearby(self, lat: float, lon: float,
                        radius_km: float = 50, limit: int = 10) -> list[dict]:
        """All known stations (ISD + Meteostat + airport ICAOs) within radius."""
        return self._c._get(
            f"/stations/nearby?lat={lat}&lon={lon}"
            f"&radius_km={radius_km}&limit={limit}"
        )

    def stations_nearest(self, lat: float, lon: float) -> dict:
        """Single nearest station (for "preview station name" UIs)."""
        return self._c._get(f"/stations/nearest?lat={lat}&lon={lon}")

    def design_conditions(self, lat: float, lon: float, edition: str = "2025") -> dict:
        """ASHRAE design conditions for the nearest reference station."""
        return self._c._get(
            f"/design-conditions?lat={lat}&lon={lon}&edition={edition}"
        )

    def qc_report(self, job_id: str) -> dict:
        """WMO No.8 / NWS QC report for a completed job."""
        return self._c._get(f"/qc-report?job_id={job_id}")

    def cmip6_models(self) -> list[dict]:
        """Available CMIP6 GCMs for future-climate morphing."""
        return self._c._get("/cmip6/models")

    def future_model_range(self, lat: float, lon: float,
                           scenario: str = "ssp245",
                           horizon: int = 2050) -> dict:
        """Multi-model range (min / median / max ΔT) at a location."""
        return self._c._get(
            f"/future/model-range?lat={lat}&lon={lon}"
            f"&scenario={scenario}&horizon={horizon}"
        )


__all__ = [
    "_JobsAPI", "_ToolsAPI", "_EPWAnalysisAPI", "_ReportsAPI", "_DataAPI",
]
