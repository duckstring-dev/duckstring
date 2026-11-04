# Azure and Google Cloud

**Goal.** Everything a Catchment does on AWS today, on Azure and on Google Cloud: the data root, Spouts,
remote Ducks (container and VM), Pools, the Verify and sizing helpers, the UI and the docs. Storage for
both clouds lands first (phase 1, built 2026-11-04); compute follows one cloud at a time, Azure first.

**Why staggered.** The storage half is small and testable offline (Azurite, fake-gcs-server). The compute
half is mostly a provider seam pulled out of AWS-only code plus one launcher per cloud, and its long pole
is real-account validation: every AWS release smoke test has caught bugs, and the EC2/relay paths are
still unvalidated. One new cloud at a time keeps the unvalidated surface small. The seam is designed for
three providers from the start (phase 2), so Google compute is a check of the seam, not a second refactor.

## Where AWS lives today

| Concern | Code | AWS-specific? |
|---|---|---|
| Duck boot (artifact fetch, events, jobs) | `duck/__main__.py`, `duck/client.py` | No: all over the Catchment's HTTP |
| Launcher routing | `launcher.DispatchingLauncher` (`remotes = {provider: backend}`) | No |
| Data plane, fsspec half | `storage.ObjectStorage.fs` | No (fsspec), but option mapping was S3-shaped |
| Data plane, DuckDB half | `storage.ObjectStorage.duckdb_setup` | S3 and GCS-HMAC only; Azure returned early |
| Spouts to object stores | `egress/object_store.py` | `file`/`s3`/`gs` (HMAC only) |
| Cloud gate | `catchment/cloud.py` | Yes: remote root + AWS credentials, STS check |
| Backends | `cloud_backends.py`, `fargate_launcher.py`, `ec2_launcher.py`, `pool_launcher.py` (`FargatePoolMachine`/`Ec2PoolMachine`) | Yes |
| Deploy fields | `cloud_deploy.py` | Yes (Fargate/EC2 fields and env names) |
| Presets S/M/L/XL | `driver.PRESET_POOLS` | Yes (Fargate cpu/memory) |
| Verify, instance types | `routes/catchment.py` (STS, S3 probe, `describe_instance_types`) | Yes |
| Relay | `relay.py` | Yes (an EC2 box + `ssh -R`) |
| UI | `CloudMenu.tsx`, `DeployConfigForm.tsx`, `InstanceTypePicker.tsx`, `lib/aws.ts` | Yes |
| Image | `Dockerfile` (`[aws]` extra), ghcr | Partly (the extra) |
| Flock | MotherDuck, StarRocks, Athena | Only Athena |

## Phase 1: storage, both clouds (built 2026-11-04)

What was wrong before:

- An `abfss://` data root was accepted, and the formats reference said it was "configured through the
  environment", but `duckdb_setup` returned early for it: DuckDB never loaded `azure` or got a secret, so
  every `COPY`/`read_parquet` failed.
- `TYPE GCS, PROVIDER credential_chain` (what a `gs://` root without keys used) is DuckDB's **AWS** chain
  looking for HMAC keys in `AWS_ACCESS_KEY_ID`/`_SECRET_ACCESS_KEY` (measured: the secret carried an
  `AKIA…` key and the AWS region; DuckDB's docs say so). On a machine with AWS credentials it signed
  Google requests with them.
- `gcsfs` silently ignores the `key`/`secret` the fsspec half passed it and uses Google's default
  credentials, so a `gs://` root's two halves authenticated differently (HMAC or AWS for DuckDB, ADC for
  fsspec).
- Object-store Spouts resolved `${…}` references in the whole URI before parsing its query, so `parse_qs`
  turned a `+` in a credential into a space: any AWS secret key containing `+` failed to sign, and a
  service-account key's JSON could never have worked. Fixed by parsing first (as the data plane and DuckLake
  already did).
- The formats reference's S3 data-root example used `${secret:…}`, which a Duck can't resolve (it gets the
  URI unresolved and has no secret store). Data roots take `${env:}` only; the docs say so now.

What was built:

