"""AWS: Fargate and EC2 Ducks, S3 data roots, credentials from the botocore chain or ``AWS_*`` secrets."""

from __future__ import annotations

import functools
import os

from . import DeploySpec, Provider

SECRET_PREFIX = "AWS_"

# Fargate and EC2 deployment config (plans/compute-config-ui.md). cluster defaults to "default" and
# security_groups is recommended, not required. Fargate needs an image or a task definition.
FARGATE = DeploySpec(
    fields=("image", "task_definition", "cluster", "subnets", "security_groups",
            "execution_role", "task_role", "assign_public_ip", "cpu_arch"),
    env={
        "image": "DUCKSTRING_FARGATE_IMAGE", "task_definition": "DUCKSTRING_FARGATE_TASK_DEF",
        "cluster": "DUCKSTRING_FARGATE_CLUSTER", "subnets": "DUCKSTRING_FARGATE_SUBNETS",
        "security_groups": "DUCKSTRING_FARGATE_SECURITY_GROUPS",
        "execution_role": "DUCKSTRING_FARGATE_EXECUTION_ROLE", "task_role": "DUCKSTRING_FARGATE_TASK_ROLE",
        "assign_public_ip": "DUCKSTRING_FARGATE_ASSIGN_PUBLIC_IP", "cpu_arch": "DUCKSTRING_FARGATE_CPU_ARCH",
    },
    required=("subnets", "execution_role", "task_role"),
    one_of=(("image", "task_definition"),),
)
EC2 = DeploySpec(
    fields=("ami", "instance_profile", "pip_spec", "subnet", "security_groups", "assign_public_ip"),
    env={
        "ami": "DUCKSTRING_EC2_AMI", "instance_profile": "DUCKSTRING_EC2_INSTANCE_PROFILE",
        "pip_spec": "DUCKSTRING_EC2_PIP_SPEC", "subnet": "DUCKSTRING_EC2_SUBNET",
        "security_groups": "DUCKSTRING_EC2_SECURITY_GROUPS",
        "assign_public_ip": "DUCKSTRING_EC2_ASSIGN_PUBLIC_IP",
    },
    required=("ami", "instance_profile"),
)

# The built-in presets as Fargate task sizes (plans/cloud-config.md §4b): always available, so a
# `pond.toml duck = "M"` works with no pool setup, and the hosted product ships the same names.
PRESETS = {
    "S": {"provider": "fargate", "cpu": 512, "memory": 2048},
    "M": {"provider": "fargate", "cpu": 1024, "memory": 4096},
    "L": {"provider": "fargate", "cpu": 2048, "memory": 8192},
    "XL": {"provider": "fargate", "cpu": 4096, "memory": 16384},
}

_INSTANCE_TYPES: dict[str, list[dict]] = {}  # region → sorted spec list; process-lifetime cache


@functools.lru_cache(maxsize=1)
def _chain_has_credentials() -> bool:
    try:
        import boto3
        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def _region_from_env() -> str | None:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def _aws_error(exc: Exception) -> str:
    """A short, non-secret message for the UI. boto3 error strings carry the operation + error code,
    not credentials, but cap the length so a stray verbose message can't dominate the panel."""
    msg = str(exc) or exc.__class__.__name__
    return msg if len(msg) <= 300 else msg[:297] + "..."


def _describe_instance_types(client) -> list[dict]:
    """Every current-generation EC2 instance type in the client's region, with vCPU / memory / GPU:
    enough to render an informative, sortable dropdown."""
    out: list[dict] = []
    paginator = client.get_paginator("describe_instance_types")
    for page in paginator.paginate(Filters=[{"Name": "current-generation", "Values": ["true"]}]):
        for it in page.get("InstanceTypes", []):
            gpus = sum(g.get("Count", 0) for g in it.get("GpuInfo", {}).get("Gpus", []))
            out.append({
                "name": it["InstanceType"],
                "vcpu": it.get("VCpuInfo", {}).get("DefaultVCpus"),
                "memory_gib": round(it.get("MemoryInfo", {}).get("SizeInMiB", 0) / 1024, 1),
                "gpu": gpus,
            })
    out.sort(key=lambda t: (t.get("vcpu") or 0, t.get("memory_gib") or 0, t["name"]))
    return out


def _probe_s3_writable(boto3, uri: str, region: str | None) -> None:
    """Put and delete a tiny probe object under the data-root prefix: proves write access without
    leaving anything behind. Raises the boto3 error on failure."""
    rest = uri.split("://", 1)[1]
    bucket, _, prefix = rest.partition("/")
    key = f"{prefix.rstrip('/')}/.duckstring-probe" if prefix else ".duckstring-probe"
    s3 = boto3.client("s3", **({"region_name": region} if region else {}))
    s3.put_object(Bucket=bucket, Key=key, Body=b"duckstring")
    s3.delete_object(Bucket=bucket, Key=key)


