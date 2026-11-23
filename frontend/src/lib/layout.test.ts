import { describe, expect, it } from 'vitest';
import { computeLayout, externalApp, externalLabel } from './layout';
import type { Pond } from './types';

const pond = (id: string, external: string[] = [], sources: string[] = []): Pond => ({
  id, name: id.split('@')[0], kind: 'inlet', isDraw: false, isSpout: false, isPuddle: false, dbt: false,
  isApp: false, sources, external,
});

describe('external sources (plans/records-and-media.md)', () => {
  it('draws one node per declared name, shared by the Ponds naming it, with an edge into each', () => {
    const ponds = {
      'tally@1': pond('tally@1', ['app:feedback@1', 'GDELT events']),
      'news@1': pond('news@1', ['GDELT events']),
    };
    const { nodes, edges } = computeLayout(ponds, {}, {});
    const ext = nodes.filter((n) => n.type === 'external').map((n) => n.id).sort();
    expect(ext).toEqual(['ext:GDELT events', 'ext:app:feedback@1']);
    const into = edges.filter((e) => e.source.startsWith('ext:')).map((e) => `${e.source}>${e.target}`).sort();
    expect(into).toEqual(['ext:GDELT events>news@1', 'ext:GDELT events>tally@1', 'ext:app:feedback@1>tally@1']);
  });

  it('reads an App line from its app:name@major form', () => {
    expect(externalApp('app:feedback@2')).toEqual({ name: 'feedback', major: 2 });
    expect(externalLabel('app:feedback@2')).toBe('feedback v2');
    expect(externalApp('GDELT events')).toBeNull();
    expect(externalLabel('GDELT events')).toBe('GDELT events');
  });
});
