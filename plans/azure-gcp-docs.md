# Azure and Google compute docs: handoff

Facts for writing "Cloud Compute on Azure" and "Cloud Compute on Google Cloud", gathered while building and testing
phases 3 and 4 (2026-11-07). This is a list of facts, not prose: write the guides fresh, following
`docs/documentation_style.md` and the shape of `docs/docs/guides/cloud_compute_on_aws.md`. A first draft in the
build session read as too technical and alien; aim for the AWS guide's register.

Each fact is marked **[live]** (seen working on a real account), **[code]** (from the code and unit tests only) or
**[unverified]**.

## Pages to change

- New: `docs/docs/guides/cloud_compute_on_azure.md`, `docs/docs/guides/cloud_compute_on_google_cloud.md`; add both to
  `docs/sidebars.ts` after `guides/cloud_compute_on_aws` (Operating Catchments).
- `cloud_compute_on_aws.md`: the checklist still says `cloud: enabled` (the output now says `compute: enabled`); the
  image section should say releases are tagged `<version>-aws` with the bare `<version>` kept as an alias.
- `docs/docs/reference/cli/duck.md` and the help strings in `src/duckstring/cli/duck.py`: `--provider` lists only
  fargate/ec2, `--cpu`/`--memory` say "Fargate", `--instance-type` says "EC2", `--region` says "AWS". Backends are now
  `fargate`, `ec2`, `containerapps`, `azurevm`, `cloudrun`, `gce`, `local`. Docstrings and the reference page change together.
- Check `running_on_a_server.md` and `hosting_on_a_platform.md` for AWS-only statements about cloud compute.

## Shared facts (both clouds)

- A Catchment runs remote Ducks only on its data root's cloud: `s3://` AWS, `az://`/`abfss://` Azure, `gs://` Google.
  A Catchment's own Ducks read any data root. **[code]** Pipelines spanning clouds are a Catchment per cloud joined by
  Ducts.
- `duckstring catchment settings` prints `data root`, `cloud`, `creds`, `compute` lines. **[live]**
- The same `pond.toml` works anywhere: a pool the Catchment doesn't have, or a pool on another cloud, runs on the
  Catchment's machine. Presets `S`/`M`/`L`/`XL` exist on every cloud with that cloud's sizes. **[code]**