- **One secret builder** (`storage.duckdb_secret_sql(uri, params, name=)`) for Azure and GCS, used by the
  data plane (`duckstring_data_{hash of scope}`), object-store Spouts and DuckLake data paths
  (`__duckstring_egress`). S3 keeps its existing builders (unchanged behaviour). Azure and GCS secrets are
  `SCOPE`d to `{scheme}://{bucket or container}`, so a data root and a Spout in different accounts don't take
  each other's credentials, and the data plane's are named per scope, since a secret name is unique across
  types (a fixed name let the Flock's S3 staging secret replace a `gs://` root's on the same connection).
- **Azure** (`az://container/prefix`, `abfs[s]://container@account.dfs.core.windows.net/prefix`): the
  account comes from the URI, `?account_name=`, or `AZURE_STORAGE_ACCOUNT_NAME`. Credentials, first
  match: `connection_string` (or `AZURE_STORAGE_CONNECTION_STRING`; Azurite, SAS), `account_key` (or
  `AZURE_STORAGE_ACCOUNT_KEY`), `tenant_id`+`client_id`+`client_secret` (a service principal), else the
  Azure default chain (env, workload identity, managed identity, CLI), which DuckDB's `credential_chain`
  and adlfs's `DefaultAzureCredential` both run. The env names are adlfs's own, so both halves read the
  same ones. DuckDB 2.0 writes to both Blob and ADLS (`COPY … TO`, block uploads committed on close).
- **GCS** (`gs://bucket/prefix`): Google credentials everywhere. `?credentials=` is a service-account
  key (a file path, or the JSON itself through a reference: `${env:}` in a data root, `${secret:}` too in a
  Spout); `anon` for none; without it, application default
  credentials (`GOOGLE_APPLICATION_CREDENTIALS`, workload identity, the metadata server, `gcloud`). gcsfs
  takes them as `token=`; DuckDB gets an OAuth access token minted from the same credentials as a
  `BEARER_TOKEN` secret (measured: httpfs sends `Authorization: Bearer` on HEAD, GET, PUT and the
  multipart upload a large file uses; the XML API is S3's, so the same calls). HMAC
  `key_id`/`secret` still work for the DuckDB half (a snapshot Spout needs nothing else); the fsspec half
  (a data root, a mirror Spout) needs Google credentials regardless.
- **Token refresh.** An access token lives about an hour and DuckDB never refreshes a bearer secret. The
  token is cached per credential and re-minted when under 40 minutes remain, and the secret is rewritten
  by every `duckdb_setup` (which runs before every Source read, export and Spout delivery). Two
  long-lived connections also needed it: the Duck re-runs `duckdb_setup` on its registry before each
  Ripple (hydration leaves views over published files), and the full-access serving connection is rebuilt
  when the token is re-minted (`storage.credential_generation`). A single statement running more than 40
  minutes on a near-expiry token can still fail; Azure has no such limit (the SDK refreshes its own).
- **Spouts**: `az://`, `abfs://`, `abfss://` destinations (snapshot and mirror), `gs://` without HMAC,
  and DuckLake `data_path` on either cloud, with the Azure and GCS parameters.
- **Extras**: `duckstring[azure]` (adlfs, azure-identity) and `duckstring[gcp]` (gcsfs, google-auth), named
  for the cloud because phases 3 and 4 add the compute SDKs to them. A missing one fails with the install
  hint.
- **Not changed**: the cloud gate. `gs://` already counts as a remote root (so a `gs://` root + AWS
  credentials enables Fargate Ducks, cross-cloud by accident, removed in phase 2); `az://` deliberately
  doesn't, so an Azure root runs Catchment Ducks only until phase 3. The image keeps `[aws]` only.
- **Tests**: unit (secret SQL, scopes, quoting, option mapping, token refresh, serving rebuild, Spout and
  DuckLake parsing; offline) and integration against Azurite (`DUCKSTRING_TEST_AZURE`) and fake-gcs-server
  (`DUCKSTRING_TEST_GCS`), both CI jobs in `ci.yml`. The GCS integration test covers the fsspec half only:
  fake-gcs-server doesn't serve the XML API DuckDB uses. The DuckDB half is checked against moto with a GCS
  secret pointed at it (the bearer token travels and the XML protocol round-trips), which proves our
  wiring but not Google's server. Both need a real-account check before release (see Validation).

## Phase 2: the provider seam (AWS only, no behaviour change)

Pull the AWS knowledge behind one interface, `catchment/providers/{aws,azure,gcp}.py`, each a `Provider`:

| Member | AWS (today's code moved) | Azure | Google |
|---|---|---|---|
| `name`, `storage_schemes` | `aws`, `s3` | `azure`, `az`/`abfs`/`abfss` | `gcp`, `gs`/`gcs` |
| `credentials_present(secret_store)` | `aws_configured` | `DefaultAzureCredential` resolvable, or `AZURE_*` secrets | `google.auth.default()` resolvable, or `GOOGLE_*` secrets |
| `load_env(secret_store)` | `load_aws_env` (`AWS_*`) | `AZURE_*` | `GOOGLE_*` (a key JSON secret written to a 0600 file for `GOOGLE_APPLICATION_CREDENTIALS`) |
| `validate()` | STS `GetCallerIdentity` | a management-scope token + `GET` the resource group | token refresh + `projects.get` |
| `probe_root(uri)` | S3 put/delete | blob put/delete | object put/delete |
| `backends` | `fargate`, `ec2` | `containerapps`, `vm` | `cloudrun`, `gce` |
| `deploy_fields/env/required` | `cloud_deploy` lists | below | below |
| `presets` | Fargate S/M/L/XL | Container Apps sizes | Cloud Run sizes |
| `instance_types(region)` | `describe_instance_types` | Compute `resourceSkus` (VM sizes) | `machineTypes.list` |
| `relay` | `RelayManager` | none (needs a public URL) | none |

- **One cloud per Catchment** (decided 2026-11-04). The data root's scheme picks the Catchment's cloud
  (`s3` → AWS, so an S3-compatible store such as R2 counts as AWS; `gs` → Google; `az`/`abfs[s]` → Azure),
  and remote Ducks are only offered on that cloud: Ducks run next to their data. `cloud_enabled` = the data
  root's provider has credentials. Catchment Ducks (subprocesses on the Catchment's own machine) still read
  any data root, so a laptop with a bucket stays the onboarding path. A pipeline that spans clouds is a
  Catchment per cloud joined by Ducts, which move published output once per change and make the crossing
  visible; Spouts to another cloud stay allowed (moving data out is their job). This removes today's
  accident: a `gs://` root plus AWS credentials enables Fargate Ducks (which never worked: the image has no
  gcsfs). Enforced, not warned: loosening later breaks no one, tightening would.
- A pool's backend must be one of the Catchment's cloud's backends (`fargate`/`ec2` today; the column is
  `provider`, which names the backend, not the cloud). Refused at `duck pool add`; after a data-root switch to
  another cloud, a pool on the old cloud falls back to Catchment Ducks with a warning, as an undefined pool
  does now. Presets resolve against the Catchment's cloud, so `duck = "M"` means "medium on this cloud".
