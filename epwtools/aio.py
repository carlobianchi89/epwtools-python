"""
epwtools.aio
============
Async variant of :class:`epwtools.EPWClient`, built on httpx.

Why a separate module
---------------------
Building-portfolio workloads (e.g. "generate 200 future-climate EPWs in
parallel") spend most of their wall-clock time waiting on the API. The
sync client poll loop ``time.sleep(2)`` per job blocks the event loop
in any framework (FastAPI, Trio, AnyIO, Litestar, asyncio scripts).

This module exposes the same public surface, but every method is an
``async def`` and concurrency is bounded by an ``asyncio.Semaphore``
that the caller can size to their account's rate limit.

Quickstart
----------
    import asyncio
    from epwtools.aio import AsyncEPWClient

    async def main():
        async with AsyncEPWClient(api_key="...") as client:
            paths = await asyncio.gather(*[
                client.generate(lat=lat, lon=lon, year=2023, name=name)
                for (lat, lon, name) in PORTFOLIO
            ])
        return paths

    asyncio.run(main())

Honest limitations
------------------
- Only the high-level generation methods (``generate``, ``tmy``, ``xmy``,
  ``future``) are async-native. Tool / report / analysis endpoints can
  be reached via :attr:`AsyncEPWClient._raw` for now (low-traffic, not
  worth the duplication).
- SSE streaming is async via :meth:`AsyncEPWClient.stream`.
- This module is an optional dependency: ``pip install 'epwtools[async]'``
  installs httpx; importing without httpx raises an informative error.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

from .client import EPWToolsError, _DEFAULT_BASE, _POLL_INTERVAL, _POLL_TIMEOUT, _source


class AsyncEPWClient:
    """Asyncio-native client for the epw.tools REST API.

    Use as an async context manager so the underlying ``httpx.AsyncClient``
    is closed cleanly even on exceptions::

        async with AsyncEPWClient(api_key="...") as client:
            epw = await client.generate(lat=40.7, lon=-74.0, year=2023)

    Parameters
    ----------
    api_key: API key (or read from ``EPWTOOLS_API_KEY`` env var).
    api_url: Base URL override (or ``EPWTOOLS_API_URL`` env var).
    timeout: Per-request timeout in seconds.
    max_concurrency: Bound on simultaneous in-flight requests. The
        platform's per-key cap is 500/day for EPW generation; this
        limit governs *concurrent* requests, not the daily quota.
    """

    def __init__(
        self,
        api_key:         str = "",
        api_url:         str | None = None,
        timeout:         float = 30.0,
        max_concurrency: int = 8,
    ):
        if not _HAS_HTTPX:
            raise ImportError(
                "AsyncEPWClient requires httpx. "
                "Install with: pip install 'epwtools[aio]'"
            )
        self.api_key = api_key or os.environ.get("EPWTOOLS_API_KEY", "")
        self.base    = (api_url or os.environ.get("EPWTOOLS_API_URL", _DEFAULT_BASE)).rstrip("/")
        self.timeout = timeout
        self._sem    = asyncio.Semaphore(max_concurrency)

        from .client import _sdk_version
        headers = {"User-Agent": f"epwtools-python-async/{_sdk_version()}"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        self._client = httpx.AsyncClient(
            base_url=self.base, timeout=timeout, headers=headers,
        )

    # ── Context manager / lifecycle ──────────────────────────────────────────

    async def __aenter__(self) -> "AsyncEPWClient":
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _post(self, path: str, json: dict | None = None) -> Any:
        async with self._sem:
            r = await self._client.post(path, json=json)
            self._raise(r)
            return r.json()

    async def _get(self, path: str) -> Any:
        async with self._sem:
            r = await self._client.get(path)
            self._raise(r)
            return r.json()

    @staticmethod
    def _raise(r: "httpx.Response"):
        if r.is_success:
            return
        try:
            detail = r.json().get("detail", r.text[:200])
        except Exception:
            detail = r.text[:200]
        raise EPWToolsError(f"HTTP {r.status_code}: {detail}")

    @staticmethod
    def _safe_filename(name: str, fallback: str) -> str:
        """Same path-traversal guard as the sync client (CWE-22)."""
        base = Path(str(name).replace("\\", "/")).name
        base = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*')
        if not base or base in (".", ".."):
            return fallback
        return base

    async def _poll(
        self,
        job_id: str,
        progress_cb: Callable[[str], None] | None = None,
    ) -> dict:
        """Async equivalent of :meth:`EPWClient._poll`."""
        seen_logs = 0
        deadline  = asyncio.get_event_loop().time() + _POLL_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            job  = await self._get(f"/jobs/{job_id}")
            logs = job.get("logs", [])
            for line in logs[seen_logs:]:
                if progress_cb:
                    progress_cb(line)
            seen_logs = len(logs)
            if job["status"] == "complete":
                return job
            if job["status"] == "failed":
                err = (job.get("result") or {}).get("error", "unknown error")
                raise EPWToolsError(f"Job {job_id} failed: {err}")
            await asyncio.sleep(_POLL_INTERVAL)
        raise EPWToolsError(f"Job {job_id} timed out after {_POLL_TIMEOUT}s")

    async def _download_result(
        self,
        job: dict,
        out_dir: Path,
        extensions: tuple[str, ...] = ("epw", "ddy", "stat"),
    ) -> Path:
        """Download the EPW (and DDY/STAT) for a completed job."""
        result   = job.get("result", {})
        epw_path = None
        out_dir  = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
        job_id   = job.get("job_id") or job.get("id", "")

        for ext in extensions:
            fname = result.get(f"{ext}_file")
            if not fname:
                continue
            url = f"/jobs/{job_id}/download/{fname}"
            async with self._sem:
                r = await self._client.get(url, timeout=120)
                self._raise(r)
            path = out_dir / self._safe_filename(fname, f"output.{ext}")
            path.write_bytes(r.content)
            if ext == "epw":
                epw_path = path

        if epw_path is None:
            raise EPWToolsError("Job completed but no EPW file was returned.")
        return epw_path

    # ── Public generation API ────────────────────────────────────────────────

    async def generate(
        self,
        lat:         float,
        lon:         float,
        year:        int,
        name:        str = "Location",
        source:      str = "merra2",
        dc_edition:  str = "2025",
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
        leap_day:    str = "remove",
    ) -> Path:
        """Async single-year AMY generation. See :meth:`EPWClient.generate`."""
        payload = {
            "lat": lat, "lon": lon, "year": year,
            "location_name": name, "file_type": "AMY",
            "data_source": _source(source), "dc_edition": dc_edition, "leap_day": leap_day,
        }
        resp = await self._post("/generate/individual", json=payload)
        job  = await self._poll(resp["job_id"], progress_cb)
        job["job_id"] = resp["job_id"]
        return await self._download_result(job, Path(out_dir))

    async def tmy(
        self,
        lat:         float,
        lon:         float,
        start:       int,
        end:         int,
        name:        str = "Location",
        dc_edition:  str = "2025",
        weights:     dict | None = None,
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
    ) -> Path:
        """Async TMY generation. See :meth:`EPWClient.tmy`."""
        payload = {
            "lat": lat, "lon": lon, "location_name": name,
            "start_year": start, "end_year": end,
            "dc_edition": dc_edition, "weights": weights or {},
        }
        resp = await self._post("/generate/tmy", json=payload)
        job  = await self._poll(resp["job_id"], progress_cb)
        job["job_id"] = resp["job_id"]
        return await self._download_result(job, Path(out_dir))

    async def xmy(
        self,
        lat:         float,
        lon:         float,
        start:       int,
        end:         int,
        mode:        str = "hot",
        name:        str = "Location",
        dc_edition:  str = "2025",
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
    ) -> Path:
        """Async XMY generation. See :meth:`EPWClient.xmy`."""
        payload = {
            "lat": lat, "lon": lon, "location_name": name,
            "start_year": start, "end_year": end,
            "mode": mode, "dc_edition": dc_edition,
        }
        resp = await self._post("/generate/xmy", json=payload)
        job  = await self._poll(resp["job_id"], progress_cb)
        job["job_id"] = resp["job_id"]
        return await self._download_result(job, Path(out_dir))

    async def future(
        self,
        lat:         float,
        lon:         float,
        scenario:    str = "ssp245",
        horizon:     int = 2050,
        model:       str = "MRI-ESM2-0",
        base_year:   int = 2010,
        name:        str = "Location",
        dc_edition:  str = "2025",
        out_dir:     Path | str = ".",
        progress_cb: Callable[[str], None] | None = None,
    ) -> Path:
        """Async future-climate generation. See :meth:`EPWClient.future`."""
        payload = {
            "lat": lat, "lon": lon, "location_name": name,
            "base_year": base_year, "scenario": scenario,
            "horizon": horizon, "model": model, "dc_edition": dc_edition,
        }
        resp = await self._post("/generate/future-climate", json=payload)
        job  = await self._poll(resp["job_id"], progress_cb)
        job["job_id"] = resp["job_id"]
        return await self._download_result(job, Path(out_dir))

    # ── Async SSE streaming ──────────────────────────────────────────────────

    async def stream(self, job_id: str) -> AsyncIterator[dict]:
        """Async iterator over SSE progress events.

        Same wire format as :meth:`EPWClient.stream` (``data: <json>\\n\\n``).
        Use::

            async for ev in client.stream(job_id):
                if ev.get("type") == "log":
                    print(ev["line"])
        """
        url = f"/jobs/{job_id}/stream"
        async with self._client.stream("GET", url, timeout=httpx.Timeout(
            connect=10.0, read=_POLL_TIMEOUT, write=10.0, pool=10.0,
        )) as r:
            self._raise(r)
            async for raw in r.aiter_lines():
                if not raw or not raw.startswith("data:"):
                    continue
                payload = raw[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    ev = {"type": "log", "line": payload}
                yield ev
                if ev.get("type") == "status" and ev.get("status") in (
                    "complete", "failed", "cancelled"
                ):
                    return

    # ── Status / account ─────────────────────────────────────────────────────

    async def status(self) -> dict:
        """API key status — credits remaining, tier, plan."""
        return await self._get("/keys/status")


__all__ = ["AsyncEPWClient"]
