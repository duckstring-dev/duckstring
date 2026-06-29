'use client';

import { useEffect, useState } from 'react';
import { fetchSecrets, removeSecret, setSecret, type SecretName } from '@/lib/api';

const input: React.CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  background: '#1a1a1f',
  border: '1px solid #3f3f46',
  borderRadius: 4,
  color: '#e4e4e7',
  padding: '4px 7px',
  fontSize: 12,
};

// The catchment-wide write-only secret store (full access only). Set/list-names/remove — values are
// never returned. Reference a stored secret from a Spout destination as ${secret:NAME}.
export function SecretsMenu({ onClose }: { onClose: () => void }) {
  const [secrets, setSecrets] = useState<SecretName[]>([]);
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => fetchSecrets().then(setSecrets).catch(() => setSecrets([]));
  useEffect(() => { void load(); }, []);

  const submit = async () => {
    setErr(null);
    setBusy(true);
    try {
      await setSecret(name.trim(), value);
      setName('');
      setValue('');
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed to set');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      style={{
        marginTop: 8,
        background: '#15151a',
        border: '1px solid #27272a',
        borderRadius: 8,
        padding: '9px 12px',
        fontFamily: 'ui-monospace, SFMono-Regular, monospace',
        minWidth: 168,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
        <span style={{ fontSize: 10, fontWeight: 700, color: '#a1a1aa', letterSpacing: '0.08em' }}>SECRETS</span>
        <span role="button" onClick={onClose} style={{ cursor: 'pointer', color: '#52525b', fontSize: 13, lineHeight: 1 }}>✕</span>
      </div>
      <div style={{ fontSize: 10, color: '#52525b', marginBottom: 8, lineHeight: 1.5 }}>
        Write-only. Reference from a Spout as <span style={{ color: '#71717a' }}>{'${secret:NAME}'}</span>; values are never shown.
      </div>

      {secrets.length === 0 && <div style={{ fontSize: 12, color: '#52525b', marginBottom: 6 }}>None.</div>}
      {secrets.map((s) => (
        <div key={s.name} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
          <span style={{ fontSize: 12, color: '#a1a1aa' }}>{s.name}</span>
          <span
            role="button"
            title="Remove"
            onClick={() => removeSecret(s.name).then(load).catch(() => undefined)}
            style={{ cursor: 'pointer', color: '#52525b', fontSize: 13, lineHeight: 1 }}
          >
            ✕
          </span>
        </div>
      ))}

      <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="NAME (letters, digits, _)" style={input} />
        <input type="password" value={value} onChange={(e) => setValue(e.target.value)} placeholder="value" style={input} />
        {err && <div style={{ fontSize: 11, color: '#ef4444', wordBreak: 'break-word' }}>{err}</div>}
        <button
          onClick={submit}
          disabled={busy || !name.trim() || !value}
          style={{
            background: 'transparent',
            border: '1px solid #22c55e',
            color: '#22c55e',
            borderRadius: 5,
            padding: '4px 12px',
            fontSize: 12,
            cursor: busy || !name.trim() || !value ? 'not-allowed' : 'pointer',
            opacity: busy || !name.trim() || !value ? 0.5 : 1,
            fontWeight: 600,
          }}
        >
          Set secret
        </button>
      </div>
    </div>
  );
}