class AwsProvider(Provider):
    def credentials_present(self, secret_store=None) -> bool:
        """AWS credentials the control plane can use to launch/submit: env creds/profile/role, or an
        ``AWS_*`` secret in the write-only store (worker boxes themselves use instance roles; creds here
        are the control-plane half)."""
        if any(os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_ROLE_ARN",
                                           "AWS_WEB_IDENTITY_TOKEN_FILE")):
            return True
        if secret_store is not None:
            try:
                if any(n["name"].startswith(SECRET_PREFIX) for n in secret_store.names()):
                    return True
            except Exception:
                pass
        # Fall back to the botocore credential chain (a `[default]` profile, SSO, an instance role) so a
        # plain `aws configure` enables cloud, not only explicit env vars. Cached (process-lifetime).
        return _chain_has_credentials()

    def load_env(self, secret_store=None) -> None:
        """Inject any ``AWS_*`` secrets into the process environment so botocore's default credential chain
        actually picks them up. ``credentials_present`` already *counts* an ``AWS_*`` secret as creds (the
        gate); this gives that its teeth: every boto3 client is built off the ambient chain, so without this
        a secret-stored key would flip the gate green but real launches would fail with no ambient creds.

        Real environment variables win, so an operator's explicit env is never overridden. Called at startup
        and after a matching secret is set (see routes/secrets)."""
        if secret_store is None:
            return
        try:
            names = [n["name"] for n in secret_store.names() if n["name"].startswith(SECRET_PREFIX)]
        except Exception:
            return
        changed = False
        for name in names:
            if name not in os.environ:
                value = secret_store.get(name)
                if value is not None:
                    os.environ[name] = value
                    changed = True
        if changed:
            _chain_has_credentials.cache_clear()

    def credentials_changed(self) -> None:
        _chain_has_credentials.cache_clear()

    def validate(self) -> str | None:
        """STS ``GetCallerIdentity``. A region is required only to sign the request."""
        region = _region_from_env() or "us-east-1"
        try:
            import boto3
            boto3.client("sts", region_name=region).get_caller_identity()
            return None
        except Exception as exc:
            return str(exc) or exc.__class__.__name__

    def verify(self, data_root: str | None) -> dict:
        """STS ``GetCallerIdentity`` for the identity, and an S3 probe object when ``data_root`` is S3."""
        region = _region_from_env()
        signing = region or "us-east-1"  # STS/S3 clients need a region to sign; the identity is global
        try:
            import boto3
        except Exception:
            return {"ok": False, "error": "boto3 is not installed on the Catchment"}
        try:
            ident = boto3.client("sts", region_name=signing).get_caller_identity()
        except Exception as exc:
            return {"ok": False, "region": region, "error": _aws_error(exc)}
        result = {"ok": True, "account": ident.get("Account"), "arn": ident.get("Arn"), "region": region}
        root = (data_root or "").strip()
        if root and root.lower().startswith("s3://"):
            try:
                _probe_s3_writable(boto3, root, signing)
                result["bucket_ok"] = True
            except Exception as exc:
                result["bucket_ok"] = False
                result["bucket_error"] = _aws_error(exc)
        return result

    def instance_types(self, region: str | None) -> dict:
        """Current-generation EC2 instance types, cached per region for the process lifetime."""
        region = (region or "").strip() or _region_from_env()
        if not region:
            return {"available": False, "error": "no region: pass ?region= or set AWS_DEFAULT_REGION", "types": []}
        if region in _INSTANCE_TYPES:
            return {"available": True, "region": region, "types": _INSTANCE_TYPES[region]}
        try:
            import boto3  # lazy: only a cloud-enabled Catchment ever needs it
            client = boto3.client("ec2", region_name=region)
            types = _describe_instance_types(client)
        except Exception as exc:
            return {"available": False, "region": region, "error": _aws_error(exc), "types": []}
        _INSTANCE_TYPES[region] = types
        return {"available": True, "region": region, "types": types}

    def relay(self, base_url: str | None):
        """The auto-relay (an EC2 box and an ``ssh -R`` tunnel) for a Catchment behind NAT, when configured."""
        from ..relay import RelayManager, needs_relay

        if not needs_relay(base_url):
            return None
        candidate = RelayManager(bind_url=base_url)
        return candidate if candidate.configured() else None

    def build_backends(self, root, base_url: str | None, *, token: str, data_root: str | None, dialback) -> dict:
        from ..ec2_launcher import Ec2Launcher
        from ..fargate_launcher import FargateLauncher

        kw = dict(token=token, data_root=data_root, dialback=dialback)
        return {"fargate": FargateLauncher(root, base_url, **kw), "ec2": Ec2Launcher(root, base_url, **kw)}

    def pool_machine(self, backend: str, pool: str, spec: dict, remote):
        from ..pool_launcher import Ec2PoolMachine, FargatePoolMachine

        if backend == "ec2":
            return Ec2PoolMachine(pool, spec, remote)
        return FargatePoolMachine(pool, spec, remote)


AWS = AwsProvider(
    name="aws", title="AWS", storage_schemes=("s3", "s3a"), secret_prefixes=(SECRET_PREFIX,),
    backends={"fargate": FARGATE, "ec2": EC2}, default_backend="fargate", presets=PRESETS,
)
