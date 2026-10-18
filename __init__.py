"""Ponte de descoberta do IngeTrazo para o módulo principal do QTC."""
from __future__ import annotations

from pathlib import Path

from core.extensions import _import_by_path

_main = _import_by_path('igz_xref_manager_main', Path(__file__).with_name('QTC_Referencias_externas.py'))

# O motor descobre extensões por ``__init__.py``. Reexportar a configuração
# mantém essa convenção, enquanto a implementação reside no módulo QTC.
setup = _main.setup


class QTCReferenciasExternasTool(_main.QTCReferenciasExternasTool):
    """Ponte da ferramenta para a descoberta de extensões."""

    pass
