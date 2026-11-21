// Modèle par défaut des agents. Si le fournisseur le retire, le serveur bascule de lui-même
// sur son modèle de repli (FALLBACK_MODEL) : le Studio ne reste pas bloqué.
const DEFAULT_AGENT_MODEL = "deepseek/deepseek-v4-pro";
import { AGENTS_DEF } from './agentsConfig';
import { activeFolderParams, setActiveFolder } from './activeFolder';
import { refreshBudget } from '../components/BudgetBadge';
import { fitHistory } from './attachments';

export const agentById    = id => AGENTS_DEF.find(a => a.id === id) || AGENTS_DEF[0];
export const agentColor   = id => agentById(id).color;
export const agentIcon    = id => agentById(id).icon;
export const agentName    = (id, lang) => agentById(id).personName?.[lang] || agentById(id).id;
const agentTitle   = (id, lang) => agentById(id).personTitle?.[lang] || "";

//  VECTDOCS-INSPIRED UTILITIES 

// Inspired by VectDocs EmbeddedDocument fileType enum — extended for finance
export const FILE_CATEGORY = ext => {
  if (["pdf"].includes(ext))                          return "pdf";
  if (["docx","doc"].includes(ext))                   return "docx";
  if (["xlsx","xls"].includes(ext))                   return "xlsx";
  if (["pptx","ppt"].includes(ext))                   return "pptx";
  if (["txt","md","csv","json","html","xml"].includes(ext)) return "text";
  if (["png","jpg","jpeg","gif","webp","tiff"].includes(ext)) return "image";
  if (["mp4","avi","mov","mkv"].includes(ext))         return "video";
  if (["mp3","wav","m4a"].includes(ext))               return "audio";
  if (["zip","rar","7z"].includes(ext))                return "archive";
  if (["msg","eml"].includes(ext))                     return "email";
  return "unknown";
};

const typeIcon = ext => ({pdf:"📄",docx:"📄",doc:"📄",xlsx:"📄",xls:"📄",pptx:"📄",ppt:"📄",csv:"📄",txt:"📄",md:"📄",json:"📄",html:"<",xml:"📄",png:"📄",jpg:"📄",jpeg:"📄",gif:"📄",webp:"📄",tiff:"📄",zip:"📄",rar:"📄",msg:"📄",eml:"📄",mp4:"<📄",mp3:"<📄",wav:"<📄"}[ext] || "📄");

// Inspired by VectDocs — client-side text extraction for instant preview
// Decision: only for lightweight text formats; DOCX/PPTX/XLSX stay server-side
// (browser can't run mammoth/JSZip without those libs, and financial data shouldn't be
//  fully client-side processed for Loi 25 traceability)
import { extractTextPreview, detectAgentFromFile, detectLanguage, estimateChunks, uploadStageLabel } from './fileUtils';