- Pool sizes use the same units everywhere: `--cpu` in Fargate units (1024 = one vCPU), `--memory` in MiB. **[code]**
- Ducks only connect out to the Catchment. Neither Azure nor Google has a relay (AWS's laptop relay is AWS-only), so
  the Catchment needs an address Ducks can reach (`DUCKSTRING_CATCHMENT_PUBLIC_URL`). **[code]**
- Job backends create one job per configuration, named `duckstring-duck-<hash>`, tagged with the Catchment. When an
  upgrade brings a new image, the Catchment deletes its own jobs on the old image once nothing runs on them. Jobs an
  operator names (`job` field) are never deleted. **[code]**
- VM backends: one VM per Duck, Ubuntu 24.04 by default, boots by installing `duckstring[<cloud>]==<version>` from
  PyPI with uv (so VMs need internet access). When a Duck finishes, the Catchment deletes its VM and disk; its first VM
  launch after a restart deletes any of its VMs it finds stopped; on shutdown it deletes its VMs. **[live]** for deletion on Catchment
  shutdown (both clouds); **[code]** for deletion after a Duck exits on its own.
- Images: `ghcr.io/duckstring-dev/duckstring:<version>-azure` and `-gcp`, from the next release (none published yet).
  Each image carries its cloud's DuckDB extensions, so Ducks work without internet access to extensions.duckdb.org.
  **[code]** Build your own with `--build-arg EXTRAS=azure` or `gcp`.
- Cold start, measured: Container Apps about 22 to 37 s to a Duck's first Ripple, Cloud Run 11 to 15 s, a VM about 60
  to 70 s (boot plus install). A Pond with a recurring Refresh keeps its Duck warm. **[live]**
- A Catchment in a container (Container Apps, Cloud Run) loses its filesystem on restart: give it a state backup
  (`DUCKSTRING_STATE_BACKUP_URI`, see `hosting_on_a_platform.md`). **[code]** for the backup; not tested on Azure or
  Google.

## Azure

Turns on with an Azure data root plus Azure credentials: a managed identity, a service principal
(`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, as env vars or all three as secrets), workload identity,
or an `az login`. **[code]**; managed identity **[live]**.

Data root: an ADLS Gen2 account (hierarchical namespace on), e.g.
`abfss://lake@acmelake.dfs.core.windows.net/duckstring`; no credentials in the URI when using managed identities.
**[live]**

Identities (both user-assigned managed identities), exactly what worked **[live]**:
- Catchment: Contributor on the Ducks' resource group; Managed Identity Operator on the Duck identity; Storage Blob
  Data Contributor on the storage account; AcrPull on the registry if the image is there.
- Duck: Storage Blob Data Contributor on the storage account; AcrPull if the image is in ACR.
- Owner/Contributor don't grant blob data access; the data role is separate.
- With one user-assigned identity on a VM, nothing else is needed for it to be found. **[live]**

Subscription resource providers must be registered: `Microsoft.App`, `Microsoft.OperationalInsights` (Container Apps),
`Microsoft.Compute`, `Microsoft.Network` (VMs), `Microsoft.ContainerRegistry` (a registry). A principal scoped to a
resource group can't register them; someone with subscription rights must. **[live]**

Container Apps (`containerapps`, the default backend):
- Operator provides a Container Apps environment (consumption profile is enough; it has a Log Analytics workspace)
  and the Duck identity. **[live]**
- `DUCKSTRING_CONTAINERAPPS_ENVIRONMENT` and `DUCKSTRING_CONTAINERAPPS_IDENTITY` (resource ids) required; optional
  `_IMAGE` (default the release's `-azure` image), `_WORKLOAD_PROFILE` (default `Consumption`), `_JOB`. Subscription,
  resource group, location and log workspace come from the environment. **[live]**
- Presets: S 0.5 vCPU / 1 GiB, M 1 / 2, L 2 / 4, XL 4 / 8 (consumption allows 2 GiB per vCPU, at most 4 vCPU / 8 GiB).
  **[live]** for M. Larger sizes need a dedicated workload profile. **[unverified]**, not tested.
- Container Apps pulls public ghcr images directly; an ACR image is pulled with the Duck identity. **[live]** for ACR.
- ACR's cloud builds (`az acr build`) were blocked on the test subscription ("TasksOperationsNotAllowed"); building
  locally and pushing worked. Worth a short note. **[live]**

VMs (`azurevm`):
- `DUCKSTRING_AZUREVM_SUBNET` (resource id), `_IDENTITY`, `_LOCATION` required; optional `_RESOURCE_GROUP` (default the
  subnet's), `_IMAGE` (URN or image id), `_ASSIGN_PUBLIC_IP` (default true: new Azure subnets have no default outbound
  access), `_PIP_SPEC`, `_DISK_GB`. **[live]** with `Standard_D2s_v5`.
- An Azure VM whose OS has shut down is still allocated and billed, which is why the Catchment deletes it. **[code]**

Running the Catchment on Azure, as tested **[live]**: a Container App with one replica, external ingress on port 7474,
the Catchment identity, an API key as a Container Apps secret in `DUCKSTRING_API_KEY`,
`DUCKSTRING_CATCHMENT_PUBLIC_URL` set to its own `https://…azurecontainerapps.io` address, and the command
`duckstring catchment init --name X --host 0.0.0.0 --data-root … --yes --no-start && exec duckstring catchment start X`.

Debugging **[live]**:
- A Pond's failure message includes the execution status, its container events (image pull, exit code) and the end of
  its console log, from Log Analytics. Logs arrive minutes late.
- `az containerapp job logs show -n <job> -g <rg> --execution <execution> --container duck --follow` streams live.
- `az vm boot-diagnostics get-boot-log -g <rg> -n <vm>` shows a VM's console.

## Google Cloud

Turns on with a `gs://` data root plus Google credentials: `GOOGLE_APPLICATION_CREDENTIALS`, the metadata server (a
Catchment on Cloud Run or Compute Engine), `gcloud auth application-default login`, or a service-account key stored as
the `GOOGLE_CREDENTIALS` secret (with `GOOGLE_CLOUD_PROJECT`). The stored key is written to a private file and never
included in a Catchment download or state backup. **[code]**; metadata server **[live]**.

Service accounts, exactly what worked **[live]**:
- Catchment: `roles/run.admin`, `roles/compute.instanceAdmin.v1`, `roles/logging.viewer`,
  `roles/artifactregistry.reader`, `roles/storage.objectAdmin` on the bucket, and `roles/iam.serviceAccountUser` on
  the Duck service account. (Narrower roles may work; not tested.)
- Duck: `roles/storage.objectAdmin` on the bucket; `roles/run.invoker` on the Catchment's Cloud Run service if that is
  private (below).

Cloud Run (`cloudrun`, the default backend):
- `DUCKSTRING_CLOUDRUN_REGION` and `_SERVICE_ACCOUNT` (the Duck's email) required, plus `_IMAGE` or `_REGISTRY`;
  optional `_PROJECT` (default the credentials' project or `GOOGLE_CLOUD_PROJECT`), `_JOB`. **[live]**
- Cloud Run pulls only from Artifact Registry (or Docker Hub), not ghcr. Either push the image to Artifact Registry and
  set `_IMAGE`, or create a remote repository proxying ghcr and set `_REGISTRY` to it; the image is then
  `<registry>/duckstring-dev/duckstring:<version>-gcp`:
  `gcloud artifacts repositories create ghcr --repository-format=docker --location=<region> --mode=remote-repository --remote-docker-repo=https://ghcr.io`.
  **[live]**: Cloud Run pulled and started the published `0.7.1` image through such a repository.
- Cloud Run can't change one execution's CPU or memory, so there is one job per size. **[code]**
- Presets: S 1 vCPU / 2 GiB, M 1 / 4, L 2 / 8, XL 4 / 16 (Cloud Run's smallest whole vCPU for S). Up to 8 vCPU /
  32 GiB. **[live]** for M.

Compute Engine (`gce`):
- `DUCKSTRING_GCE_ZONE` and `_SERVICE_ACCOUNT` required; optional `_PROJECT`, `_IMAGE`, `_NETWORK` (default the
  project's `default` network), `_SUBNETWORK`, `_ASSIGN_PUBLIC_IP` (default true; the default network has no Cloud
  NAT), `_PIP_SPEC`, `_DISK_GB` (default 20). Default machine type `e2-standard-2`. **[live]**
- A stopped VM no longer bills its CPU but still bills its disk, which is why the Catchment deletes it. **[code]**

A private Catchment on Cloud Run **[live]**, the author's preferred setup: no public access to the service. Ducks and
Pool agents send their service account's Google identity token in `X-Serverless-Authorization` when the Catchment's URL
is a `*.run.app` address (or `DUCKSTRING_CATCHMENT_AUDIENCE` names the audience, for a custom domain); the Duck service
account needs `roles/run.invoker` on the service. From a laptop, connect with both the API key and an identity token,
which lasts an hour:
`duckstring catchment connect --name gcp --path <url> --key <key> --header "X-Serverless-Authorization: Bearer $(gcloud auth print-identity-token --audiences=<url>)"`.

Running the Catchment on Cloud Run, as tested **[live]**: one instance (min and max 1), CPU always allocated
(`run.googleapis.com/cpu-throttling: false`, the scheduler runs between requests), port 7474, the Catchment service
account, 4 GiB (the filesystem is held in memory), the same `init … && exec … start` command as on Azure,
`DUCKSTRING_CATCHMENT_PUBLIC_URL` set to its `https://<service>-<project number>.<region>.run.app` address. Deleting
the service stopped the Catchment cleanly within Cloud Run's 10 s shutdown window, deleting its VMs.

Debugging **[live]**:
- A Pond's failure message includes the execution's state and the end of its Cloud Logging entries.
- `gcloud compute instances get-serial-port-output <vm> --zone <zone>` shows a VM's console.
