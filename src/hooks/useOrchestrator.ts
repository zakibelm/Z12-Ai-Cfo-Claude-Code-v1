import { activeFolderParams } from '../utils/activeFolder';
import { useState, useCallback } from 'react';

export interface AgentResult {
  agent: string;
  content: string;
  timestamp: number;
}

export interface OrchestratorState {
  isRunning: boolean;
  complexity: 'simple' | 'medium' | 'complex' | null;
  activeAgents: string[];
  completedAgents: string[];
  agentResults: AgentResult[];
  synthesis: string | null;
  error: string | null;
}

export function useOrchestrator() {
  const [state, setState] = useState<OrchestratorState>({
    isRunning: false,
    complexity: null,
    activeAgents: [],
    completedAgents: [],
    agentResults: [],
    synthesis: null,
    error: null,
  });

  const orchestrate = useCallback(async (question: string, context: Record<string, unknown> = {}) => {
    setState({ isRunning: true, complexity: null, activeAgents: [], completedAgents: [], agentResults: [], synthesis: null, error: null });
    try {
      const apiKey = localStorage.getItem('openrouter_api_key') || '';
      const response = await fetch('/api/orchestrate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(apiKey ? { 'X-API-Key': apiKey } : {}) },
        // Dossier client actif : limite les données client lues par les agents à ce dossier.
        body: JSON.stringify({ question, context: { ...activeFolderParams(), ...context } }),
      });
      if (!response.ok) {
        // Le serveur explique lui-même un refus (budget atteint, dossier introuvable).
        const refusal = await response.json().catch(() => ({}));
        throw new Error((typeof refusal?.detail === 'string' && refusal.detail) || `HTTP ${response.status}`);
      }
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const lines = decoder.decode(value).split('\n').filter(l => l.startsWith('data: '));
        for (const line of lines) {
          const raw = line.replace('data: ', '').trim();
          if (raw === '[DONE]') { setState(prev => ({ ...prev, isRunning: false })); return; }
          try {
            const event = JSON.parse(raw);
            if (event.type === 'assessment') {
              setState(prev => ({ ...prev, complexity: event.complexity, activeAgents: event.agents }));
            } else if (event.type === 'agent_result') {
              setState(prev => ({
                ...prev,
                completedAgents: [...prev.completedAgents, event.agent],
                agentResults: [...prev.agentResults, { agent: event.agent, content: event.content, timestamp: Date.now() }],
              }));
            } else if (event.type === 'synthesis') {
              setState(prev => ({ ...prev, synthesis: event.content }));
            }
          } catch { /* skip malformed */ }
        }
      }
    } catch (err) {
      setState(prev => ({ ...prev, isRunning: false, error: err instanceof Error ? err.message : 'Erreur inconnue' }));
    }
  }, []);

  const reset = useCallback(() => {
    setState({ isRunning: false, complexity: null, activeAgents: [], completedAgents: [], agentResults: [], synthesis: null, error: null });
  }, []);

  return { ...state, orchestrate, reset };
}
