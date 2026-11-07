'use client';

import { useEffect, useRef, useState } from 'react';
import {
  addDuckPool, fetchCloudSettings, fetchComputeDefaults, fetchDuckPools, fetchMigration, removeDuckPool,
  setDataRoot, setSecret, verifyCloud, type CloudSettings, type CloudVerifyResult, type ComputeDefaults,
  type DuckPool, type MigrationStatus,
} from '@/lib/api';
import { defaultMemoryFor } from '@/lib/aws';
import { CREDENTIALS, backendInfo, cloudBackends, consumptionMemory } from '@/lib/clouds';
import { useLiveStore } from '@/lib/store';
import { DeployConfigForm, SizeFields, deployConfigMissing } from './DeployConfigForm';

// Catchment-level cloud config (plans/cloud-config.md + plans/cloud-menu-redesign.md, full access only).
// A modal with one section per concern: Status, Data plane, credentials, Duck pools, each following the
// Catchment's cloud (its data root's: lib/clouds.ts). Cloud enabling is EMERGENT (a data root on a cloud + valid
// credentials for it → the gate flips), not a wizard. Reverting to local is
// always available (no creds, no target). The backend surface is unchanged.

const input: React.CSSProperties = {
  width: '100%', boxSizing: 'border-box', background: '#1a1a1f', border: '1px solid #3f3f46',
  borderRadius: 4, color: '#e4e4e7', padding: '5px 8px', fontSize: 12,
};
const smallInput: React.CSSProperties = { ...input, fontSize: 11, padding: '4px 7px' };
const btn = (color: string, disabled: boolean): React.CSSProperties => ({
  background: 'transparent', border: `1px solid ${color}`, color, borderRadius: 5, padding: '5px 12px',
  fontSize: 12, cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.5 : 1, fontWeight: 600,
  fontFamily: 'inherit',
});
const heading: React.CSSProperties = { fontSize: 10, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.08em' };
const err = (m: string) => <div style={{ fontSize: 11, color: '#ef4444', wordBreak: 'break-word' }}>{m}</div>;

// A provider-readiness pill — green when the provider's deployment config is adequate to launch.
export function StateChip({ label, on }: { label: string; on: boolean }) {
  return (
    <span title={on ? `${label} ready` : `${label} not configured`} style={{
      fontSize: 9, fontWeight: 700, letterSpacing: '0.04em', padding: '1px 5px', borderRadius: 4,
      border: `1px solid ${on ? '#22c55e66' : '#3f3f46'}`, color: on ? '#22c55e' : '#52525b',
    }}>{on ? '✓ ' : ''}{label}</span>
  );
}

const modalBackdrop: React.CSSProperties = {
  position: 'fixed', inset: 0, zIndex: 1100, display: 'flex', alignItems: 'center', justifyContent: 'center',
  background: 'rgba(9,9,11,0.78)', backdropFilter: 'blur(2px)', fontFamily: 'ui-monospace, SFMono-Regular, monospace',
};

function Section({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div style={{ border: '1px solid #27272a', borderRadius: 8, padding: '11px 13px', marginBottom: 10, background: '#141418' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 9 }}>
        <span style={heading}>{title}</span>
        {right}
      </div>
      {children}
    </div>
  );
}

// A data root must be an object URI (s3://…) or an absolute path — a bare name would resolve to a LOCAL
// relative folder (the backend rejects it; this soft-warns in the form first).
const looksBareRoot = (v: string) => {
  const t = v.trim();
  return t !== '' && !/^[a-z0-9]+:\/\//i.test(t) && !t.startsWith('/') && !t.startsWith('~');
};
const bareRootHint = (v: string) =>
  looksBareRoot(v) ? (
    <div style={{ fontSize: 10, color: '#ee9333', lineHeight: 1.4 }}>
      Add <b>s3://</b> for a bucket, or use an absolute path — a bare name becomes a local folder.
    </div>
  ) : null;

function fmtBytes(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)} KB`;
  return `${n} B`;
}

// ─── Migration progress ──────────────────────────────────────────────────────

function MigrationBanner({ onDone }: { onDone: () => void }) {
  const [mig, setMig] = useState<MigrationStatus | null>(null);
  const doneHandled = useRef(false);

  useEffect(() => {
    let live = true;
    const tick = () => void fetchMigration().then((m) => { if (live) setMig(m); }).catch(() => {});
    tick();
    const id = setInterval(tick, 1000);
    return () => { live = false; clearInterval(id); };
  }, []);

  const status = mig?.status;
  useEffect(() => {
    if (status === 'done' && !doneHandled.current) {
      doneHandled.current = true;
      const t = setTimeout(onDone, 1500);
      return () => clearTimeout(t);
    }
  }, [status, onDone]);

  if (!mig || mig.status === 'idle') return null;

  const total = mig.total_bytes ?? 0;
  const copied = mig.copied_bytes ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round((copied / total) * 100)) : (status === 'copying' ? 0 : 100);
  const color = status === 'failed' ? '#ef4444' : status === 'done' ? '#22c55e' : '#06c4e6';
  return (
    <div style={{ marginBottom: 10, border: `1px solid ${color}55`, background: `${color}12`, borderRadius: 6, padding: '7px 9px' }}>
      <div style={{ fontSize: 11, color, fontWeight: 700, marginBottom: 3 }}>
        {status === 'copying' && `Migrating → ${mig.target || 'local'}`}
        {status === 'adopting' && 'Adopting migrated data…'}
        {status === 'done' && 'Migration complete ✓'}
        {status === 'failed' && 'Migration failed'}
      </div>
      {(status === 'copying' || status === 'adopting') && (
        <>
          <div style={{ height: 5, background: '#27272a', borderRadius: 3, overflow: 'hidden' }}>
            <div style={{ width: `${pct}%`, height: '100%', background: color, transition: 'width 0.4s' }} />
          </div>
          <div style={{ fontSize: 10, color: '#a1a1aa', marginTop: 3 }}>
            {mig.copied_files ?? 0}/{mig.total_files ?? 0} files · {fmtBytes(copied)}{total > 0 ? ` / ${fmtBytes(total)}` : ''}
            {mig.pond ? ` · ${mig.pond}` : ''}
          </div>
        </>
      )}
      {status === 'failed' && err(mig.error ?? 'failed')}
    </div>
  );
}

// ─── The empty/adopt/migrate choice ──────────────────────────────────────────

type SwitchMode = 'empty' | 'adopt' | 'migrate';

function SwitchModeChoice({ mode, setMode }: { mode: SwitchMode; setMode: (m: SwitchMode) => void }) {
  const chip = (m: SwitchMode, label: string, hint: string) => (
    <button key={m} onClick={() => setMode(m)} title={hint} style={{
      flex: 1, background: 'transparent', border: `1px solid ${mode === m ? '#f59e0b' : '#3f3f46'}`,
      color: mode === m ? '#f59e0b' : '#a1a1aa', borderRadius: 5, padding: '4px 5px', fontSize: 10,
      cursor: 'pointer', fontFamily: 'inherit',
    }}>{label}</button>
  );
  const blurb = mode === 'empty'
    ? <>Empties the data plane — every Pond is left with no data and idle (no auto-rebuild). Refresh to rebuild, or hand-copy data across first.</>
    : mode === 'adopt'
      ? <>Picks up data already in the target (you copied it in, or a plane you&apos;re returning to) and resumes with no rebuild. Use only if the target already holds this Catchment&apos;s data.</>
      : <>Copies the current data to the target (server-side where possible), then resumes with no rebuild. May take a while for large data.</>;
  return (
    <>
      <div style={{ display: 'flex', gap: 4 }}>
        {chip('empty', 'Empty + reset', 'Leave every Pond with no data and idle (no auto-rebuild).')}
        {chip('adopt', 'Adopt existing', 'The target already holds this Catchment’s data — pick it up and resume, no rebuild.')}
        {chip('migrate', 'Migrate (copy)', 'Copy the current data to the target (server-side where possible), then resume — no rebuild.')}
      </div>
      <div style={{ fontSize: 10, color: '#f59e0b', lineHeight: 1.4 }}>{blurb} The current location is kept intact as a backup.</div>
    </>
  );
}

// ─── Data plane ──────────────────────────────────────────────────────────────

function DataPlanePanel({ settings, catchmentName, reload }:
  { settings: CloudSettings; catchmentName: string; reload: () => void }) {
  const [target, setTarget] = useState('');
  const [confirm, setConfirm] = useState('');
  const [mode, setMode] = useState<SwitchMode>('migrate');  // changing to a new plane usually means carry the data
  const [busy, setBusy] = useState(false);
  const [e, setE] = useState<string | null>(null);

  const confirmed = confirm.trim() === catchmentName && catchmentName !== '';
  const validTarget = target.trim() !== '' && !looksBareRoot(target);

  const applyBucket = async () => {
    setE(null); setBusy(true);
    try {
      await setDataRoot(target.trim(), confirm.trim(), mode);
      setTarget(''); setConfirm(''); reload();
    } catch (ex) { setE(ex instanceof Error ? ex.message : 'failed'); } finally { setBusy(false); }
  };
  const revertLocal = async () => {
    setE(null); setBusy(true);
    try {
      await setDataRoot('', undefined, 'adopt');  // no confirm / creds needed; adopt restores local data
      setTarget(''); setConfirm(''); reload();
    } catch (ex) { setE(ex instanceof Error ? ex.message : 'failed'); } finally { setBusy(false); }
  };

  const onLocal = !settings.data_root;
  return (
    <Section title="DATA PLANE">
      <div style={{ fontSize: 11, color: '#a1a1aa', marginBottom: 9 }}>
        current: <span style={{ color: '#e4e4e7' }}>{settings.data_root || '(local disk)'}</span>
        {onLocal ? '' : <span style={{ color: '#52525b' }}> · {settings.data_root_remote ? 'object store' : 'local path'}</span>}
      </div>

      {/* Change to a bucket / absolute path. The mode + confirm + apply appear only once a valid target
          is entered (a bare name is not a valid data root). */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div style={{ fontSize: 10, color: '#71717a' }}>Point the data plane at a new location:</div>
        <input value={target} onChange={(ev) => setTarget(ev.target.value)} placeholder="s3://bucket/prefix" style={smallInput} />
        {bareRootHint(target)}
        {validTarget && (
          <>
            <SwitchModeChoice mode={mode} setMode={setMode} />
            <input value={confirm} onChange={(ev) => setConfirm(ev.target.value)} placeholder={`type ${catchmentName} to confirm`} style={smallInput} />
            <button onClick={applyBucket} disabled={busy || !confirmed} style={btn('#f59e0b', busy || !confirmed)}>
              {busy ? 'Working…' : 'Apply'}
            </button>
          </>
        )}
        {e && err(e)}
      </div>

      {/* The always-available escape hatch — no creds, no target, no confirm. */}
      {!onLocal && (
        <div style={{ marginTop: 10, paddingTop: 9, borderTop: '1px solid #27272a' }}>
          <button onClick={revertLocal} disabled={busy} style={btn('#a1a1aa', busy)}
                  title="Point the data plane back at local disk and pick up your local data. No credentials needed.">
            Revert to local disk
          </button>
          <div style={{ fontSize: 10, color: '#52525b', marginTop: 4, lineHeight: 1.4 }}>
            Always available — needs no credentials. Adopts your local data; the current location is kept.
          </div>
        </div>
      )}
    </Section>
  );
}

// ─── Credentials (the Catchment's cloud's) ───────────────────────────────────

function CredentialsPanel({ settings, reload }: { settings: CloudSettings; reload: () => void }) {
  const cloud = CREDENTIALS[settings.provider ?? 'aws'] ?? CREDENTIALS.aws;
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CloudVerifyResult | null>(null);
  const [e, setE] = useState<string | null>(null);
  const configured = settings.credentials_configured ?? settings.aws_configured;

  const run = async (withSave: boolean) => {
    setE(null); setResult(null); setBusy(true);
    try {
      if (withSave) {
        for (const f of cloud.fields) if (values[f.secret]?.trim()) await setSecret(f.secret, values[f.secret].trim());
      }
      const res = await verifyCloud();
      setResult(res);
      if (res.ok) setValues({});
      reload();
    } catch (ex) { setE(ex instanceof Error ? ex.message : 'failed'); } finally { setBusy(false); }
  };

  const badge = configured
    ? (settings.creds_valid === false
        ? <span style={{ fontSize: 10, color: '#ef4444' }}>configured · failing ✗</span>
        : settings.creds_valid
          ? <span style={{ fontSize: 10, color: '#22c55e' }}>configured · valid ✓</span>
          : <span style={{ fontSize: 10, color: '#a1a1aa' }}>configured</span>)
    : <span style={{ fontSize: 10, color: '#71717a' }}>not set</span>;

  const canSave = !busy && cloud.fields.every((f) => !f.required || !!values[f.secret]?.trim());
  return (
    <Section title={cloud.title} right={badge}>
      {settings.creds_valid === false && (
        <div style={{ fontSize: 11, color: '#f59e0b', marginBottom: 7 }}>
          ⚠ The stored credentials are not authenticating{settings.creds_error ? <span style={{ color: '#a1a1aa' }}> ({settings.creds_error})</span> : null}. Remote Duck launches will fail — update them below.
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {cloud.fields.map((f) => (
          <input key={f.secret} value={values[f.secret] ?? ''} placeholder={f.placeholder} style={smallInput}
                 type={f.password ? 'password' : undefined} autoComplete="off"
                 onChange={(ev) => setValues({ ...values, [f.secret]: ev.target.value })} />
        ))}
        {result && (result.ok
          ? <div style={{ fontSize: 10, color: '#22c55e', wordBreak: 'break-word' }}>authenticated ✓ {result.arn}{result.account ? ` · account ${result.account}` : ''}</div>
          : err(`verification failed: ${result.error}`))}
        {e && err(e)}
        <div style={{ display: 'flex', gap: 6 }}>
          <button onClick={() => run(true)} disabled={!canSave} style={btn('#f59e0b', !canSave)}>{busy ? 'Verifying…' : 'Save & verify'}</button>
          <button onClick={() => run(false)} disabled={busy || !configured} style={btn('#a1a1aa', busy || !configured)}
                  title="Re-check the stored credentials">Verify</button>
        </div>
        <div style={{ fontSize: 10, color: '#52525b', lineHeight: 1.4 }}>Sent over the wire — use HTTPS. Stored write-only.</div>
      </div>
    </Section>
  );
}

// ─── Duck pools ──────────────────────────────────────────────────────────────

function PoolsPanel({ pools, reload, defaults, backends, presetsReady }:
  { pools: DuckPool[]; reload: () => void; defaults: ComputeDefaults | null; backends: string[]; presetsReady: boolean }) {
  const [busy, setBusy] = useState(false);
  const [pName, setPName] = useState('');
  const [pProvider, setPProvider] = useState(backends[0]);
  const [pType, setPType] = useState('');
  const [pRegion, setPRegion] = useState('');
  const [pCpu, setPCpu] = useState('1024');
  const [pMem, setPMem] = useState(String(defaultMemoryFor(1024)));
  const [deploy, setDeploy] = useState<Record<string, string>>({});
  const [pErr, setPErr] = useState<string | null>(null);
  const sizing = backendInfo(pProvider).sizing;
  const presetBackend = backendInfo(backends[0]).label.split(' ')[0];

  const pickBackend = (b: string) => {
    setPProvider(b); setDeploy({});
    if (backendInfo(b).sizing === 'consumption') setPMem(String(consumptionMemory(Number(pCpu))));
  };

  const addPool = async () => {
    setPErr(null); setBusy(true);
    try {
      await addDuckPool({
        name: pName.trim(),
        provider: pProvider,
        instance_type: sizing === 'vm' ? pType.trim() || null : null,
        cpu: sizing !== 'vm' && pCpu ? Number(pCpu) : null,
        memory: sizing !== 'vm' && pMem ? Number(pMem) : null,
        region: sizing === 'vm' && pRegion.trim() ? pRegion.trim() : null,
        deploy_config: deploy,
      });
      setPName(''); setPType(''); setPRegion(''); setDeploy({}); reload();
    } catch (e) { setPErr(e instanceof Error ? e.message : 'failed'); } finally { setBusy(false); }
  };
  const deployMissing = deployConfigMissing(pProvider, deploy, defaults);

  return (
    <Section title="DUCK POOLS">
      {!presetsReady && (
        <div style={{ fontSize: 10, color: '#71717a', marginBottom: 8, lineHeight: 1.4 }}>
          The S/M/L/XL presets are unavailable until {presetBackend} is enabled — add its deployment config via
          the env or a pool below.
        </div>
      )}
      {pools.map((p) => {
        const backend = p.provider || backends[0];
        const spec = backendInfo(backend).sizing === 'vm'
          ? (p.instance_type || '—')
          : (p.cpu || p.memory ? `${p.cpu ?? '?'}cpu / ${p.memory ?? '?'}MiB` : '—');
        const unavailable = p.managed && !presetsReady;  // a preset with no backend env behind it
        return (
          <div key={p.name} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4, opacity: unavailable ? 0.5 : 1 }}>
            <span style={{ fontSize: 12, color: '#a1a1aa' }}>
              {p.name}
              <span style={{ color: '#52525b', fontSize: 10 }}> [{backend}] {spec}{p.managed ? (unavailable ? ` · preset · needs ${presetBackend}` : ' · preset') : ''}</span>
            </span>
            {!p.managed && (
              <span role="button" title="Remove" onClick={() => removeDuckPool(p.name).then(reload).catch(() => undefined)}
                    style={{ cursor: 'pointer', color: '#52525b', fontSize: 13, lineHeight: 1 }}>✕</span>
            )}
          </div>
        );
      })}
      <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
        <input value={pName} onChange={(e) => setPName(e.target.value)} placeholder="pool name (e.g. heavy)" style={smallInput} />
        <select value={pProvider} onChange={(e) => pickBackend(e.target.value)} style={smallInput}>
          {backends.map((b) => <option key={b} value={b}>{backendInfo(b).label}</option>)}
        </select>
        <SizeFields provider={pProvider} cpu={pCpu} memory={pMem} onSize={(c, m) => { setPCpu(c); setPMem(m); }}
                    instanceType={pType} onInstanceType={setPType} region={pRegion} onRegion={setPRegion}
                    location={deploy[backendInfo(pProvider).location ?? '']} style={smallInput} />
        <div style={{ borderTop: '1px solid #27272a', paddingTop: 6, marginTop: 2 }}>
          <div style={{ fontSize: 10, color: '#71717a', marginBottom: 4 }}>Deployment ({pProvider})</div>
          <DeployConfigForm provider={pProvider} value={deploy} onChange={setDeploy} defaults={defaults} />
        </div>
        {pErr && err(pErr)}
        <button onClick={addPool} disabled={busy || !pName.trim() || deployMissing.length > 0}
                style={btn('#22c55e', busy || !pName.trim() || deployMissing.length > 0)}>Add pool</button>
      </div>
    </Section>
  );
}

// ─── The modal ───────────────────────────────────────────────────────────────

export function CloudMenu({ onClose }: { onClose: () => void }) {
  const [settings, setSettings] = useState<CloudSettings | null>(null);
  const [pools, setPools] = useState<DuckPool[]>([]);
  const [computeDefaults, setComputeDefaults] = useState<ComputeDefaults | null>(null);
  const catchment = useLiveStore((s) => s.catchment);
  const catchmentName = catchment?.name || catchment?.id || '';

  const load = () => {
    // Opening / acting in the Cloud menu also freshens the global gate (store.cloud) the rest of the UI
    // reads — the cloud gate is event-driven, not polled.
    void fetchCloudSettings().then((cs) => { setSettings(cs); useLiveStore.setState({ cloud: cs }); })
      .catch(() => setSettings(null));
    void fetchDuckPools().then(setPools).catch(() => setPools([]));
  };
  useEffect(load, []);
  useEffect(() => { void fetchComputeDefaults().then(setComputeDefaults).catch(() => setComputeDefaults(null)); }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const enabled = !!settings?.cloud_enabled;
  const statusBadge = !settings ? null : enabled
    ? (settings.creds_valid === false
        ? <span style={{ fontSize: 11, color: '#f59e0b', fontWeight: 700 }}>enabled · creds failing</span>
        : <span style={{ fontSize: 11, color: '#22c55e', fontWeight: 700 }}>enabled</span>)
    : <span style={{ fontSize: 11, color: '#71717a', fontWeight: 700 }}>disabled</span>;

  return (
    <div onClick={onClose} style={modalBackdrop}>
      <div onClick={(e) => e.stopPropagation()} style={{
        background: '#15151a', border: '1px solid #27272a', borderRadius: 10, width: 'min(560px, 94vw)',
        maxHeight: '86vh', display: 'flex', flexDirection: 'column', overflow: 'hidden', color: '#e4e4e7',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px', borderBottom: '1px solid #27272a', flexShrink: 0 }}>
          <span style={{ fontSize: 13, fontWeight: 700 }}>Cloud</span>
          {statusBadge}
          {enabled && settings && (
            <span style={{ display: 'flex', gap: 5, marginLeft: 2 }}>
              {cloudBackends(settings).map((b) => (
                <StateChip key={b} label={backendInfo(b).label.split(' ')[0]} on={!!settings.backends?.[b]} />
              ))}
            </span>
          )}
          {!enabled && settings && (
            <span style={{ fontSize: 10, color: '#52525b' }}>
              {!settings.provider_title
                ? 'needs a data root on a cloud + its credentials'
                : settings.compute_available
                  ? `needs ${settings.provider_title} credentials`
                  : `remote Ducks on ${settings.provider_title} aren't available yet`}
            </span>
          )}
          <button onClick={onClose} title="Close (Esc)"
                  style={{ marginLeft: 'auto', background: 'transparent', border: '1px solid #3f3f46', borderRadius: 5,
                           color: '#a1a1aa', fontSize: 13, lineHeight: 1, padding: '4px 9px', cursor: 'pointer', fontFamily: 'inherit' }}>
            ✕
          </button>
        </div>
        <div style={{ overflowY: 'auto', padding: '12px 16px', minHeight: 0 }}>
          <MigrationBanner onDone={load} />
          {settings && <DataPlanePanel settings={settings} catchmentName={catchmentName} reload={load} />}
          {settings && <CredentialsPanel settings={settings} reload={load} />}
          {enabled && settings && (
            <PoolsPanel pools={pools} reload={load} defaults={computeDefaults} backends={cloudBackends(settings)}
                        presetsReady={!!settings.backends?.[cloudBackends(settings)[0]]} />
          )}
        </div>
      </div>
    </div>
  );
}
