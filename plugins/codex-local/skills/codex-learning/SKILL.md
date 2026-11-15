---
name: codex-learning
description: Boucle d'apprentissage de la collaboration Claude/Codex. À utiliser après chaque run Codex (enregistrer le verdict), quand `stats` montre un seuil atteint, ou pour mettre à jour les règles apprises (learned.md), les niveaux d'autonomie et le choix des modèles par type de tâche.
---

# Apprendre à travailler avec Codex

La confiance se construit sur des verdicts enregistrés, pas sur des impressions. Tout vit dans `.claude/codex-local/` (que Codex peut lire aussi):

| Fichier | Rôle |
|---|---|
| `ledger.jsonl` | un événement par run et par verdict (append-only, ne pas éditer à la main) |
| `runs/<id>/` | brief, résultat, événements, stderr (ignoré par git) |
| `config.json` | modèle et effort par tier, timeout |
| `learned.md` | règles distillées par Claude, avec preuves |

## Boucle

0. **Choisir le bon verdict**: `accepted` (utilisé tel quel), `modified` (utilisable après correction), `rejected` (faux ou inutilisable), `incomplete` (abstention honnête de Codex, neutre pour la confiance, signal pour le tier ou le brief). Un verdict déjà enregistré se corrige par un nouveau `record` (le dernier l'emporte).
1. **Après chaque run**: vérifier, puis `"$CODEX_LOCAL" record <id> --outcome ... --note "..."`. Une note d'une ligne sur le pourquoi vaut mieux que le verdict seul. En fin de session, `"$CODEX_LOCAL" pending` doit être vide.
2. **Tous les 10 verdicts ou en fin de gros chantier**: `"$CODEX_LOCAL" stats`, puis mettre à jour `learned.md`.
3. **Promotion/rétrogradation**: appliquer le niveau recommandé par `stats` (L2 vers L3 à 5 verdicts, 80% acceptés, aucun rejet récent). Jamais de promotion au feeling. Après un rejet, redescendre ce type de tâche en L2 et noter la cause.
4. **Retour de Codex sur les briefs**: chaque réponse se termine par "Brief feedback". Si le même manque revient deux fois, l'intégrer au template de brief dans `learned.md`.
5. **Choix des modèles**: quand un tier est insuffisant ou superflu, le noter. Pour changer de modèle, éditer `config.json` (nom exact) et noter la date et la raison dans `learned.md`, section Modèles.

## Écrire une règle dans learned.md

Format: une ligne, vérifiable, avec sa preuve.
```
- [tests] Codex tier standard suffit pour tests unitaires Python; 6/6 acceptés (runs 20261115-1042-a3f9 ...). Piège: oublie les fixtures partagées, les citer dans le brief.
```
Règles de tenue:
- remplacer les lignes `PRIOR` dès qu'une preuve les confirme ou les contredit
- une règle sans preuve reste marquée `PRIOR`
- garder le fichier sous 60 lignes: fusionner, supprimer le périmé
- ne pas y mettre d'opinion sur Codex en général, seulement ce qui change une décision (quoi déléguer, quel tier, quel piège de brief)

## Ce qui doit être appris, concrètement

- Quels `--task-type` Codex réussit sans retouche, et à quel tier.
- Quels types de conclusions de Claude Codex corrige (`codex_right`) et lesquels il conteste à tort (`claude_right`): cela règle la fréquence des seconds avis.
- Les angles morts de Claude que Codex détecte, pour les vérifier soi-même en amont.
- Le coût réel (tokens et durée par mode) pour décider si une délégation en valait la peine.

## Ce qu'on ne fait pas

- Modifier le ledger pour "améliorer" un verdict.
- Promouvoir un niveau d'autonomie sur la base de runs non vérifiés.
- Laisser Codex écrire seul dans `learned.md`: il peut proposer, Claude valide avec les preuves.
