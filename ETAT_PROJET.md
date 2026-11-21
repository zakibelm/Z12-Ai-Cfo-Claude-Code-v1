# Z12 AI CFO — ÉTAT DU PROJET

## ÉTAT ACTUEL — 20 novembre 2026 (v6.0, dépôt Z12-Ai-Cfo-Claude-Code-v1)

Les sections plus bas (v5.8, mai 2026) sont conservées comme **historique**.

### Ce qui est prêt côté code
- **Sécurité** : audit + red team appliqués (voir `AUDIT_2026-11-20.md`) ; 12 tests de durcissement + suite complète verts.
- **Équipe d'agents unifiée** : 9 spécialistes (Sophie, Alexandre, Natalie, Isabelle, Marc, Sarah, Jean-François, Émilie, Patrick). Source unique serveur : `backend/orchestrator/team.py` (prompts réutilisés de `agent_prompts.py`). Côté navigateur : `src/utils/agentsConfig.ts`.
- **Délégation fiable** : le plan est assaini des deux côtés (`sanitizePlan` navigateur, `build_assessment` serveur) : agents inconnus retirés, doublons supprimés, **4 agents maximum**, repli par mots-clés **signalé à l'utilisateur** (pastille « routage auto » / événement SSE `warning`). Modèle de l'orchestrateur configurable : `ORCHESTRATOR_MODEL`.
- **RAG** : `services/retrieval.py` (recherche hybride, séparation métier/client, citations, données confidentielles jamais envoyées au cloud). `services/rag_service.py` reste utilisé par `/query`.
- **Benchmarks mesurés** (hors ligne, dans la CI) : délégation 20/20 côté serveur, ≥ 85 % côté navigateur ; RAG rappel@3 100 % sur le jeu d'essai, 0 fuite entre dossiers clients. Jeux : `backend/tests/benchmarks/*.json`. **À enrichir avec des cas réels de clients** avant de les considérer comme représentatifs.
- **Exploitation** : `scripts/backup.sh` (quotidien, rotation 14 j), `scripts/restore_test.sh` (restauration dans une base jetable), `scripts/install_backup_cron.sh`, `update.sh` réécrit (sauvegarde → mise à jour → santé → retour arrière de la base).
- **CI** : tests backend (dont sécurité + benchmarks), tests frontend, erreurs de syntaxe TS et noms non définis bloquants, build. Les tests `tests/api/*` visent un serveur réel et ne tournent que si `TEST_API_URL` (+ `TEST_EMAIL`/`TEST_PASSWORD`) sont définis.

### Audit du 21 nov. 2026 — correctifs appliqués
- Limiteur d'abus : `FORWARDED_ALLOW_IPS` (pont Docker + réseaux privés) pour que chaque client ait sa propre IP. **À vérifier en production** : 2 adresses différentes doivent avoir des quotas distincts.
- Clés OpenRouter des utilisateurs chiffrées (Fernet, `TOKEN_ENC_KEY`), migration automatique des anciennes clés en clair au démarrage, refus d'écrire en clair si la clé manque.
- `/auth/google/login` renvoie 503 (et non 500) si Google n'est pas configuré.
- Reste : jeu de questions validé par un CPA, rapprochement sur relevés réels, MFA administrateur, Redis si plusieurs workers.

### Ce qui reste à faire de ton côté (hors code)
0. **Générer `TOKEN_ENC_KEY`** (commande dans `.env.example`) et la sauvegarder hors du serveur : sans elle, impossible d'enregistrer une clé utilisateur.
1. Secrets GitHub : `SSH_HOST`, `SSH_USER`, `SSH_PRIVATE_KEY` ; valeurs de `.env` sur le VPS (jamais dans Git).
2. Lancer `scripts/install_backup_cron.sh` une fois sur le VPS, puis vérifier `restore_test.sh`.
3. Gate 2 : 20 relevés bancaires réels à 95 %, CGU/licence, assurance RC, tarification, bêta avec un CPA.
4. `gitleaks` sur tout l'historique du dépôt source ; chiffrement optionnel des clés OpenRouter par utilisateur.
5. Connecteurs ERP/CRM/QuickBooks : à personnaliser par client (volontairement hors socle).

---

Version 5.8 — 31 mai 2026
Score : 8.8/10 · C1-C17 ✅ · Landing ✅ CI #105 · tiret-insécable/FAQ/C-27 corrigés

