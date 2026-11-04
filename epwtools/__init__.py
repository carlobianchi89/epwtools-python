"""epw.tools Python SDK — generate and analyse EPW weather files."""
__version__ = "0.5.2"

from .client import EPWClient, EPWToolsError  # noqa: F401

# AsyncEPWClient is an optional re-export gated on httpx availability.
# Importing the symbol unconditionally would force everyone to install
# httpx; instead we provide a lazy attribute that raises an informative
# error only when the user actually reaches for it.

def __getattr__(name):  # noqa: D401
    if name == "AsyncEPWClient":
        from .aio import AsyncEPWClient as _A
        return _A
    raise AttributeError(f"module 'epwtools' has no attribute {name!r}")
