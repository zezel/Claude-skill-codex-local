---
name: codex-delegation
description: Décider si, quoi et comment déléguer du travail à Codex CLI pour répartir les quotas. À utiliser avant un gros travail de lecture ou d'exploration, une tâche mécanique volumineuse (tests, refactor, migration, doc depuis le code), ou quand le quota Claude est tendu. Couvre le choix du mode, du tier de modèle, la rédaction du brief et la vérification du résultat.
---

# Déléguer à Codex

Codex est un collègue avec ses propres quotas et un autre modèle, pas un exécutant aveugle. Deux usages distincts:
1. **Déléguer** pour économiser le quota Claude et paralléliser (ce skill).
2. **Contredire** pour fiabiliser une conclusion (skill `codex-second-opinion`).

Pont d'exécution: `"$CODEX_LOCAL"` (défini au démarrage de session). Si la variable est vide (hook non exécuté), localiser le script: `python3 "$(find ~/.claude/plugins -name codex_local.py -path '*codex-local*' 2>/dev/null | head -1)"`. Aucune initialisation requise: `run` prépare `.claude/codex-local/` au premier usage. En cas de souci: `"$CODEX_LOCAL" doctor`.

Avant toute délégation, lire `.claude/codex-local/learned.md` (règles apprises sur ce projet). Elles priment sur ce skill.

## 1. Déléguer ou garder ?

Déléguer quand la tâche est **bien bornée, vérifiable à bas coût, et gourmande en tokens**:
- exploration large: inventaire d'un code, cartographie de dépendances, recherche de tous les usages
- génération ou complétion de tests, avec commande de test comme critère
- refactor ou migration mécanique répétitif
- documentation dérivée du code, résumé de gros diffs ou de gros logs
- scripts de transformation de données avec échantillon de sortie attendu
- première passe de revue sur un gros diff

Garder quand:
- la décision engage l'architecture ou le produit (Claude propose, Codex peut contredire, pas décider)
- le contexte utile vit dans la conversation et n'est pas écrit nulle part
- la spec est floue: clarifier d'abord avec l'utilisateur
- la tâche touche secrets, accès prod, credentials, données personnelles brutes
- la tâche prendrait moins de temps à faire qu'à briefer (règle: si le brief dépasse 10 lignes pour un résultat de 20 lignes, ne pas déléguer)

## 2. Mode et niveau d'autonomie

| Mode | Sandbox | Usage |
|---|---|---|
| `read` | read-only | analyse, inventaire, rapport |
| `write` | workspace-write | modifications de fichiers |
| `review` | read-only + schéma JSON | attaque d'une conclusion (voir second-opinion) |
| `blind` | read-only | conclusion indépendante sans voir celle de Claude |

Niveaux de confiance par type de tâche (calculés par `"$CODEX_LOCAL" stats`):
- **L1**: lecture seule uniquement.
- **L2** (défaut en écriture): `--worktree`, Claude relit tout le diff avant intégration.
- **L3**: écriture directe sur arbre propre; Claude relit le `diff --stat`, les fichiers sensibles et lance les tests.
- Promotion L2 vers L3 uniquement quand `stats` la recommande. Rétrogradation immédiate après un rejet.
- Un type de tâche jamais vu démarre toujours à L2, même si Codex semble très bon.

## 3. Choix du tier (modèle et effort)

Les noms exacts de modèles se lisent avec `"$CODEX_LOCAL" config show` (ne pas supposer de noms, ils changent). Pas de modèle défini = défaut de Codex.
- `fast`: inventaire, grep sémantique, doc, résumés.
- `standard`: code ordinaire, tests, refactor.
- `deep`: sécurité, migration de données, bugs subtils, toute contradiction sur enjeu élevé.

Monter d'un tier seulement si le tier inférieur a échoué ou si les enjeux l'exigent. Noter dans `learned.md` quel tier a suffi pour quel type de tâche.

## 4. Écrire le brief

Un brief = ce qu'un collègue compétent mais absent de la conversation doit savoir. Template:

```
OBJECTIF: une phrase.
CONTEXTE: où regarder (chemins), décisions déjà prises, pourquoi.
CONTRAINTES: conventions à respecter, ce qu'il ne faut pas changer.
HORS PÉRIMÈTRE: ce qu'il ne faut pas toucher.
CRITÈRE D'ACCEPTATION: commande ou observation vérifiable (ex: `pytest tests/x` passe, 0 usage restant de foo()).
  Pour un inventaire ou une lecture de fichiers: donner les tailles réelles (`wc -l`), exiger la lecture intégrale, et un critère de couverture vérifiable (la dernière entrée de chaque fichier doit pointer près de sa dernière ligne). Observé: sans cela, le tier fast a lu des fichiers tronqués et s'est abstenu; avec cela, le tier standard a couvert les 4 fichiers.
FORMAT DE SORTIE: liste, tableau, JSON, diff.
```

