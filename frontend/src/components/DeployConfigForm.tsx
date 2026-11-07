'use client';

import type { ComputeDefaults } from '@/lib/api';
import { FARGATE_CPU, FARGATE_MEMORY, cpuLabel, memoryLabel } from '@/lib/aws';
import { CLOUDRUN_CPU, CONSUMPTION_CPU, backendInfo, cloudrunMemory, consumptionMemory, deployConfigMissing } from '@/lib/clouds';
import { InstanceTypePicker } from './InstanceTypePicker';

export { deployConfigMissing } from '@/lib/clouds';

// The deployment fields a remote Duck needs (plans/compute-config-ui.md), per backend (lib/clouds.ts), shared
// by the Cloud-menu pool form and a Pond's Dedicated Duck. Fields already supplied by the Catchment env show a
// muted "env:…" placeholder and are optional; the rest are required to launch. `deployConfigMissing` mirrors
// the backend validation so the caller can gate its save button.

type Cfg = Record<string, string>;

export function DeployConfigForm({ provider, value, onChange, defaults }: {
  provider: string;
  value: Cfg;
  onChange: (v: Cfg) => void;
  defaults: ComputeDefaults | null;
}) {
  const env = defaults?.[provider]?.env ?? {};
  const set = (k: string, v: string) => onChange({ ...value, [k]: v });
  const missing = deployConfigMissing(provider, value, defaults);
  const input: React.CSSProperties = {
    width: '100%', boxSizing: 'border-box', background: '#1a1a1f', border: '1px solid #3f3f46',
    borderRadius: 4, color: '#e4e4e7', padding: '3px 6px', fontSize: 11, fontFamily: 'inherit',
  };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      {backendInfo(provider).fields.map((f) => {
        const fromEnv = env[f.key] != null;
        return (
          <div key={f.key} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <span style={{ fontSize: 10, color: '#a1a1aa' }}>
              {f.label}{f.required && <span style={{ color: '#f59e0b' }}> *</span>}
              {fromEnv && <span style={{ color: '#52525b' }}> · from env</span>}
            </span>
            <input
              value={value[f.key] ?? ''}
              onChange={(e) => set(f.key, e.target.value)}
              placeholder={fromEnv ? `env: ${env[f.key]}` : f.ph}
              style={input}
              autoComplete="off"
            />
          </div>
        );
      })}
      {missing.length > 0 && (
        <div style={{ fontSize: 10, color: '#ee9333', lineHeight: 1.4 }}>
          Required to launch: {missing.join(', ')} (set here, or the DUCKSTRING_{provider.toUpperCase()}_* env)
        </div>
      )}
    </div>
  );
}

// A backend's size: a Fargate cpu × memory pair, a Container Apps consumption size (memory follows the cpu), or
// a VM instance type (listed for the region, else typed). The region input is shown only where the backend
// takes it on the pool (EC2); an Azure VM's location is one of its deployment fields.
export function SizeFields({ provider, cpu, memory, onSize, instanceType, onInstanceType, region, onRegion,
                             location, style }: {
  provider: string;
  cpu: string; memory: string; onSize: (cpu: string, memory: string) => void;
  instanceType: string; onInstanceType: (v: string) => void;
  region: string; onRegion: (v: string) => void;
  location?: string;
  style: React.CSSProperties;
}) {
  const sizing = backendInfo(provider).sizing;
  if (sizing === 'fargate') {
    const pickCpu = (v: string) => {
      const mems = FARGATE_MEMORY[Number(v)] ?? [];
      onSize(v, mems.includes(Number(memory)) ? memory : String(mems[0] ?? ''));
    };
    return (
      <div style={{ display: 'flex', gap: 6 }}>
        <select value={cpu} onChange={(e) => pickCpu(e.target.value)} style={style}>
          {FARGATE_CPU.map((c) => <option key={c} value={c}>{cpuLabel(c)}</option>)}
        </select>
        <select value={memory} onChange={(e) => onSize(cpu, e.target.value)} style={style}>
          {(FARGATE_MEMORY[Number(cpu)] ?? []).map((m) => <option key={m} value={m}>{memoryLabel(m)}</option>)}
        </select>
      </div>
    );
  }
  if (sizing === 'cloudrun') {
    const pickCpu = (v: string) => {
      const mems = cloudrunMemory(Number(v));
      onSize(v, mems.includes(Number(memory)) ? memory : String(mems[mems.length > 1 ? 1 : 0]));
    };
    return (
      <div style={{ display: 'flex', gap: 6 }}>
        <select value={cpu} onChange={(e) => pickCpu(e.target.value)} style={style}>
          {CLOUDRUN_CPU.map((c) => <option key={c} value={c}>{c / 1024} vCPU</option>)}
        </select>
        <select value={memory} onChange={(e) => onSize(cpu, e.target.value)} style={style}>
          {cloudrunMemory(Number(cpu)).map((m) => <option key={m} value={m}>{m / 1024} GiB</option>)}
        </select>
      </div>
    );
  }
  if (sizing === 'consumption') {
    return (
      <select value={cpu} onChange={(e) => onSize(e.target.value, String(consumptionMemory(Number(e.target.value))))}
              style={style}>
        {CONSUMPTION_CPU.map((c) => (
          <option key={c} value={c}>{c / 1024} vCPU · {consumptionMemory(c) / 1024} GiB</option>
        ))}
      </select>
    );
  }
  const where = backendInfo(provider).location ? location : region;
  return (
    <>
      {!backendInfo(provider).location && (
        <input value={region} onChange={(e) => onRegion(e.target.value)} placeholder="region (blank = Catchment default)"
               style={style} />
      )}
      <InstanceTypePicker value={instanceType} onCommit={onInstanceType} region={where?.trim() || undefined} style={style} />
    </>
  );
}
