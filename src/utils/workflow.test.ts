import { describe, it, expect } from 'vitest';

describe('workflow — DEFAULT_AGENT_MODEL', () => {
  it('DEFAULT_AGENT_MODEL should be importable from workflow module', async () => {
    const mod = await import('./workflow');
    expect(mod).toBeDefined();
  });

  it('DEFAULT_AGENT_MODEL constant should be a non-empty string', async () => {
    const fs = await import('fs');
    const content = fs.readFileSync('./src/utils/workflow.ts', 'utf-8');
    const match = content.match(/const DEFAULT_AGENT_MODEL\s*=\s*["']([^"']+)["']/);
    expect(match).not.toBeNull();
    expect(match![1]).toBeTruthy();
    expect(match![1].length).toBeGreaterThan(0);
  });

  it('DEFAULT_AGENT_MODEL should follow model/name format (contains /)', async () => {
    const fs = await import('fs');
    const content = fs.readFileSync('./src/utils/workflow.ts', 'utf-8');
    const match = content.match(/const DEFAULT_AGENT_MODEL\s*=\s*["']([^"']+)["']/);
    expect(match).not.toBeNull();
    expect(match![1]).toContain('/');
  });
});

describe('workflow — callOpenRouter function signature', () => {
  it('callOpenRouter should be exported from workflow', async () => {
    const mod = await import('./workflow');
    expect(typeof mod.callOpenRouter).toBe('function');
  });

  it('callAgent should be exported from workflow', async () => {
    const mod = await import('./workflow');
    expect(typeof mod.callAgent).toBe('function');
  });

  it('fastRoute should be exported from workflow', async () => {
    const mod = await import('./workflow');
    expect(typeof mod.fastRoute).toBe('function');
  });

  it('fastRoute should return a value (string or undefined) for a given message', async () => {
    const mod = await import('./workflow');
    const result = mod.fastRoute('Analyse mon cashflow');
    // fastRoute may return agent id or undefined for unknown queries
    expect(result === undefined || result === null || typeof result === 'string').toBe(true);
  });
});

describe('workflow — genTitle', () => {
  it('genTitle should be exported', async () => {
    const mod = await import('./workflow');
    expect(typeof mod.genTitle).toBe('function');
  });

  it('genTitle should truncate long messages to max 7 words + ellipsis', async () => {
    const mod = await import('./workflow');
    const longMsg = 'un deux trois quatre cinq six sept huit neuf dix onze douze';
    const title = mod.genTitle(longMsg);
    // genTitle takes first 7 words then adds "..." if more words exist
    expect(title).toContain('...');
    const wordCount = title.replace('...', '').trim().split(' ').filter(Boolean).length;
    expect(wordCount).toBeLessThanOrEqual(7);
  });

  it('genTitle should return a string for a normal message', async () => {
    const mod = await import('./workflow');
    const title = mod.genTitle('Analyse mon bilan financier');
    expect(typeof title).toBe('string');
    expect(title.length).toBeGreaterThan(0);
  });

  it('genTitle should not add ellipsis for short messages (7 words or fewer)', async () => {
    const mod = await import('./workflow');
    const shortMsg = 'Analyse le bilan';
    const title = mod.genTitle(shortMsg);
    expect(title).not.toContain('...');
  });
});

import { sanitizePlan, MAX_PLAN_AGENTS } from './workflow';
import deleg from '../../backend/tests/benchmarks/delegation_dataset.json';

describe('sanitizePlan', () => {
  it('retire les agents inconnus, les doublons et borne à 4', () => {
    const p = sanitizePlan({ type: 'parallel', agents: ['TaxAgent', 'Bidon', 'TaxAgent', 'AuditAgent', 'CashFlowAgent', 'ComplianceAgent', 'FinancialAgent'] }, 'x');
    expect(p.agents).toEqual(['TaxAgent', 'AuditAgent', 'CashFlowAgent', 'ComplianceAgent']);
    expect(p.agents.length).toBeLessThanOrEqual(MAX_PLAN_AGENTS);
  });
  it('filtre aussi les phases hybrides', () => {
    const p = sanitizePlan({ type: 'hybrid', agents: ['TaxAgent', 'AuditAgent'],
      phases: [{ type: 'parallel', agents: ['TaxAgent', 'Fantome'] }, { type: 'sequential', agents: ['Fantome'] }] }, 'x');
    expect(p.phases).toHaveLength(1);
    expect(p.phases[0].agents).toEqual(['TaxAgent']);
  });
  it('normalise le type selon le nombre d\'agents', () => {
    expect(sanitizePlan({ type: 'parallel', agents: ['TaxAgent'] }, '').type).toBe('single');
    expect(sanitizePlan({ type: 'single', agents: ['TaxAgent', 'AuditAgent'] }, '').type).toBe('parallel');
  });
  it('repli signalé quand le plan est inutilisable', () => {
    const p = sanitizePlan({ agents: ['Bidon'] }, 'Comment payer moins de TPS ?');
    expect(p.fallback).toBe(true);
    expect(p.agents).toEqual(['TaxAgent']);
    expect(sanitizePlan(null, 'zzz').agents).toEqual(['FinancialAgent']);
  });
});

describe('benchmark de délégation (fastRoute côté client)', () => {
  const server2client: Record<string, string> = { sophie: 'TaxAgent', alexandre: 'AuditAgent', natalie: 'CashFlowAgent',
    isabelle: 'ComplianceAgent', marc: 'FinancialAgent', sarah: 'InvestmentAgent', jeanfrancois: 'OCRAgent',
    emilie: 'VeilleAgent', patrick: 'SubventionsAgent' };
  it('route correctement au moins 85 % des questions à agent unique', async () => {
    const { fastRoute } = await import('./workflow');
    const single = (deleg as any[]).filter(c => c.expect.length === 1);
    const ok = single.filter(c => fastRoute(c.q) === server2client[c.expect[0]]).length;
    console.log(`[benchmark délégation client] ${ok}/${single.length}`, single.filter(c => fastRoute(c.q) !== server2client[c.expect[0]]).map(c => c.q + ' -> ' + fastRoute(c.q)));
    expect(ok / single.length).toBeGreaterThanOrEqual(0.85);
  });
});