- `cloud_status` gains `provider` and per-backend readiness (`{backend}_enabled`), replacing
  `fargate_enabled`/`ec2_enabled` (keep those two keys for the UI until it's updated).
- Migration: none for data; the seam is code. Tests: the AWS suites unchanged, plus a provider registry test.

## Images, one per cloud (decided 2026-11-04)

A Catchment picks a cloud and is optimised for it; most users never use a second, so another cloud's
packages would be dead weight (measured installed: AWS 28 MB, Azure 10 MB, Google 58 MB, mostly gRPC; the
compute SDKs add more). So:

- One `Dockerfile` with `ARG EXTRAS` (`aws`, `azure` or `gcp`); `release.yml` builds each as a matrix entry
  (× amd64/arm64) from the same wheels.
- Tags `ghcr.io/duckstring-dev/duckstring:{version}-aws`, `-azure`, `-gcp`. The plain `:{version}` stays an
  alias of `-aws`, since existing Fargate configs and the AWS guide name it; drop it at a later major.
- Each provider's launcher defaults to its own tag (`FargateLauncher` to `-aws`, and so on); a configured
  image still wins. The build-your-own-image guidance names the build argument.
- The release smoke test runs per cloud image once phases 3 and 4 land (AWS only until then).
- Phase 2 ships the `ARG` and the `-aws` tag (no behaviour change); phases 3 and 4 add their images.

## Phase 3: Azure compute

- **Container backend: Container Apps jobs** (`azure-mgmt-appcontainers`). One manual-trigger job per pool
  (or per Catchment for presets), each Duck a job **execution** started with a template override (command
  = the Duck args, env, cpu/memory), mirroring Fargate's task-per-spawn with a task definition.
  Consumption profile: up to 4 vCPU / 8 GiB per execution; dedicated workload profiles (D/E series): up to
  32 vCPU / 256 GiB, at the cost of an environment with a dedicated profile (min nodes 0 scales to zero).
  `is_running` = execution status; `terminate` = stop execution; `diagnose` = Log Analytics query (or the
  console log stream). Identity: a user-assigned managed identity on the job, with Storage Blob Data
  Contributor on the data root's account (the Duck's `credential_chain` picks it up; check whether
  `AZURE_CLIENT_ID` must be set for a user-assigned identity, on both the Python and DuckDB C++ chains).
