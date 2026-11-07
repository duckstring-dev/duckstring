import { describe, expect, it } from 'vitest';
import { BACKENDS, cloudBackends, deployConfigMissing } from './clouds';
import type { CloudGate, ComputeDefaults } from './api';

const gate = (backends: Record<string, boolean>) => ({ backends } as unknown as CloudGate);

describe('deployConfigMissing', () => {
  it('mirrors the server: Fargate needs an image or a task definition, and its roles', () => {
    expect(deployConfigMissing('fargate', {}, null)).toEqual(['image', 'subnets', 'execution_role', 'task_role']);
    expect(deployConfigMissing('fargate', { task_definition: 'td:1', subnets: 's', execution_role: 'e', task_role: 't' }, null))
      .toEqual([]);
  });

  it('counts a field the environment supplies', () => {
    const defaults = { containerapps: { fields: [], env: { environment: '/subscriptions/x' }, missing_without_input: [] } } as ComputeDefaults;
    expect(deployConfigMissing('containerapps', {}, defaults)).toEqual(['identity']);
    expect(deployConfigMissing('azurevm', { subnet: 's', identity: 'i' }, null)).toEqual(['location']);
  });
});

describe('cloudBackends', () => {
  it("lists the Catchment's backends, AWS's for an older Catchment", () => {
    expect(cloudBackends(gate({ containerapps: true, azurevm: false }))).toEqual(['containerapps', 'azurevm']);
    expect(cloudBackends(null)).toEqual(['fargate', 'ec2']);
  });

  it('knows every backend the server has', () => {
    expect(Object.keys(BACKENDS).sort()).toEqual(['azurevm', 'containerapps', 'ec2', 'fargate']);
  });
});
