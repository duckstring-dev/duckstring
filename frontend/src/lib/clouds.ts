import type { CloudGate, ComputeDefaults } from './api';

// What the UI knows about each cloud and each Duck backend (plans/azure-gcp.md): the deployment fields a backend
// collects (mirroring its DeploySpec in catchment/providers/), how it's sized, and the credential secrets a cloud
// takes. The server stays the authority: the Catchment's cloud and its backends come from the cloud gate, and
// /catchment/compute-defaults says which fields the environment already supplies.

export type Field = { key: string; label: string; ph: string; required?: boolean };
// How a backend is sized: a Fargate task (cpu × memory matrix), a Container Apps consumption size (2 GiB per
// vCPU), a Cloud Run job (whole vCPUs, memory capped per vCPU), or a VM's instance type.
export type Sizing = 'fargate' | 'consumption' | 'cloudrun' | 'vm';

export interface BackendInfo {
  label: string;      // the option text in a backend picker
  sizing: Sizing;
  fields: Field[];
  oneOf?: string[][]; // groups of which at least one is required (reported under the first)
  location?: string;  // the deployment field naming the VM sizes' region, when it isn't a pool's region
}

export const BACKENDS: Record<string, BackendInfo> = {
  fargate: {
    label: 'fargate (serverless)', sizing: 'fargate', oneOf: [['image', 'task_definition']],
    fields: [
      { key: 'image', label: 'Container image', ph: '…dkr.ecr….amazonaws.com/duckstring:tag', required: true },
      { key: 'task_definition', label: 'or task-def ARN', ph: 'family:revision (skips image registration)' },
      { key: 'subnets', label: 'Subnets', ph: 'subnet-aaa,subnet-bbb', required: true },
      { key: 'security_groups', label: 'Security groups', ph: 'sg-aaa,sg-bbb' },
      { key: 'execution_role', label: 'Execution role', ph: 'arn:aws:iam::…:role/exec (image pull + logs)', required: true },
      { key: 'task_role', label: 'Task role', ph: 'arn:aws:iam::…:role/task (S3 access)', required: true },
      { key: 'cluster', label: 'Cluster', ph: 'default' },
      { key: 'assign_public_ip', label: 'Public IP', ph: 'ENABLED' },
      { key: 'cpu_arch', label: 'CPU arch', ph: 'X86_64' },
    ],
  },
  ec2: {
    label: 'ec2 (big / GPU)', sizing: 'vm',
    fields: [
      { key: 'ami', label: 'AMI', ph: 'ami-… (with duckstring installed)', required: true },
      { key: 'instance_profile', label: 'Instance profile', ph: 'duckstring-worker (IAM for S3)', required: true },
      { key: 'pip_spec', label: 'pip spec', ph: 'duckstring==x.y (boot override)' },
    ],
  },
  containerapps: {
    label: 'containerapps (serverless)', sizing: 'consumption',
    fields: [
      { key: 'environment', label: 'Environment', ph: '/subscriptions/…/managedEnvironments/ducks', required: true },
      { key: 'identity', label: 'Identity', ph: '/subscriptions/…/userAssignedIdentities/duck (blob access)', required: true },
      { key: 'image', label: 'Container image', ph: 'ghcr.io/duckstring-dev/duckstring:<version>-azure' },
      { key: 'workload_profile', label: 'Workload profile', ph: 'Consumption' },
      { key: 'job', label: 'Job name', ph: 'duckstring-duck-… (one per config)' },
    ],
  },
  azurevm: {
    label: 'azurevm (big / GPU)', sizing: 'vm', location: 'location',
    fields: [
      { key: 'subnet', label: 'Subnet', ph: '/subscriptions/…/virtualNetworks/…/subnets/ducks', required: true },
      { key: 'identity', label: 'Identity', ph: '/subscriptions/…/userAssignedIdentities/duck (blob access)', required: true },
      { key: 'location', label: 'Location', ph: 'australiaeast', required: true },
      { key: 'resource_group', label: 'Resource group', ph: "the subnet's" },
      { key: 'image', label: 'Image', ph: 'Canonical:ubuntu-24_04-lts:server:latest' },
      { key: 'assign_public_ip', label: 'Public IP', ph: 'true' },
      { key: 'pip_spec', label: 'pip spec', ph: 'duckstring[azure]==x.y (boot override)' },
    ],
  },
  cloudrun: {
    label: 'cloudrun (serverless)', sizing: 'cloudrun', oneOf: [['image', 'registry']],
    fields: [
      { key: 'region', label: 'Region', ph: 'australia-southeast1', required: true },
      { key: 'service_account', label: 'Service account', ph: 'duck@<project>.iam.gserviceaccount.com (bucket access)', required: true },
      { key: 'image', label: 'Container image', ph: '<region>-docker.pkg.dev/<project>/<repo>/duckstring:<version>-gcp', required: true },
      { key: 'registry', label: 'or registry', ph: '<region>-docker.pkg.dev/<project>/ghcr (proxying ghcr.io)' },
      { key: 'project', label: 'Project', ph: "the credentials' project" },
      { key: 'job', label: 'Job name', ph: 'duckstring-duck-… (one per config and size)' },
    ],
  },
  gce: {
    label: 'gce (big / GPU)', sizing: 'vm', location: 'zone',
    fields: [
      { key: 'zone', label: 'Zone', ph: 'australia-southeast1-a', required: true },
      { key: 'service_account', label: 'Service account', ph: 'duck@<project>.iam.gserviceaccount.com (bucket access)', required: true },
      { key: 'project', label: 'Project', ph: "the credentials' project" },
      { key: 'subnetwork', label: 'Subnetwork', ph: 'regions/<region>/subnetworks/<name> (blank = default network)' },
      { key: 'image', label: 'Image', ph: 'projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64' },
      { key: 'assign_public_ip', label: 'Public IP', ph: 'true' },
      { key: 'pip_spec', label: 'pip spec', ph: 'duckstring[gcp]==x.y (boot override)' },
    ],
  },
};

