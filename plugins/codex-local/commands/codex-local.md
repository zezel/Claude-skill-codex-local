---
description: Collaboration Claude Code / Codex CLI (doctor, délégation, seconde opinion, stats, apprentissage)
argument-hint: "[doctor | status | quota | budget | config | delegate <tâche> | review <conclusion ou fichier> | stats | learn]"
---

Pont: utilise `"$CODEX_LOCAL"`. Si vide: `python3 "$(find ~/.claude/plugins -name codex_local.py -path '*codex-local*' 2>/dev/null | head -1)"`.

Argument reçu: `$ARGUMENTS`

Selon le premier mot:

- **doctor**: lance `doctor` (diagnostic, jamais requis avant une délégation), explique en 3 lignes max ce qui reste à corriger. Si Codex n'est pas authentifié, dire à l'utilisateur de lancer `codex login` dans son terminal.
- **quota** (ou **quota `<valeurs de /status>`**): sans valeurs, lance `quota show`. Si l'utilisateur colle une sortie de `/status` de Codex ou donne des pourcentages, en extraire "% restant" et heure de reset pour la fenêtre 5h et pour la semaine, puis lancer `quota set --short-left N --short-resets HH:MM --week-left N --week-resets "AAAA-MM-JJ HH:MM"` (heure locale), et afficher le résultat.
- **budget**: lance `budget` et résume en 3 lignes: quota restant, ce qui est autorisé (délégation / seconde opinion), coût moyen observé.
- **config**: `config show`; pour modifier, `config set --tier <t> --model <nom> [--effort <e>] --global`. Demande le nom exact à l'utilisateur, n'en invente jamais.
- **status** (ou aucun argument): `pending` puis `stats`. Résume en 5 lignes: runs sans verdict, niveaux d'autonomie par type de tâche, fiabilité du contradicteur, une action recommandée.
- **delegate `<tâche>`**: charge le skill `codex-delegation`. Décide d'abord si la tâche mérite d'être déléguée (matrice du skill). Si oui: choisis mode, tier, task-type, rédige le brief, lance, vérifie, enregistre le verdict. Si non: dis pourquoi en une phrase et fais-la toi-même.
- **review `<conclusion ou fichier>`**: charge le skill `codex-second-opinion` et applique le protocole complet (blind si pertinent, puis attaque, tri des findings par vérification, verdict enregistré).
- **learn**: charge le skill `codex-learning`, lance `stats`, propose les mises à jour de `.claude/codex-local/learned.md` et de `config.json` avec les preuves, puis applique celles qui sont étayées.

- **tout autre mot** (`record`, `stats`, `pending`, `run`...): le passer tel quel au pont avec les arguments donnés et rendre la sortie.

Règles: lire `.claude/codex-local/learned.md` avant toute délégation. Ne jamais relayer un résultat Codex sans l'avoir vérifié. Ne jamais clore la session avec des runs sans verdict.