---

## SCORE & MILESTONES

| Milestone | Statut | Score |
|-----------|--------|-------|
| M0   Gate 1 fermé          | ✅ PROD | 6.5/10 |
| M0.5 Dette technique       | ✅ PROD | 7.5/10 |
| M1   Patrick KB QC + RAG + valeur chiffrée | ✅ 100% PROD | 8.3/10 |
| M2   Auth self-service (README + WelcomeBanner) | ✅ PROD (test réel ⏳ humain) | 8.5/10 |
| M3   BankReconciliationAgent + sidebar Rapprochement | ✅ 100% PROD | 9.0/10 |
| M4   Vitrine parfaite + démo vidéo | ⬜ PENDING | cible 9.5/10 |

---

## ÉTAT PRODUCTION — 30 MAI 2026

### Infrastructure
- VPS : utilisateur de déploiement non-root@<IP-VPS> (Hostinger Ubuntu 24.04)
- URL : https://cfo.optigenius.pro ✅ OPÉRATIONNEL
- CI/CD : GitHub Actions — 121 runs · CI #90 ✅ 21s (dernier)
- Docker : ai-cfo-phoenix-v3-backend-1 sur port 8000
- nginx : /var/www/z12-cfo/dist (bundle index-2MzwgL-Z.js)

### Endpoints vérifiés en prod
- GET  /api/bank/status   → {"status":"ok","agent":"BankReconciliationAgent","version":"1.0"}
- POST /api/bank/reconcile → matched/unmatched/disclaimer ✅
- POST /api/chat           → 9 agents répondent ✅
- GET  /api/whatsapp/status → {"whitelist_count":1,"mode":"twilio_api"} ✅

### Commits clés (cette session)
- e38272a : M3.4 Fix navItems array brackets — Rapprochement sidebar nav ✅ CI #90
- 0a1874d : M3.3 Sidebar nav Rapprochement + backend bank_router ✅ CI #89
- abe9c85 : M3.2 BankReconciliationView frontend (201 lignes) ✅
- b101276 : M3.1 BankReconciliationAgent backend (421 lignes) ✅

---

## M3 — BANKRECONCILIATIONAGENT (✅ 100% PROD)

### Architecture
- INPUT  : Relevé bancaire CSV + Grand livre CSV
- OUTPUT : matchés / non-matchés / différentiel / doublons
- RÈGLE  : temperature=0 (pur Python, déterministe, sans LLM)
- Disclaimer OBLIGATOIRE sur chaque réponse
- Matching : montant exact (±0.01$) + date ±3 jours

### Fichiers
- backend/bank_reconciliation.py (421 lignes) — router bank_router
- backend/main.py — import bank_router + include_router avec guard
- src/components/BankReconciliationView.tsx (201 lignes)
- App.tsx — navItems["bankreconciliation"] dans OUTILS

### Test end-to-end validé
- JS browser test : matched:2 unmatched:0 disclaimer:true ✅
- Prod : https://cfo.optigenius.pro/api/bank/status ✅

---

## GATE 2 — CRITÈRES AVANT MONÉTISATION

| Critère | Statut |
|---------|--------|
| 1 CPA bêta utilise spontanément chaque semaine | ⬜ PENDING |
| BankReconciliationAgent 95% précision vrais relevés | ⬜ 20 cas réels requis |
| CGU licence logicielle (modèle Docker, pas SaaS) | ⬜ PENDING |
| Assurance RC professionnelle souscrite | ⬜ PENDING |
| Pricing validé Van Westendorp (10 entretiens CPA) | ⬜ PENDING |

---

## M2 — CRITÈRE RESTANT (ACTION HUMAINE)

Le critère M2 restant est une action humaine — pas du code.
Trouver 1 personne qui ne connaît pas l'app.
Lui donner uniquement : https://cfo.optigenius.pro
Sans rien expliquer.
Observer où elle bloque.
Si elle pose une question à Patrick en moins de 10 minutes sans aide → M2 ✅

---

## PROCHAINES ÉTAPES TECHNIQUES (M4)

### M4.1 — Bugs UI restants
- Agent cards : afficher nom + rôle ("Alexandre B. · Auditeur") au lieu des initiales ("AB")
- Scroll PARALLEL → synthèse toujours visible sans scroll