//  SHARED UTILS 
const fmtSize = b => { if(!b) return "~"; const m=b/1048576; return m>=1?m.toFixed(1)+" MB":Math.round(b/1024)+" KB"; };
const fmtTime = (iso: string) => { const d=Math.floor((Date.now()-new Date(iso).getTime())/60000); if(d<1)return"é l'instant";if(d<60)return`${d} min`;if(d<1440)return`${Math.floor(d/60)}h`;if(d<2880)return"Hier";return new Date(iso).toLocaleDateString("fr-CA",{day:"numeric",month:"short"}); };
export const genTitle = msg => { const w=msg.replace(/[*#_]/g,"").trim().split(" "); return w.slice(0,7).join(" ")+(w.length>7?"...":""); };
// Aucune limite de taille — tous les fichiers acceptés sans restriction
const validateFile = () => null;

const T = {
  fr: { nav:{dashboard:"Dashboard",chat:"Chat IA",documents:"Documents",pipeline:"Pipeline RAG",governance:"Gouvernance",agents:"Agents",settings:"Paramètres"}, lang:"FR", langToggle:"EN",
    dash:{title:"Tableau de bord",updated:"Mis à jour",activity:"Activité récente",calendar:"Calendrier fiscal 2025"},
    docs:{title:"Gestion documentaire RAG",knowledge:"Sources de connaissance métier",client:"Documents client",upload:"Glissez vos fichiers ici",sub:"Cliquez pour parcourir à Dossier entier à Jusqu'à 500 MB/fichier à Stockage RAG illimité à Tous types",indexed:"✅ Indexé",staServerOnly:"Extraction côté serveur"},
    chat:{new:"Nouvelle conversation",send:"Envoyer",copy:"Copier",copied:"Copié !",export:"Exporter",retry:"Réessayer",routing:"Détection agent...",noConv:"Aucune conversation\nCommencez par envoyer un message",resume:"Conversation reprise",autoRouted:"Auto-routé vers"},
    agents:{title:"Annuaire des agents",startConv:"Démarrer une conversation",savePrompt:"Sauvegarder",cancel:"Annuler"},
    pipeline:{title:"Pipeline RAG · Observabilité",availability:"Disponibilité",latency:"Latence",errors:"Erreurs",sla:"✓",lastRun:"Dernier run"},
    governance:{title:"Gouvernance & Conformité",policies:"Politiques actives",catalog:"Catalogue données",owner:"Responsable",lastReview:"Dernière revue",nextAudit:"Prochain audit",status:{compliant:"Conforme",review:"é réviser",noncompliant:"Non conforme"}},
  },
  en: { nav:{dashboard:"Dashboard",chat:"AI Chat",documents:"Documents",pipeline:"RAG Pipeline",governance:"Governance",agents:"Agents",settings:"Settings"}, lang:"EN", langToggle:"FR",
    dash:{title:"Dashboard",updated:"Updated",activity:"Recent activity",calendar:"Fiscal calendar 2025"},
    docs:{title:"RAG Document Management",knowledge:"Business knowledge sources",client:"Client documents",upload:"Drag your files here",sub:"Click to browse à Folder upload à Up to 500 MB/file à Unlimited RAG storage à All types",indexed:"✅ Indexed",staServerOnly:"Server-side extraction"},
    chat:{new:"New conversation",send:"Send",copy:"Copy",copied:"Copied!",export:"Export",retry:"Retry",routing:"Detecting agent...",noConv:"No conversations\nStart by sending a message",resume:"Conversation resumed",autoRouted:"Auto-routed to"},
    agents:{title:"Agent directory",startConv:"Start a conversation",savePrompt:"Save",cancel:"Cancel"},
    pipeline:{title:"RAG Pipeline — Observability",availability:"Availability",latency:"Latency",errors:"Errors",sla:"SLA",lastRun:"Last run"},
    governance:{title:"Governance & Compliance",policies:"Active policies",catalog:"Data catalog",owner:"Owner",lastReview:"Last review",nextAudit:"Next audit",status:{compliant:"Compliant",review:"Needs review",noncompliant:"Non-compliant"}},
  }
};

//  API 
// Standard call — RAG agents (no web search)
export async function callClaude(system: string, messages: any[], openrouterKey: string) {
  // Routes through OpenRouter which supports Anthropic Claude models
  return callOpenRouter("deepseek/deepseek-v4-pro", system, messages, openrouterKey, false);
}

// Web-search-enabled call — VeilleAgent + SubventionsAgent
// Uses Anthropic web_search tool for real-time information
export async function callClaudeWithWebSearch(system: string, messages: any[], openrouterKey: string) {
  // Routes through OpenRouter with web search support
  return callOpenRouter("deepseek/deepseek-v4-pro", system, messages, openrouterKey, true);
}

// Route to correct API based on agent type and available key
const WEB_SEARCH_AGENTS = new Set(["VeilleAgent","SubventionsAgent"]);

//  SHARED UTILS 
// Web-search-enabled call — VeilleAgent + SubventionsAgent
// Uses Anthropic web_search tool for real-time information
// Route to correct API based on agent type and available key
const ORCHESTRATOR_PROMPT = {
  fr: `Tu es l'Orchestrateur du Bureau CPA Virtuel — le directeur coordinateur qui dirige une Équipe de 9 spécialistes CPA.

## Ton réle
Analyser chaque demande de l'utilisateur et décider de la meilleure stratégie de traitement :
- Quel(s) spécialiste(s) mobiliser
- Dans quel ordre (séquentiel) ou simultanément (parallèle)
- Avec quelle priorité

## Ton Équipe
1. **Sophie Mercier** (TaxAgent) — Fiscaliste CPA, M.Fisc. — T1/T2, TPS/TVQ, RS&DE, planification fiscale
2. **Alexandre Bouchard** (AuditAgent) — Auditeur CPA-CA senior — IFRS, ASPE, NCA, contrôles internes
3. **Natalie Chen** (CashFlowAgent) — Directrice trésorerie CTP — BFR, rolling forecast, covenants
4. **Isabelle Roy** (ComplianceAgent) — Conseillére DPO, LL.M. — Loi 25, CASL, PIPEDA, EFVP
5. **Marc Tremblay** (FinancialAgent) — Analyste CFA — ratios, benchmarks, évaluation entreprise
6. **Sarah Blackwell** (InvestmentAgent) — Analyste CFA/MBA — M&A, DCF, LBO, due diligence QoE
7. **Jean-François Lebel** (OCRAgent) — Spécialiste extraction — factures scannées, formulaires CRA/RQ
8. **Émilie Côté** (VeilleAgent) — Analyste veille — ARC, IFRS, AMF, Loi 25 (recherche web temps réel)
9. **Patrick Gagnon** (SubventionsAgent) — Expert subventions — SR&DE, IRAP, Investissement Québec (web)

## Types de workflows

### SINGLE — Requête simple, domaine unique
Exemples : "Quelle est la date limite T2?", "Calcule mon BAIIA", "Extrait cette facture"
é 1 spécialiste, réponse directe

### PARALLEL — Requête multi-domaines, analyses indépendantes
Exemples : "Analysez notre acquisition sous tous les angles", "Préparez notre rapport annuel"
é 2-4 spécialistes travaillent SIMULTANéMENT, synthèse finale
é Quand chaque analyse est indépendante et n'a pas besoin des autres

### SEQUENTIAL — Requête oé chaque étape alimente la suivante
Exemples : "évaluez si ce projet est viable fiscalement ET financièrement ET trouver des subventions"
é étape 1 à son output devient le contexte de l'étape 2 à etc.
é Quand l'analyse d'un spécialiste dépend des conclusions du précédent

### HYBRID — Mélange parallèle puis séquentiel
Exemples : "Nouveau projet tech : quelles subventions, quelle structure fiscale, et validez que c'est conforme"
é Phase 1 PARALLEL : Sophie (fiscal) + Isabelle (conformité)
é Phase 2 SEQUENTIAL : Patrick (subventions, avec contexte fiscal)

## Régles de priorité
- **URGENT** (=4) : délais règlementaires <30 jours, risques légaux, cotisations imminentes
- **ÉLEVÉE** (H) : décisions d'affaires importantes, opportunités financières, audit en cours
- **NORMALE** (M) : analyse stratégique, planification, optimisation
- **FAIBLE** (L) : veille, information générale, questions de fond

## Régles d'assignation intelligente
- Toujours mobiliser OCR en PREMIER si un document scanné est mentionné (Jean-François extrait, les autres analysent)
- Toujours mobiliser Veille si la demande concerne des mises à jour récentes ou l'actualité règlementaire
- Toujours mobiliser Subventions si un nouveau projet/investissement est mentionné
- Pour une acquisition : Sarah (investissement) + Sophie (fiscal) + Marc (financier) en parallèle
- Pour un audit : Alexandre seul OU Alexandre + Isabelle (conformité) si risques données
- Pour une restructuration : Sophie + Marc + Sarah en séquentiel (fiscal à financier à investissement)
- Pour un nouveau projet tech : Émilie (veille) + Patrick (subventions) en parallèle à Sophie (fiscal) séquentiel

## Format de réponse OBLIGATOIRE
Tu dois répondre UNIQUEMENT avec un objet JSON valide, sans texte avant ni aprés, sans markdown :
{
  "type": "single|parallel|sequential|hybrid",
  "priority": "urgent|high|normal|low",
  "agents": ["AgentId1", "AgentId2"],
  "phases": [
    {"type":"parallel","agents":["AgentId1","AgentId2"]},
    {"type":"sequential","agents":["AgentId3"]}
  ],
  "reason": "Explication en 1 phrase de pourquoi ce workflow",
  "user_message": "Message personnalisé à afficher à l'utilisateur (prénom des spécialistes mobilisés, ce qu'ils vont faire)",
  "estimated_seconds": 15,
  "synthesis_needed": true
}

Note : "phases" n'est utilisé que pour le type "hybrid". Pour single/parallel/sequential, utilise "agents".`,

  en: `You are the Virtual CPA Firm Orchestrator — the coordinating director managing a team of 9 CPA specialists.

## Your Role
Analyze each user request and decide the optimal processing strategy:
- Which specialist(s) to mobilize
- In what order (sequential) or simultaneously (parallel)
- With what priority

## Your Team
1. **Sophie Mercier** (TaxAgent) — CPA Tax Specialist — T1/T2, GST/QST, SR&ED, tax planning
2. **Alexandre Bouchard** (AuditAgent) — Senior CPA-CA Auditor — IFRS, ASPE, CAS, internal controls
3. **Natalie Chen** (CashFlowAgent) — CTP Treasury Director — working capital, rolling forecast, covenants
4. **Isabelle Roy** (ComplianceAgent) — DPO Advisor — Law 25, CASL, PIPEDA, DPIA
5. **Marc Tremblay** (FinancialAgent) — CFA Analyst — ratios, benchmarks, business valuation
6. **Sarah Blackwell** (InvestmentAgent) — CFA/MBA Analyst — M&A, DCF, LBO, QoE due diligence
7. **Jean-François Lebel** (OCRAgent) — Extraction Specialist — scanned invoices, CRA/RQ forms
8. **Émilie Côté** (VeilleAgent) — Watch Analyst — CRA, IFRS, AMF, Law 25 (real-time web search)
9. **Patrick Gagnon** (SubventionsAgent) — Grants Expert — SR&ED, IRAP, Investissement Québec (web)

## Workflow Types

### SINGLE — Simple request, single domain à 1 specialist
### PARALLEL — Multi-domain, independent analyses à 2-4 simultaneous à synthesis
### SEQUENTIAL — Each step feeds the next à chain of specialists
### HYBRID — Parallel phases followed by sequential steps

## Priority Rules
- **URGENT** (=4): regulatory deadlines <30 days, legal risks
- **HIGH** (H): important business decisions, active audits
- **NORMAL** (M): strategic analysis, planning, optimization
- **LOW** (L): monitoring, general information

## Smart Assignment Rules
- Always OCR first if scanned document mentioned (JF extracts, others analyze)
- Always Veille if recent regulatory updates requested
- Always Subventions if new project/investment mentioned
- Acquisition: Sarah + Sophie + Marc parallel
- New tech project: Émilie + Patrick parallel à Sophie sequential

## MANDATORY Response Format
Respond ONLY with valid JSON, no text before or after:
{
  "type": "single|parallel|sequential|hybrid",
  "priority": "urgent|high|normal|low",
  "agents": ["AgentId1", "AgentId2"],
  "phases": [{"type":"parallel","agents":["AgentId1"]},{"type":"sequential","agents":["AgentId2"]}],
  "reason": "1-sentence explanation",
  "user_message": "Message to user (specialist names, what they will do)",
  "estimated_seconds": 15,
  "synthesis_needed": true
}`
};

export const MAX_PLAN_AGENTS = 4;
const PLAN_TYPES = ["single", "parallel", "sequential", "hybrid"];

/**
 * Rend un plan d'orchestration (souvent issu d'un LLM) sûr à exécuter :
 * agents inconnus retirés (y compris dans les phases), doublons supprimés, maximum 4 agents,
 * type cohérent avec le nombre d'agents. Si rien de valide ne reste, repli sur le routage
 * par mots-clés avec `fallback: true` et un message pour l'utilisateur.
 */
export function sanitizePlan(plan, query = "") {
  const known = new Set(AGENTS_DEF.map(a => a.id));
  const clean = list => [...new Set((Array.isArray(list) ? list : []).filter(id => typeof id === "string" && known.has(id)))];
  let agents = clean(plan?.agents).slice(0, MAX_PLAN_AGENTS);
  if (!agents.length) {
    const fast = fastRoute(query);
    return { type: "single", priority: "normal", agents: [fast || "FinancialAgent"],
      reason: "Routage automatique de secours", estimated_seconds: 10, synthesis_needed: false, fallback: true,
      user_message: "Plan de l'orchestrateur invalide : routage automatique utilisé." };
  }
  const keep = new Set(agents);
  const phases = (Array.isArray(plan?.phases) ? plan.phases : [])
    .map(ph => ({ ...ph, agents: clean(ph?.agents).filter(id => keep.has(id)) }))
    .filter(ph => ph.agents.length);
  let type = PLAN_TYPES.includes(plan?.type) ? plan.type : (agents.length === 1 ? "single" : "parallel");
  if (agents.length === 1) type = "single";
  else if (type === "single") type = "parallel";
  if (type === "hybrid" && !phases.length) type = "parallel";
  const out = { ...plan, type, agents, fallback: !!plan?.fallback };
  // Les consommateurs testent `plan.phases` : on ne le garde que s'il sert (hybride).
  if (type === "hybrid") out.phases = phases; else delete out.phases;
  return out;
}

// Analyze request and return workflow plan
export async function analyzeWorkflow(query, historyMsgs, lang, openrouterKey) {
  // Aucune clé n'est exigée dans le navigateur : le serveur utilise la clé de la plateforme et
  // répond lui-même par un message clair s'il n'en a pas.
  const system = ORCHESTRATOR_PROMPT[lang] || ORCHESTRATOR_PROMPT.fr;
  const msgs = [
    // Pour choisir les spécialistes, le début de chaque pièce jointe suffit : le plan n'a pas
    // besoin du document entier, que chaque spécialiste recevra.
    ...fitHistory(historyMsgs, 4).map(m => m.files?.length && m.content.length > ROUTING_EXCERPT
      ? { ...m, content: m.content.slice(0, ROUTING_EXCERPT) + "\n[…]\n" + (m.display ?? "") } : m),
    { role:"user", content: `Analyse cette demande et retourne le plan de workflow JSON :\n\n"${query}"` }
  ];
  try {
    let raw;
    if (openrouterKey) {
      raw = await callOpenRouter(DEFAULT_AGENT_MODEL, system, msgs, openrouterKey, false, "routing");
    } else {
      raw = await callOpenRouter(DEFAULT_AGENT_MODEL, system, msgs, openrouterKey, false, "routing");
    }
    // Extract JSON from response
    const jsonMatch = raw.match(/\{[\s\S]*\}/);
    if (jsonMatch) {
      return sanitizePlan(JSON.parse(jsonMatch[0]), query);
    }
  } catch(e) { console.warn("Orchestrator parse error:", e); }
  // Fallback: fast route
  return sanitizePlan(null, query);
}

// Execute a workflow plan — returns array of {agentId, name, reply, status}
const ROUTING_EXCERPT = 3000;

/** Vrai si un texte est un plan d'orchestration (JSON avec « agents ») et non une réponse. */
export function looksLikeRoutingPlan(text) {
  const t = (text || "").trim().replace(/^```(?:json)?\s*/i, "").replace(/```$/, "").trim();
  return t.startsWith("{") && /"agents"\s*:/.test(t) && /"type"\s*:/.test(t);
}

export async function executeWorkflow(plan, query, historyMsgs, agentSettings, openrouterKey, lang, onProgress) {
  // L'historique porte déjà la question en cours (avec ses pièces jointes) en dernier message :
  // on ne la répète pas, sauf si l'appelant ne l'y a pas mise.
  const baseMessages = fitHistory(historyMsgs, 6);
  const last = baseMessages[baseMessages.length - 1];
  const alreadyAsked = last?.role === "user" && typeof last.content === "string" && last.content.endsWith(query);
  const userMsg = { role:"user", content:query };

  const runOne = async (agentId, contextExtra="") => {
    const def = agentById(agentId);
    const prompt = agentSettings[agentId]?.prompt || (def.defaultPrompt?.[lang] ?? def.systemPrompt ?? "");
    const model  = agentSettings[agentId]?.model;
    const msgs = alreadyAsked ? [...baseMessages] : [...baseMessages, userMsg];
    if (contextExtra) msgs.push({ role:"user", content: contextExtra });
    onProgress?.(agentId, "working");
    try {
      let reply = await callAgent(agentId, prompt, msgs, openrouterKey, model);
      if (looksLikeRoutingPlan(reply)) {
        // Un spécialiste ne doit jamais répondre par un plan d'orchestration : un nouvel essai, consigne explicite.
        reply = await callAgent(agentId, prompt, [...msgs, { role:"user", content: lang==="fr"
          ? "Réponds à la demande ci-dessus en texte rédigé, en tant que spécialiste. Ne renvoie pas de JSON ni de plan de workflow."
          : "Answer the request above in written prose, as the specialist. Do not return JSON or a workflow plan." }], openrouterKey, model);
        if (looksLikeRoutingPlan(reply)) throw new Error(lang==="fr" ? "Le spécialiste n'a pas produit de réponse rédigée. Reposez la question." : "The specialist did not produce a written answer. Ask again.");
      }
      onProgress?.(agentId, "done");
      return { agentId, name:agentName(agentId, lang), title:agentTitle(agentId, lang), reply, status:"done" };
    } catch(e) {
      onProgress?.(agentId, "error");
      return { agentId, name:agentName(agentId, lang), title:agentTitle(agentId, lang), reply:`Erreur : ${e.message}`, status:"error" };
    }
  };

  if (plan.type === "single") {
    const result = await runOne(plan.agents[0]);
    return [result];
  }

  if (plan.type === "parallel") {
    return await Promise.all(plan.agents.map(id => runOne(id)));
  }

  if (plan.type === "sequential") {
    const results = [];
    let context = "";
    for (const agentId of plan.agents) {
      const result = await runOne(agentId, context);
      results.push(result);
      const n = agentName(agentId, lang);
      context = lang==="fr"
        ? `\n\n[Analyse préalable de ${n} :]:\n${result.reply}\n\n[Suite de la demande originale :]`
        : `\n\n[Prior analysis by ${n}:]:\n${result.reply}\n\n[Continuation of original request:]`;
    }
    return results;
  }

  if (plan.type === "hybrid" && plan.phases) {
    const results = [];
    let prevContext = "";
    for (const phase of plan.phases) {
      if (phase.type === "parallel") {
        const phaseResults = await Promise.all(phase.agents.map(id => runOne(id, prevContext)));
        results.push(...phaseResults);
        prevContext = phaseResults.map(r => `[${r.name}]: ${r.reply}`).join("\n\n");
      } else {
        for (const agentId of phase.agents) {
          const result = await runOne(agentId, prevContext);
          results.push(result);
          prevContext = `[${result.name}]: ${result.reply}`;
        }
      }
    }
    return results;
  }

  return [await runOne(plan.agents?.[0] || "FinancialAgent")];
}

// Synthesize multiple agent results into a unified response
export async function synthesizeResults(results, query, plan, lang, openrouterKey, agentSettings) {
  if (results.length <= 1) return null;
  const synthPrompt = lang === "fr"
    ? `Tu es l'Orchestrateur du Bureau CPA Virtuel. Plusieurs spécialistes ont analysé la demande suivante en parallèle ou en séquence. Tu dois maintenant synthétiser leurs analyses en une réponse unifiée, structurée et directement actionnable pour le client.

INSTRUCTIONS :
- Commence par un résumé exécutif de 3-5 points clés
- Intégre les recommandations complémentaires de chaque spécialiste sans répétition
- Mets en évidence les points de convergence et les tensions éventuelles entre analyses
- Termine par un plan d'action priorisé (URGENT / éLEVé / NORMAL) avec responsable suggéré
- Sois direct, pratique et orienté décision — pas de théorie
- Indique quel spécialiste a produit chaque analyse (prénom seulement)`
    : `You are the Virtual CPA Firm Orchestrator. Multiple specialists have analyzed the following request in parallel or sequentially. Synthesize their analyses into a unified, structured, directly actionable response.

INSTRUCTIONS:
- Start with a 3-5 point executive summary
- Integrate complementary recommendations without repetition
- Highlight convergence points and potential tensions
- End with a prioritized action plan (URGENT / HIGH / NORMAL) with suggested owner
- Be direct, practical, decision-oriented — no theory
- Indicate which specialist produced each analysis (first name only)`;

  const combined = results.map(r => `### ${r.name} — ${r.title}\n${r.reply}`).join("\n\n---\n\n");
  const msgs = [{ role:"user", content:`Demande originale :\n"${query}"\n\n${combined}` }];
  try {
    return await callOpenRouter(DEFAULT_AGENT_MODEL, synthPrompt, msgs, openrouterKey, false, "synthesis");
  } catch { return null; }
}

// Longueur maximale d'une réponse, selon la nature de l'appel (plafond du serveur : 16 000).
// Pour un modèle « à réflexion », cette longueur couvre la réflexion puis le texte : avec 4 000,
// l'analyse d'un cas de sept pages s'arrêtait avant le premier mot.
const MAX_TOKENS = { agent: 12000, routing: 1500, synthesis: 8000 };

/** Réponse du serveur pour une question. `purpose` : "agent" (spécialiste), "routing" (plan de
 *  l'orchestrateur) ou "synthesis". Seul un spécialiste consulte les documents et la mémoire. */
export async function callOpenRouter(model, system, messages, apiKey, useWebSearch = false, purpose = "agent") {
  const body = {
    model,
    purpose,
    messages: [{ role:"system", content:system }, ...messages.map(m=>({role:m.role,content:m.content}))],
    max_tokens: MAX_TOKENS[purpose] || MAX_TOKENS.agent,
    ...(useWebSearch ? { plugins:[{ id:"web", max_results:5 }] } : {}),
  };
  const send = (extra) => fetch("/api/chat", {
    method:"POST",
    headers:{
      "Content-Type":"application/json",
      ...(apiKey ? {"X-API-Key":apiKey} : {}),
    },
    // Dossier client actif : les agents ne lisent les données client que de ce dossier.
    body: JSON.stringify({ ...body, ...extra })
  });
  const folder = activeFolderParams();
  let res = await send(folder);
  if (res.status === 404 && folder.client_id) {
    // Le dossier actif n'existe plus : on l'oublie et on répond sans données client.
    setActiveFolder(null);
    res = await send({});
  }
  refreshBudget(); // la consommation affichée suit chaque réponse
  if (!res.ok) {
    const err = await res.json().catch(()=>({}));
    // Le serveur explique lui-même un refus (budget atteint, fournisseur indisponible).
    throw new Error((typeof err?.detail === "string" && err.detail) || err?.error?.message || `OpenRouter ${res.status}`);
  }
  const d = await res.json();
  // Une réponse servie par le cache du serveur peut ne porter que « content ».
  const choice = d.choices?.[0];
  const text = (choice?.message?.content ?? d.content ?? "").trim();
  if (!text) {
    throw new Error(choice?.finish_reason === "length"
      ? "Le modèle a atteint sa limite de longueur avant d'écrire sa réponse. Reposez la question en la découpant."
      : "Le modèle n'a renvoyé aucun texte. Reposez la question.");
  }
  if (choice?.finish_reason === "length" && purpose !== "routing") {
    return text + "\n\n_Réponse interrompue par la limite de longueur. Écrivez « continue » pour obtenir la suite._";
  }
  return text;
}

export async function callAgent(agentId, system, messages, openrouterKey, agentModel) {
  // Le modèle réglé pour l'agent s'applique à tous les comptes, avec ou sans clé dans le navigateur.
  return callOpenRouter(agentModel || DEFAULT_AGENT_MODEL, system, messages, openrouterKey, WEB_SEARCH_AGENTS.has(agentId), "agent");
}

export function fastRoute(msg) {
  const m = (msg || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g,"");
  // Du plus spécifique au plus général : « crédit d'impôt » est une subvention avant d'être un impôt.
  if (/subvention|aide.financiere|programme.{0,12}(financement|gouvernement)|grant|bourse|irap|\bpari\b|investissement.quebec|\bcld\b|\bmrc\b|financement.gouvern|non.gouvern|fondation|accelerateur|canexport|\bcdap\b|credits?.d.impot/.test(m)) return "SubventionsAgent";
  if (/veille|actualite|mise.a.jour|nouveaute|changement.{0,12}(recent|de.loi)|derniere.loi|nouvelle.norme|nouvelles?.reglementation|jurisprudence|bulletin|circulaire|nouvelles.fiscales|budget.(federal|du.quebec)/.test(m)) return "VeilleAgent";
  if (/\bocr\b|scan|numeris|photo|extrai|extraction|manuscrit|releve.bancaire/.test(m)) return "OCRAgent";
  if (/loi.25|casl|pipeda|conformite|donnees.personnelles|vie.privee|consentement|efvp/.test(m)) return "ComplianceAgent";
  if (/audit|ifrs|aspe|ncecf|verification|controle.interne|materialite|fraude|risques?/.test(m)) return "AuditAgent";
  if (/tresorerie|cash.?flow|liquidite|flux.de|prevision.de.caisse|bfr|fonds.de.roulement/.test(m)) return "CashFlowAgent";
  if (/investissement|\broi\b|\btir\b|\bvan\b|dcf|portefeuille|acquisition|valorisation|due.diligence/.test(m)) return "InvestmentAgent";
  if (/ratio|analyse.financiere|benchmark|baiia|ebitda|marge|solvabilite|rentabilite|bilan/.test(m)) return "FinancialAgent";
  if (/\bt1\b|\bt2\b|tps|tvq|gst|hst|fiscal|impot|cra|revenu.quebec|deduction|amortissement|rs.?&?.?de|sred/.test(m)) return "TaxAgent";
  return null;
}


const card = (P, extra={}) => ({ background:P.card, border:`1px solid ${P.border}`, borderRadius:12, ...extra });

//  MOCK DATA 
const KNOWLEDGE_DOCS_INIT: any[] = []

const CLIENT_DOCS_INIT: any[] = []

const PIPELINE_DATA = [
  {id:"bronze",label:"Ingestion (Bronze)",icon:"⚖️",desc:"Upload, validation SHA-256, stockage S3 ca-central-1",metrics:{availability:"99.8%",latency:"1.2s",errors:"0.02%",sla:"✅"},status:"active",lastRun:"Il y a 4 min"},
  {id:"silver",label:"Traitement (Silver)",icon:"⚖️",desc:"Extraction texte (PyPDF2/python-docx), nettoyage, chunking 500 tokens",metrics:{availability:"99.5%",latency:"3.8s",errors:"0.1%",sla:"✅"},status:"active",lastRun:"Il y a 5 min"},
  {id:"gold",  label:"Embedding (Gold)",  icon:"(",desc:"HF multilingual-e5-large à pgvector 1024 dims",metrics:{availability:"99.9%",latency:"2.1s",errors:"0.0%",sla:"✅"},status:"active",lastRun:"Il y a 5 min"},
  {id:"ready", label:"Prêt à l'emploi",   icon:"✅",desc:"search_chunks() à cosine similarity à seuil 0.6 à EVV 9/10",metrics:{availability:"100%",latency:"0.4s",errors:"0.0%",sla:"✅"},status:"completed",lastRun:"En continu"},
];

const GOV_POLICIES = [
  {id:"loi25",  name:"Loi 25 (Québec)",  owner:"DPO — Marie Tremblay",lastReview:"2025-01-10",nextAudit:"2025-09-22",status:"compliant",desc:"Protection renseignements personnels, EFVP, droit à l'effacement"},
  {id:"casl",   name:"CASL",             owner:"Compliance — Jean Roy",lastReview:"2024-12-01",nextAudit:"2025-06-01",status:"review",   desc:"Double opt-in, mécanisme désabonnement, logs consentement"},
  {id:"pipeda", name:"PIPEDA (fédéral)", owner:"DPO — Marie Tremblay",lastReview:"2025-01-15",nextAudit:"2025-07-15",status:"compliant",desc:"Collecte, utilisation et divulgation renseignements personnels"},
  {id:"ifrs",   name:"IFRS Disclosure",  owner:"CFO — Zaki Belkhiter", lastReview:"2024-11-30",nextAudit:"2025-03-31",status:"compliant",desc:"Obligations de divulgation états financiers IFRS"},
  {id:"cra",    name:"Conformité ARC",   owner:"Tax — Sophie Mercier", lastReview:"2025-01-20",nextAudit:"2025-04-30",status:"review",   desc:"T2, T4, TPS/TVQ — échéances et remises"},
];

const DATA_QUALITY = [
  {label:{fr:"Précision sources métier",en:"Knowledge source accuracy"},value:"98.4%",trend:"+0.3%",status:"improving"},
  {label:{fr:"Fraécheur documents",     en:"Document freshness"},       value:"94.1%",trend:"-0.5%",status:"stable"},
  {label:{fr:"Couverture domaines",     en:"Domain coverage"},          value:"87.0%",trend:"+2.1%",status:"improving"},
  {label:{fr:"Taux d'indexation",       en:"Indexing rate"},            value:"99.2%",trend:"é",    status:"stable"},
];

//  ENHANCED UPLOAD ZONE (VectDocs-inspired) 


//  ORCHESTRATOR SYSTEM 
// The orchestrator is the brain of the virtual CPA firm.
// It analyzes each request, determines the optimal workflow (single/parallel/sequential),
// assigns the right specialists, coordinates execution, and synthesizes results.

// Execute a workflow plan — returns array of {agentId, name, reply, status}
//  MOCK DATA 
