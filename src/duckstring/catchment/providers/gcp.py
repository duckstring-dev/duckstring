"""Google Cloud: ``gs://`` data roots. Storage only until phase 4 (Cloud Run jobs and VMs)."""

from __future__ import annotations

from . import Provider

GCP = Provider(name="gcp", title="Google Cloud", storage_schemes=("gs", "gcs"), secret_prefixes=("GOOGLE_",))