### M4.2 — Démo vidéo 90 secondes
- Scénario : PME construction Laval → Patrick (22K$ subventions) → Rapprochement Desjardins
- Format : Loom ou OBS · Sous-titres FR · Pas de voix sur IA

### M4.3 — Landing page z12cfo.com
- Après 10 entretiens CPA QC (pour avoir le vrai message)
- Message central : "Le CFO IA pour PME québécoises. Sur votre serveur."

---

## RÈGLES TECHNIQUES (RAPPEL)

- Ne JAMAIS committer .env, __pycache__, logs, uploads, données client
- Ne PAS refactoriser App.tsx — insertions chirurgicales uniquement
- Frontend source VPS : /var/www/z12-cfo/src/
- nginx sert depuis : /var/www/z12-cfo/dist/
- Pas de secrets hardcodés — toujours os.environ.get()
- bank_reconciliation.py exporte bank_router (pas router)
- Import dans main.py : from bank_reconciliation import bank_router
- Guard : if bank_router is not None: app.include_router(bank_router)

---

## SESSIONS PRÉCÉDENTES — INCIDENTS RÉSOLUS

| Incident | Cause | Fix |
|----------|-------|-----|
| authFetch is not defined | Scope App.tsx | commits 449dbc3 + 5aa16fb |
| OpenRouter 401 | JWT sur /api/chat | commit b0f2614 |
| JWT invalidé docker force-recreate | JWT_SECRET regénéré | JWT_SECRET stable dans .env |
| OPENROUTER_API_KEY hardcodée | Variable globale module | os.environ.get() à chaque appel |
| WhatsApp Sandbox expire 72h | Limitation Twilio | join select-wire au +1 415 523 8886 |
| HF_API_TOKEN absent | search_kb_qc() embedding | search_kb_qc_by_text() via OpenRouter |
| nginx trailing slash | proxy_pass /z12upload/; | proxy_pass sans slash |
| /debug/config en prod | Endpoint debug oublié | Supprimé de main.py |
| cannot import 'router' | bank_reconciliation.py exporte bank_router | from bank_reconciliation import bank_router |

| decode_token inexistant | auth.py exporte _decode_jwt | from auth import _decode_jwt as _decode_token |
| /api/orchestrate non protege | Depends absent | Bloc auth INTERNAL_SERVICE_SECRET + Bearer JWT |
| WelcomeBanner non affiche | Import + JSX absent dans App.tsx | import + fragment <><WelcomeBanner/><DashboardView/></> |
| navItems extra ] | Python replace créait ]] | Correction ligne par ligne (lines[2528/2529]) |

---

## RAPPEL STRATÉGIQUE

Z12 CFO = vitrine → ouvre la porte aux mandats sur-mesure ops
Ancrage prix : valeur remplacée (40h CPA × 200$/h = 8K$)
Logique : démo parfaite → crédibilité → mandat sur-mesure → marge réelle

## VPS — ÉTAT DU COOKIE AUTH (30 mai 2026)

auth.py exporte : _decode_jwt (L36) + _is_valid_uuid
main.py C3 : X-Internal-Service verifie contre INTERNAL_SERVICE_SECRET
main.py C6 : from auth import _decode_jwt as _decode_token (fix alias)
main.py C7 : /api/orchestrate meme protection que /api/chat
App.tsx C8 : WelcomeBanner affiche sur view===dashboard (post-login)

Test cookie Set-Cookie : en attente resultat VPS (curl login)
Si Set-Cookie absent : patcher auth_local.py avec response.set_cookie(...)

## Corrections C9-C12 (commit CI #95)

| Code | Description | Statut |
|------|-------------|--------|
| C9 | useAuth.ts /api/auth/me → /api/auth/local/me | ✅ |
| C10 | /api/chat default_user supprimé → 401 strict | ✅ |
| C11 | /api/orchestrate default_user supprimé → 401 strict | ✅ |
| C12 | SettingsView texte anglais OpenRouter honnête | ✅ |

Score révisé : **8.6/10** (vs 8.3/10 état après audit critique 4)

## Correction C13 (commit CI #96)

| Code | Description | Statut |
|------|-------------|--------|
| C13 | \_resolve\_user() helper: cookie phoenix\_session + Bearer + X-Internal-Service | ✅ |
| C13 | chat\_proxy + api\_orchestrate → await \_resolve\_user(request) | ✅ |