export function backendInfo(backend: string): BackendInfo {
  return BACKENDS[backend] ?? { label: backend, sizing: 'vm', fields: [] };
}

// The Catchment's backends, its default first (the server lists it first); AWS's on an older Catchment.
export function cloudBackends(cloud: CloudGate | null | undefined): string[] {
  const names = cloud?.backends ? Object.keys(cloud.backends) : [];
  return names.length ? names : ['fargate', 'ec2'];
}

// The required deployment fields still unsatisfied by input OR env: empty means ready to launch.
export function deployConfigMissing(backend: string, value: Record<string, string>,
                                    defaults: ComputeDefaults | null): string[] {
  const info = backendInfo(backend);
  const env = defaults?.[backend]?.env ?? {};
  const has = (k: string) => !!(value[k]?.trim() || env[k]);
  const inGroup = new Set((info.oneOf ?? []).flat());
  const miss = info.fields.filter((f) => f.required && !inGroup.has(f.key) && !has(f.key)).map((f) => f.key);
  for (const group of info.oneOf ?? []) if (!group.some(has)) miss.unshift(group[0]);
  return miss;
}

// Container Apps consumption sizes: cpu in 1024ths of a vCPU (as on every pool), memory fixed at 2 GiB per vCPU.
export const CONSUMPTION_CPU: number[] = [256, 512, 1024, 2048, 3072, 4096];
export const consumptionMemory = (cpu: number): number => cpu * 2;

// Cloud Run job sizes: whole vCPUs, and memory up to 4 GiB per vCPU (to 32 GiB at 8).
export const CLOUDRUN_CPU: number[] = [1024, 2048, 4096, 6144, 8192];
export function cloudrunMemory(cpu: number): number[] {
  const out: number[] = [];
  for (let m = 1024; m <= Math.min(cpu * 4, 32768); m += 1024) out.push(m);
  return out;
}

export interface CredentialField { secret: string; placeholder: string; password?: boolean; required?: boolean }

// The credential secrets the Cloud menu can store for a cloud (each loads into the Catchment's environment).
export const CREDENTIALS: Record<string, { title: string; fields: CredentialField[] }> = {
  aws: {
    title: 'AWS CREDENTIALS',
    fields: [
      { secret: 'AWS_ACCESS_KEY_ID', placeholder: 'AWS access key ID', required: true },
      { secret: 'AWS_SECRET_ACCESS_KEY', placeholder: 'AWS secret access key', password: true, required: true },
      { secret: 'AWS_DEFAULT_REGION', placeholder: 'region (e.g. us-east-1)' },
    ],
  },
  azure: {
    title: 'AZURE CREDENTIALS',
    fields: [
      { secret: 'AZURE_TENANT_ID', placeholder: 'tenant ID', required: true },
      { secret: 'AZURE_CLIENT_ID', placeholder: 'service principal client ID', required: true },
      { secret: 'AZURE_CLIENT_SECRET', placeholder: 'client secret', password: true, required: true },
      { secret: 'AZURE_SUBSCRIPTION_ID', placeholder: 'subscription ID (for VM sizes)' },
    ],
  },
  gcp: {
    title: 'GOOGLE CLOUD CREDENTIALS',
    fields: [
      { secret: 'GOOGLE_CREDENTIALS', placeholder: 'service-account key (paste the JSON)', password: true, required: true },
      { secret: 'GOOGLE_CLOUD_PROJECT', placeholder: 'project ID' },
    ],
  },
};
