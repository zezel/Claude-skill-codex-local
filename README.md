# codex-local

Plugin Claude Code. Claude apprend à déléguer à Codex CLI, à s'en servir comme contradicteur sur les décisions à enjeux, et à calibrer la confiance mutuelle à partir de verdicts enregistrés.

## Installation

Prérequis: Codex CLI installé et authentifié (`codex login`), Python 3.8+.

```bash
# depuis le dossier qui contient ce README
claude plugin marketplace add ./codex-local-marketplace
claude plugin install codex-local@local-tools
```
Pour tester sans installer: `claude --plugin-dir ./codex-local-marketplace/plugins/codex-local`.

Rien d'autre: le comportement est actif dès l'installation (hook + skills) et chaque projet s'initialise au premier `run`. `/codex-local doctor` sert uniquement au diagnostic.

## Ce que ça installe

| Élément | Rôle |
|---|---|
| Hook SessionStart | expose `"$CODEX_LOCAL"`, l'état de Codex, les runs sans verdict |
| Skill `codex-delegation` | quoi déléguer, quel mode, quel tier, comment briefer et vérifier, discipline de quota |
| Skill `codex-second-opinion` | protocole de contradiction (blind puis attaque), obligatoire sur enjeux élevés |
| Skill `codex-learning` | verdicts, promotion/rétrogradation d'autonomie, règles apprises |
| `/codex-local` | `doctor`, `status`, `delegate`, `review`, `learn` |
| `scripts/codex_local.py` | pont: `run`, `record`, `stats`, `pending`, `doctor` |

Les skills se déclenchent seuls selon leur description: Claude n'a pas besoin de la commande pour déléguer.

## Partage de la base de connaissances `.claude`

Point à savoir: Codex ne lit pas `CLAUDE.md` ni `.claude/` par défaut, il lit `AGENTS.md`. Le plugin contourne sans toucher à `~/.codex/config.toml`:

1. Chaque run passe `-c project_doc_fallback_filenames=["CLAUDE.md"]`: Codex charge `CLAUDE.md` là où il n'y a pas d'`AGENTS.md` dans le même dossier. Si les deux existent dans un dossier, `AGENTS.md` gagne.
2. Chaque brief liste les fichiers utiles qui existent: `CLAUDE.md`, `CLAUDE.local.md`, `~/.claude/CLAUDE.md`, `.claude/rules/**`, `.claude/skills/*/SKILL.md`, `.claude/codex-local/learned.md`, avec l'ordre de les lire. Les `@imports` de CLAUDE.md ne sont pas expansés par Codex, le brief le lui rappelle.
3. Les agents `.claude/agents/*.md` (projet et `~/.claude/agents`) sont listés comme définitions de rôle. Codex ne les exécute pas: il lit leurs consignes et ne les endosse que si le brief les nomme (`run --agent <nom>`). Leur frontmatter (`tools`, `model`) est ignoré et n'élargit jamais le sandbox. Si ton `.agents` côté Codex pointe déjà vers `.claude`, les listes du brief font doublon sans gêner.
4. Le ledger, `learned.md` et les résultats vivent dans `.claude/codex-local/`: Codex peut les lire, seul Claude écrit `learned.md`.

Limite de taille: Codex plafonne les docs d'instructions (32 KiB par défaut, `project_doc_max_bytes`). Un CLAUDE.md volumineux sera tronqué côté Codex.

## Garde-fous

- Jamais `--yolo`. Lecture seule par défaut; écriture = `workspace-write`, refusée hors dépôt git.
- Écriture: arbre propre exigé, ou `--worktree` (branche `codex/<id>` isolée, niveau L2 par défaut).
- Brief refusé s'il contient un motif de secret (clés AWS/OpenAI/GitHub/Slack, clé privée, `password=`...).
- Le pont signale tout fichier sensible touché (`.env*`, `.claude/settings*`, workflows CI, clés).
- Codex n'a pas l'instruction de commit ni de push.

## Quota Codex (garde-fou principal)

`/status` dans Codex donne le quota en pourcentage restant (fenêtre 5h et hebdomadaire, avec l'heure de reset), pas en tokens. Le pont raisonne donc en pourcentage:
- **Lecture automatique** après chaque run, si Codex expose `rate_limits` dans ses événements ou dans son fichier de session (`~/.codex/sessions/`). À confirmer sur ta version (voir ci-dessous).
- **Saisie manuelle** sinon, depuis `/status`:
  ```bash
  "$CODEX_LOCAL" quota set --short-left 89 --short-resets 23:16 --week-left 68 --week-resets "2026-11-19 11:06"
  "$CODEX_LOCAL" quota show
  ```
- **Planchers** (modifiables: `config set --budget week_delegate_floor=50 --global`), mis à l'échelle du temps restant avant le reset: sous `*_delegate_floor` (défauts: 5h 40%, semaine 50%), plus de délégation ordinaire; sous `*_review_floor` (10% et 15%), plus rien. Entre les deux, seules les secondes opinions à enjeux élevés passent.
- Un relevé périmé (fenêtre déjà réinitialisée) est ignoré.
- `quota show` estime aussi, de façon approximative, combien de tokens du pont correspondent à 1% de quota hebdo.

Vérifier si la lecture automatique marche chez toi:
```bash
grep -rhoE '"rate_limits".{0,300}' ~/.codex/sessions | tail -1
```

Les limites en tokens (`short_soft`, `week_soft`, etc., via `config set --budget`) restent possibles mais facultatives.

## Niveaux d'autonomie

L1 lecture seule, L2 écriture en worktree avec relecture complète, L3 écriture directe avec relecture du diff. Un type de tâche passe L2 vers L3 à 5 verdicts, 80% acceptés, sans rejet récent ni rejet sur enjeu élevé. Calcul dans `stats`, pas au feeling.

## À régler après installation

- Modèles par tier, une fois pour tous les projets (ou demander à Claude: `/codex-local config`):
  ```bash
  python3 <chemin>/codex_local.py config set --tier fast --model <nom> --effort low --global
  python3 <chemin>/codex_local.py config set --tier standard --model <nom> --effort medium --global
  python3 <chemin>/codex_local.py config set --tier deep --model <nom> --effort high --global
  ```
  Écrit dans `~/.claude/codex-local/config.json`. Un projet peut surcharger avec la même commande sans `--global`. Priorité: défauts < global < projet. Les noms changent souvent, je ne les ai pas figés.
- Ajouter `.claude/codex-local/` à ton `.gitignore` si tu ne veux pas versionner le ledger (le sous-dossier `runs/` est déjà ignoré).

## Points non vérifiés (à tester une fois avec le vrai Codex)

Je n'ai pas pu lancer le vrai Codex dans mon environnement, les tests ont utilisé un faux binaire qui imite l'interface. À confirmer chez toi:
- `codex login status` (affiché dans `doctor`, ignoré s'il échoue).
- Reprise `--resume`: l'identifiant de session est lu dans les événements JSON (`thread_id`); à défaut le pont utilise `--last`.
- Le prompt est passé sur stdin avec `-`, y compris pour `codex exec resume`.
- `--output-schema` exige un schéma strict (fourni dans `schemas/review.json`).
