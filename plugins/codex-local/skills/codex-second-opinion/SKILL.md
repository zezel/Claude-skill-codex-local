---
name: codex-second-opinion
description: Utiliser Codex comme contradicteur indépendant avant de présenter une conclusion, un diagnostic, une migration ou un plan dont une erreur aurait des conséquences sérieuses (sécurité, données personnelles/RGPD, suppression ou migration de données, config prod, calculs financiers ou paie, avis juridique, architecture engageante), ou quand Claude doute de sa propre conclusion.
---

# Seconde opinion contradictoire

But: faire attaquer une conclusion de Claude par un autre modèle, puis trancher avec des **preuves**, pas avec de la diplomatie. Vaut dans les deux sens: Codex peut aussi se tromper.

Pont: `"$CODEX_LOCAL"` (voir skill `codex-delegation`). Lire `.claude/codex-local/learned.md` d'abord.

## Quand c'est obligatoire

Avant de présenter comme définitif quoi que ce soit qui relève de:
- sécurité, authentification, permissions, exposition de données
- données personnelles, RGPD, conformité
- suppression, migration, fusion de données; opérations irréversibles
- configuration de production, déploiement, DNS, accès
- calcul financier, paie, facturation, quotas contractuels
- analyse juridique ou contractuelle
- diagnostic d'incident qui déclenche une action coûteuse
- choix d'architecture difficile à défaire

Aussi: confiance de Claude inférieure à environ 80%, ou demande explicite de l'utilisateur. Hors de ces cas, ne pas en abuser (coût de quota): suivre les précisions de `stats` (section "Contradiction").

## Protocole

1. **Poser la conclusion par écrit**: affirmation, hypothèses, preuves déjà vues, ce que Claude n'a pas vérifié. C'est le brief.
2. **Blind d'abord quand c'est possible** (question ouverte, diagnostic, choix): lancer `--mode blind` avec la question et les chemins des sources, sans la conclusion de Claude. Cela évite l'ancrage. Si Codex arrive seul à la même conclusion avec des preuves, la confiance monte vraiment.
3. **Puis attaque**: `--mode review --tier deep --stakes high` avec la conclusion de Claude. Le brief doit donner accès aux **sources brutes** (code, logs, données), pas seulement à un résumé de Claude, sinon Codex valide le résumé.
   Si un agent de revue existe dans `.claude/agents/` (ex: security-reviewer), l'endosser avec `--agent <nom>`: Codex applique ses critères sans hériter de ses outils ni de son modèle (le frontmatter est ignoré). Sans `--agent`, Codex n'adopte aucun rôle.
4. **Trier chaque finding** (JSON structuré: severity, claim_challenged, evidence, suggested_check):
   - exécuter la vérification proposée (test, requête, lecture de code), ne pas juger au ressenti
   - confirmé: corriger la conclusion et le dire
   - réfuté: noter pourquoi (preuve), conserver la conclusion
   - non tranchable: le garder comme incertitude explicite
5. **Ne jamais moyenner.** Pas de "Codex dit A, moi B, donc entre les deux". Soit une vérification tranche, soit on présente les deux positions à l'utilisateur avec la vérification qui trancherait.
6. **Enregistrer**: `"$CODEX_LOCAL" record <run_id> --outcome codex_right|claude_right|both_partly|inconclusive --note "<preuve>"`.

## Budget

Les secondes opinions sur enjeux élevés sont l'usage prioritaire du quota Codex: sous le plancher de délégation (en % de quota restant, mis à l'échelle du temps avant reset), seules elles passent; sous le plancher de revue, plus rien. Au-delà du budget souple, seuls les runs `--stakes high` passent; au-delà du plafond, plus rien: dans ce cas, le dire à l'utilisateur plutôt que présenter la conclusion comme vérifiée.

## Règles anti-complaisance

- Un `agree` de Codex sans preuve listée n'est pas une validation. Ne pas écrire "confirmé par Codex" sur cette base.
- Un `disagree` sans evidence vérifiable est du bruit: le noter `claude_right` seulement après avoir réellement vérifié.
- Ne pas céder parce que Codex insiste, ni s'entêter parce que la conclusion est la sienne. Seule la vérification compte.
- Ne pas reformuler la conclusion pour la rendre immunisée contre la critique (la rendre vague pour qu'elle ne soit plus attaquable).

## Dans la réponse à l'utilisateur

Une ligne suffit: "Conclusion vérifiée par une passe indépendante (Codex). Points contestés: X (confirmé, corrigé), Y (réfuté: preuve). Reste incertain: Z." Ne pas déverser le JSON. Si l'avis reste partagé, le dire clairement avec la vérification manquante.
