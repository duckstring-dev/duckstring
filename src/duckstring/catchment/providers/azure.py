"""Azure: ``az://`` and ``abfs[s]://`` data roots. Storage only until phase 3 (Container Apps jobs and VMs)."""

from __future__ import annotations

from . import Provider

AZURE = Provider(name="azure", title="Azure", storage_schemes=("az", "abfs", "abfss"), secret_prefixes=("AZURE_",))