Le pont ajoute automatiquement la liste des fichiers de la base de connaissances partagée (CLAUDE.md, `.claude/rules`, skills, learned.md). Codex ne lit pas `.claude/` tout seul: si une règle précise du projet est critique pour la tâche, la citer dans le brief. Les agents Claude (`.claude/agents/*.md`) sont listés comme définitions de rôle, que Codex n'endosse que si le brief les nomme: utiliser `--agent <nom>`. Ce ne sont pas des agents exécutables côté Codex, seulement leurs consignes. Jamais de secret dans le brief (le pont refuse les motifs évidents): référencer le fichier.

Lancement:
```bash
"$CODEX_LOCAL" run --mode write --tier standard --task-type tests --worktree --brief - <<'EOF'
OBJECTIF: ...
EOF
```
Plusieurs tâches indépendantes: lancer en arrière-plan (`run_in_background`), 3 en parallèle maximum, et en `--worktree` distincts pour l'écriture.

## 5. Après réception: vérifier avant de relayer

Rien de ce que dit Codex n'est un fait tant que Claude ne l'a pas vérifié à hauteur des enjeux:
- `read`: re-vérifier 2 ou 3 affirmations clés au hasard dans les sources (file:line). Pour les numéros de ligne, contrôler aussi un titre de section: une citation peut pointer le contenu plutôt que le titre, à un ou deux numéros près. Une seule fausse = traiter tout le rapport comme suspect.
- `write`: lire le diff, lancer les tests soi-même (ne pas croire "tests passent"), chercher les fichiers sensibles touchés (le pont les signale).
- Si le résultat contredit ce que Claude savait: ne pas arbitrer au feeling, trouver la vérification qui tranche.

Puis **toujours** enregistrer: `"$CODEX_LOCAL" record <run_id> --outcome accepted|modified|rejected|incomplete --note "..."`

Distinguer `rejected` (résultat faux ou inutilisable) de `incomplete` (Codex dit honnêtement qu'il n'a pas pu finir et refuse de deviner). L'abstention est un comportement à encourager: ne pas la punir comme une erreur. Corriger la cause (tier trop faible, brief trop large, lecture tronquée) puis relancer.. Sans verdict, rien ne s'apprend.

## 6. Discipline de quota

Le quota Codex est une ressource partagée, pas un puits sans fond: l'objectif est que Codex reste disponible, en priorité pour les secondes opinions à enjeux élevés. Avant toute délégation:
1. `"$CODEX_LOCAL" budget`: quota Codex réel en % (fenêtre 5h et hebdo, avec leurs resets), conso du pont, coût moyen observé pour ce type de tâche. Si aucun relevé valide: demander à l'utilisateur de lancer `/status` dans Codex et enregistrer les pourcentages avec `quota set` (la sortie de `budget` donne la commande). Le pourcentage restant fait foi, pas le décompte de tokens du pont, qui ne voit ni l'usage direct de Codex ni celui des autres personnes du même compte.
2. Estimer le coût. Sans moyenne observée, supposer cher: un agent qui lit des fichiers renvoie son contexte à chaque tour (observé: environ 400k tokens d'entrée, cache inclus, pour lire 1 400 lignes en tier standard, soit 8 fois plus que l'essai fast qui a échoué).
3. Ne pas déléguer si: le quota ou le budget de délégation est atteint (le pont refuse avec exit 3: ne pas contourner, `--override-budget` seulement avec l'accord explicite de l'utilisateur), si la tâche est un inventaire ou un résumé d'un petit corpus (moins de 2 000 lignes) que Claude lit lui-même pour une fraction du coût, ou si le coût estimé dépasse environ le quart du budget courant sans enjeux élevés.
4. Réserver Codex aux cas où il apporte quelque chose que Claude ne fait pas aussi bien ou aussi vite: gros volumes, travail parallèle, regard indépendant. Tant qu'aucune limite n'est définie (`budget` le signale), prévenir l'utilisateur au lieu de déléguer en volume.

- Max 2 itérations par tâche (`--resume <run_id>` pour continuer la session). Au-delà, reprendre la main ou revoir le brief.
- Erreur de quota ou d'auth côté Codex: ne pas boucler. Reprendre soi-même la partie critique, ou reporter, et le dire à l'utilisateur.
- Ne pas déléguer par réflexe: chaque délégation coûte un brief et une vérification côté Claude.
- Le résumé que Claude lit est celui du pont (`result.md`), pas le flux d'événements complet: ne pas ouvrir `events.jsonl` sauf débogage.

## 7. Garde-fous non négociables

- Jamais `--yolo` ni `--dangerously-bypass-approvals-and-sandbox`.
- Codex ne commit pas, ne push pas, ne touche pas `.env*`, `.claude/settings*`, workflows CI.
- Pas de délégation d'actions irréversibles ou externes (envoi de mails, appels API d'écriture, suppression de données).
- Si le pont signale des fichiers sensibles touchés: stop, relire, informer l'utilisateur.
