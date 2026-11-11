"""KarenBot shared backend (control plane).

This package has no dependency on the browser frontend (``webui``) or the
native GUI (``gui``).  Both frontends talk to it over the same FastAPI
control API, so a feature cannot silently exist in one frontend only.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.1-alpha.1"