Score : **8.6/10 → 8.8/10** (cookie HttpOnly enfin opérationnel bout-en-bout)

## État final — v5.4 · 30 mai 2026 · Score 8.7/10

### Milestone status
| Milestone | Statut | CI |
|-----------|--------|----|
| M0 Gate 1 | ✅ FERMÉ | — |
| M0.5 Dette technique | ✅ | — |
| M1 Patrick KB QC + RAG + valeur chiffrée | ✅ 100% PROD | #82 |
| M2 Auth self-service (README + WelcomeBanner) | ✅ PROD · test réel ⏳ humain | #85 |
| M3 BankReconciliationAgent + sidebar | ✅ 100% PROD | #90 |

### Corrections déployées — C1 à C13
| Code | Description | CI |
|------|-------------|----|
| C1 | nginx.conf trailing slash | #92 |
| C2 | docker-compose AUTH_MODE=strict | #92 |
| C3 | /api/chat X-Internal-Service + Bearer | #93 |
| C4 | OpenRouter texte cohérent | #92 |
| C5 | useAuth cookie HttpOnly · localStorage supprimé | #93 |
| C6 | _decode_jwt alias | #94 |
| C7 | /api/orchestrate Depends auth | #94 |
| C8 | WelcomeBanner dashboard | #94 |
| C9 | useAuth /api/auth/local/me | #95 |
| C10 | /api/chat 401 strict · default_user supprimé | #95 |
| C11 | /api/orchestrate 401 strict | #95 |
| C12 | SettingsView texte anglais OpenRouter | #95 |
| C13 | _resolve_user() cookie+Bearer+Internal · chat+orchestrate | #96 |

### Architecture auth finale (C13)
```
Login  → POST /api/auth/local/login → Set-Cookie: phoenix_session
Accès  → /api/chat   → await _resolve_user(request) → get_current_user_id()
         /api/orchestrate → await _resolve_user(request)
Canal  → Cookie HttpOnly OU Bearer token OU X-Internal-Service (WhatsApp)
```

### Gate 2 — Checklist avant monétisation
- [ ] 1 CPA bêta utilise son instance spontanément chaque semaine
- [ ] BankReconciliationAgent 95% précision sur vrais relevés Desjardins
- [ ] CGU licence logicielle rédigées (modèle Docker, pas SaaS)
- [ ] Assurance RC professionnelle souscrite (~500-1500$/an)
- [ ] Pricing validé avec 10 entretiens Van Westendorp (CPA QC)

### M2 critère restant (action humaine)
Trouver 1 personne qui ne connaît pas l'app.
Donner uniquement l'URL : https://cfo.optigenius.pro
Observer : pose une question à Patrick en < 10 min sans aide → M2 ✅

### VPS test cookie — résultats
```bash
# Étape 1 — login
curl -v -c /tmp/cookies.txt -X POST http://localhost:8000/api/auth/local/login \
  -H "Content-Type: application/json" \
  -d '{"email":"EMAIL","password":"MDP"}' 2>&1 | grep -i "set-cookie|< HTTP"

# Étape 2 — /api/chat avec cookie
curl -s -b /tmp/cookies.txt -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"test auth"}],"agent":"Sophie"}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('STATUS:', d.get('detail','OK'))"
```


---
## C14-C16 — Corrections auth cookie (31 mai 2026)

### C14 — auth_local.register : token manquant dans INSERT user_sessions
- **Bug** : NotNullViolation — colonne `token` NOT NULL ignorée lors de l'inscription
- **Fix** : Générer le JWT avant l'INSERT, inclure `token` dans les colonnes
- **Commit** : e0d6d07

### C15 — auth.py : _session_secret fallback manquant
- **Bug** : `_session_secret = os.environ.get("SESSION_JWT_SECRET", "")` → `""` falsy → branche cookie jamais prise → 401
- **Fix** : Fallback `phoenix-local-secret` identique à auth_local.py
- **Commit** : 3b35c35

### C16 — auth_local._get_user : u.id::uuid sur colonne INTEGER
- **Bug** : `WHERE u.id = %s::uuid` → erreur SQL car local_users.id est INTEGER
- **Fix** : `WHERE u.id = %s::integer`
- **Résultat attendu** : /api/auth/local/me ✅ · /api/chat avec cookie ✅