- **Not Container Instances** (decided 2026-11-04): no environment to create, but a 4 vCPU / 16 GB ceiling
  is too small for DuckDB work.
- **VM backend** (`azure-mgmt-compute`): the EC2 analogue for GPUs and very large boxes; custom data
  (cloud-init) carries the userdata script `Ec2Launcher` already builds; shutdown = deallocate + delete.
  `Ec2PoolMachine`'s twin for Pools.
- **Networking**: Ducks only dial out. Container Apps environments allow outbound by default; a private
  Catchment needs VNet integration. No relay in v1: a laptop Catchment needs `DUCKSTRING_CATCHMENT_PUBLIC_URL`.
- **Deploy fields**: subscription, resource group, location, environment (Container Apps), job name or
  image, managed identity id, workload profile; VM: image (a gallery image or marketplace URN), size,
  subnet, NSG, identity.
- **Image**: `:{version}-azure` from ghcr (Container Apps pulls from public registries).
- **Docs**: "Cloud compute on Azure" guide with the same hard-won-lessons shape as the AWS one.

## Phase 4: Google compute

- **Container backend: Cloud Run jobs** (`google-cloud-run`): an execution per Duck with container
  overrides (args, env), up to 8 vCPU / 32 GiB. Identity: the job's service account with
  `roles/storage.objectAdmin` on the bucket. `diagnose` = Cloud Logging tail. Image `:{version}-gcp`; Cloud
  Run pulls only from Artifact Registry (and Docker Hub), not ghcr: document an Artifact Registry remote
  repository proxying ghcr, or publish the `-gcp` image there too (verify current registry support first).
- **VM backend: Compute Engine** (`google-cloud-compute`) with a startup script, or **Batch**, which runs
  containers on arbitrary machine types and handles teardown; Batch is likely the less code. Decide when
  phase 4 starts.
- **Networking**: Cloud Run jobs egress to the internet by default; a private Catchment needs Direct VPC
  egress. No relay in v1.
- **Docs**: "Cloud compute on Google Cloud" guide.

## Phase 5 (deferred)

- Relay on Azure/GCP VMs, or a provider-neutral relay (any reachable box with sshd).
- A Flock engine for BigQuery or Fabric/Synapse SQL through the translator (SQL engines are measured per
  construct; a real conformance suite each). Not needed for parity: MotherDuck and StarRocks are cloud-neutral.
- Warm pools and autoscaling, as for AWS.

## Validation

Each phase ends with a real-account smoke test, written up like the AWS one (`reference_aws_smoke_test`):
phase 1: a Catchment with an `abfss://` and then a `gs://` data root (managed identity / workload identity,
no keys) running the Trickle demo with a mirror Spout and a DuckLake Spout on the same cloud; phase 3 and 4:
the release smoke test with Container Apps / Cloud Run Ducks. Each needs an account with a budget alarm;
the author's call when.

## Decisions (2026-11-04)

1. Azure's container backend is Container Apps jobs; Container Instances is too small.
2. One image per cloud (see Images).
3. One cloud per Catchment, enforced: remote Ducks run on the data root's cloud, and pipelines that span
   clouds use a Catchment per cloud joined by Ducts (see phase 2).
4. Duckstring stays generic: backends are chosen on general merit, with no particular user's setup in mind.

Still open, decided when the phase starts: Google's VM backend (Compute Engine or Batch), and where the `-gcp`
image is published for Cloud Run.
