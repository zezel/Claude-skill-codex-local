#!/usr/bin/env python3
"""
codex_local.py - Pont Claude Code <-> Codex CLI (plugin codex-local)

Sous-commandes
  quota         Quota Codex réel en % (lu automatiquement ou saisi depuis /status), planchers\n  budget        Conso Codex, limites, coût moyen par type de tâche (protège le quota Codex)\n  config        Voir/régler modèle et effort par tier (--global = tous les projets)
  doctor        Diagnostic: Codex, auth, CLAUDE.md, taille, agents (aucune étape requise avant `run`)
  run           Lance `codex exec` (brief, sandbox, tier, journal) et rend un résultat compact
  record        Enregistre le verdict de Claude sur un run (c'est ce qui fait "apprendre")
  stats         Réussite par type de tâche, niveau d'autonomie recommandé, fiabilité du contradicteur
  pending       Liste les runs sans verdict
  hook-session  (interne) contexte injecté au démarrage d'une session Claude Code

Principes
  - Jamais de --yolo ni de bypass du sandbox. read-only par défaut, workspace-write sur demande.
  - Codex ne lit pas CLAUDE.md par défaut: on passe project_doc_fallback_filenames en override
    `-c` (aucune modif de ~/.codex/config.toml) et chaque brief liste les fichiers .claude utiles.
  - Init automatique: `run` crée <projet>/.claude/codex-local/ au premier usage.
  - Tout est journalisé dans <projet>/.claude/codex-local/ (lisible aussi par Codex).

Dépendances: Python 3.8+, stdlib uniquement. Codex CLI dans le PATH.
Maintenance: les flags Codex utilisés sont isolés dans build_codex_cmd().
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REVIEW_SCHEMA = PLUGIN_ROOT / "schemas" / "review.json"

DEFAULT_CONFIG = {
    # model=None: hérite du modèle défini dans ~/.codex/config.toml.
    # Renseigner ici les noms exacts de modèles une fois choisis (voir learned.md).
    "tiers": {
        "fast": {"model": None, "effort": "low"},
        "standard": {"model": None, "effort": "medium"},
        "deep": {"model": None, "effort": "high"},
    },
    "fallback_docs": ["CLAUDE.md"],
    # Budget en tokens "effectifs" (entrée hors cache + sortie), compté sur les runs faits via le pont,
    # tous projets confondus (~/.claude/codex-local/usage.jsonl). None = pas de limite.
    # soft: conso à ne pas dépasser sauf enjeux élevés. hard: plafond absolu.
    # reserve_pct: part du soft gardée pour les secondes opinions (review/blind) afin que les
    # délégations ordinaires ne vident pas ce qui sert à contredire.
    "budget": {"short_hours": 5, "short_soft": None, "short_hard": None,
               "week_soft": None, "week_hard": None, "reserve_pct": 30,
               # Planchers en % de quota RESTANT (valeurs de /status de Codex). Mis à l'échelle du temps
               # restant avant reset: à mi-fenêtre, un plancher de 50% vaut 25%. Sous le plancher
               # "delegate": plus de délégation ordinaire. Sous "review": plus rien, même enjeux élevés.
               "short_delegate_floor": 40, "short_review_floor": 10,
               "week_delegate_floor": 50, "week_review_floor": 15},
    "timeout_s": 1800,
    "max_parallel": 3,
}

OUTCOMES = {
    # délégation
    "accepted": "résultat utilisé tel quel (après vérification)",
    "modified": "utilisable après corrections de Claude",
    "rejected": "inutilisable ou faux",
    "incomplete": "Codex signale honnêtement ne pas pouvoir finir (abstention): pas un rejet de fiabilité, sert au choix du tier et du brief",
    # contradiction
    "codex_right": "objection de Codex confirmée, Claude a changé sa conclusion",
    "claude_right": "objection vérifiée et fausse, conclusion de Claude maintenue",
    "both_partly": "chacun avait une part de raison",
    "inconclusive": "non tranché (à remonter à l'utilisateur)",
}

SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",
    r"sk-[A-Za-z0-9_-]{20,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}",
]

SENSITIVE_TOUCHED = re.compile(
    r"(^|/)(\.env[^/]*|\.claude/settings[^/]*|\.github/workflows/|\.git/hooks/|id_rsa|.*\.pem|.*\.key)"
)

LEARNED_TEMPLATE = """# Règles apprises (codex-local)

Fichier maintenu par Claude Code, lisible par Codex. Chaque règle cite ses preuves (ids de runs).
Les lignes marquées PRIOR sont des hypothèses de départ, pas des faits: les remplacer dès qu'un
ledger les confirme ou les contredit (`/codex-local stats`). Garder ce fichier sous 60 lignes.

## Délégation (quoi, quel tier)
- PRIOR: inventaire, exploration large, doc depuis le code -> tier fast, mode read.
- PRIOR: génération de tests, refactor mécanique -> tier standard, mode write en worktree.
- PRIOR: revue contradictoire, sécurité, migration de données -> tier deep, mode review.

## Pièges de brief
- PRIOR: toujours fournir un critère d'acceptation vérifiable (commande de test, comparaison de sortie).

## Fiabilité observée
- (vide: se remplit avec `stats`)

## Modèles
- (noter ici les noms de modèles retenus par tier dans config.json et pourquoi)
"""

# --------------------------------------------------------------------------- utilitaires


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sh(cmd, cwd=None, timeout=30):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def project_root(start=None):
    cwd = Path(start or os.getcwd())
    r = sh(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if r and r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip()), True
    return cwd, False


def data_dir(root):
    return root / ".claude" / "codex-local"


def global_config_path():
    return Path.home() / ".claude" / "codex-local" / "config.json"


def _merge_cfg(cfg, user):
    """Fusionne user dans cfg. Une valeur None ne remplace jamais: None = hériter."""
    for k, v in user.items():
        if k == "tiers" and isinstance(v, dict):
            for t, tv in v.items():
                if isinstance(tv, dict):
                    cur = cfg["tiers"].setdefault(t, {})
                    cur.update({kk: vv for kk, vv in tv.items() if vv is not None})
        elif k == "budget" and isinstance(v, dict):
            cfg["budget"].update({kk: vv for kk, vv in v.items() if vv is not None})
        elif v is not None:
            cfg[k] = v


def load_config(root, with_sources=False):
    """Priorité: défauts < ~/.claude/codex-local/config.json (global) < <projet>/.claude/codex-local/config.json."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    sources = []
    for label, p in (("global", global_config_path()), ("projet", data_dir(root) / "config.json")):
        if p.exists():
            try:
                _merge_cfg(cfg, json.loads(p.read_text(encoding="utf-8")))
                sources.append(f"{label}: {p}")
            except json.JSONDecodeError as e:
                print(f"[codex-local] {p} invalide ({e}), ignoré", file=sys.stderr)
    return (cfg, sources) if with_sources else cfg


def usage_path():
    return Path.home() / ".claude" / "codex-local" / "usage.jsonl"


def eff_tokens(tokens):
    """Tokens effectifs: entrée hors cache + sortie. Approximation, pas le décompte réel d'OpenAI."""
    return max(0, tokens.get("input", 0) - tokens.get("cached", 0)) + tokens.get("output", 0)


def usage_append(row):
    p = usage_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def usage_rows():
    p = usage_path()
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def usage_since(rows, hours):
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    tot = 0
    for r in rows:
        try:
            if datetime.fromisoformat(r["ts"]).timestamp() >= cutoff:
                tot += r.get("eff", 0)
        except (KeyError, ValueError):
            pass
    return tot


def budget_state(cfg):
    b = cfg["budget"]
    rows = usage_rows()
    return {"short": usage_since(rows, b["short_hours"]), "week": usage_since(rows, 168), "rows": rows, "b": b}


def budget_check(mode, stakes, cfg):
    """(ok, message). Délégation (read/write) bridée à soft*(1-reserve); review/blind jusqu'au soft;
    au-delà du soft seulement stakes=high; au-delà du hard plus rien."""
    st = budget_state(cfg)
    b = st["b"]
    reserve = (b.get("reserve_pct") or 0) / 100
    for name, used, soft, hard in (
            (f"{b['short_hours']}h", st["short"], b.get("short_soft"), b.get("short_hard")),
            ("semaine", st["week"], b.get("week_soft"), b.get("week_hard"))):
        if hard and used >= hard:
            return False, f"plafond Codex atteint ({name}: {used} >= {hard} tokens)"
        if soft:
            if mode in ("read", "write") and used >= soft * (1 - reserve):
                return False, (f"budget de délégation atteint ({name}: {used} >= {int(soft * (1 - reserve))}, "
                               f"{int(reserve * 100)}% du budget est gardé pour les secondes opinions)")
            if used >= soft and stakes != "high":
                return False, f"budget Codex atteint ({name}: {used} >= {soft}), réservé aux enjeux élevés"
    return True, ""


def quota_path():
    return Path.home() / ".claude" / "codex-local" / "quota.jsonl"


def quota_append(row):
    p = quota_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def quota_rows():
    p = quota_path()
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def to_ts(x):
    """epoch (s ou ms) ou ISO -> epoch secondes, sinon None."""
    if isinstance(x, (int, float)):
        return float(x) / 1000 if x > 1e12 else float(x)
    if isinstance(x, str):
        try:
            d = datetime.fromisoformat(x.replace("Z", "+00:00"))
            return (d if d.tzinfo else d.astimezone()).timestamp()
        except ValueError:
            return None
    return None


def parse_when(txt):
    """'2026-11-19 11:06' ou '2026-11-19T11:06' (heure locale), ou 'HH:MM' = prochaine occurrence."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", txt.strip())
    if m:
        now = datetime.now().astimezone()
        d = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if d <= now:
            d = d + timedelta(days=1)
        return d.timestamp()
    return to_ts(txt.strip().replace(" ", "T"))


def _find_key(obj, key):
    if isinstance(obj, dict):
        if isinstance(obj.get(key), dict):
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r:
                return r
    return None


def read_rate_limits(path):
    """Dernier objet `rate_limits` trouvé dans un fichier JSONL (événements exec ou rollout de session)."""
    last = None
    if path and Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if '"rate_limits"' not in line:
                continue
            try:
                rl = _find_key(json.loads(line), "rate_limits")
            except json.JSONDecodeError:
                continue
            if rl:
                last = rl
    return last


def snapshot_from_rl(rl):
    out = {}
    for k in ("primary", "secondary"):
        w = rl.get(k)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        mins = w.get("window_minutes")
        kind = "short" if (mins and mins <= 720) else "week" if (mins and mins >= 5000) else (
            "short" if k == "primary" else "week")
        ts = to_ts(w.get("resets_at"))
        if ts is None and w.get("resets_in_seconds") is not None:
            ts = time.time() + float(w["resets_in_seconds"])
        out[kind] = {"left": round(100 - float(w["used_percent"]), 1), "resets": ts}
    return out or None


def find_session_file(thread_id):
    base = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    if thread_id and base.is_dir():
        found = sorted(base.rglob(f"*{thread_id}*.jsonl"))
        return found[0] if found else None
    return None


def quota_latest():
    rows = quota_rows()
    return rows[-1] if rows else None


def quota_windows(cfg, snap=None):
    """Fenêtres valides du dernier relevé, avec plancher mis à l'échelle du temps restant."""
    snap = snap or quota_latest()
    if not snap:
        return []
    b = cfg["budget"]
    now = time.time()
    snap_ts = to_ts(snap.get("ts")) or now
    res = []
    for kind, label, secs, dfl, rfl in (
            ("short", f"{b['short_hours']}h", b["short_hours"] * 3600, b["short_delegate_floor"], b["short_review_floor"]),
            ("week", "semaine", 7 * 86400, b["week_delegate_floor"], b["week_review_floor"])):
        w = snap.get(kind)
        if not w:
            continue
        resets = w.get("resets")
        if resets is not None and resets <= now:
            continue  # fenêtre déjà réinitialisée depuis le relevé: information périmée
        if resets is None and now - snap_ts > 6 * 3600:
            continue
        frac = min(1.0, max(0.0, (resets - now) / secs)) if resets else 1.0
        res.append({"kind": kind, "label": label, "left": w["left"], "resets": resets,
                    "delegate_floor": dfl * frac, "review_floor": rfl * frac})
    return res


def quota_check(mode, stakes, cfg):
    """Sous le plancher de délégation: seules les secondes opinions à enjeux élevés passent.
    Sous le plancher de revue: plus rien."""
    for w in quota_windows(cfg):
        if w["left"] < w["review_floor"]:
            return False, f"quota Codex {w['label']}: {w['left']}% restant, sous le plancher de {w['review_floor']:.0f}%"
        if w["left"] < w["delegate_floor"] and not (mode in ("review", "blind") and stakes == "high"):
            return False, (f"quota Codex {w['label']}: {w['left']}% restant, sous le plancher de délégation "
                           f"{w['delegate_floor']:.0f}% (réservé aux secondes opinions à enjeux élevés)")
    return True, ""


def quota_lines(cfg):
    ws = quota_windows(cfg)
    if not ws:
        return ["Quota Codex réel: aucun relevé valide. Demander à l'utilisateur de lancer /status dans Codex et "
                "d'enregistrer: `\"$CODEX_LOCAL\" quota set --short-left N --short-resets HH:MM --week-left N "
                "--week-resets \"AAAA-MM-JJ HH:MM\"` (ou attendre un run: lecture automatique si Codex l'expose)."]
    out = []
    for w in ws:
        r = datetime.fromtimestamp(w["resets"]).strftime("%d/%m %H:%M") if w["resets"] else "?"
        out.append(f"Quota Codex {w['label']}: {w['left']}% restant (reset {r}), planchers mis à l'échelle: "
                   f"délégation {w['delegate_floor']:.0f}%, secondes opinions {w['review_floor']:.0f}%")
    return out


def ledger_path(root):
    return data_dir(root) / "ledger.jsonl"


def ledger_append(root, event):
    p = ledger_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def ledger_runs(root):
    """Fusionne événements run + outcome en une entrée par run."""
    p = ledger_path(root)
    runs = {}
    if not p.exists():
        return runs
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "run":
            runs[e["id"]] = dict(e)
        elif e.get("type") == "outcome" and e.get("id") in runs:
            runs[e["id"]]["outcome"] = e["outcome"]
            runs[e["id"]]["note"] = e.get("note", "")
    return runs


def rel(p, root):
    try:
        return str(Path(p).relative_to(root))
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------- prompt


def knowledge_pointers(root):
    cands = [root / "CLAUDE.md", root / "CLAUDE.local.md", root / "AGENTS.md",
             Path.home() / ".claude" / "CLAUDE.md"]
    rules = root / ".claude" / "rules"
    if rules.is_dir():
        cands += sorted(rules.rglob("*.md"))
    skills = root / ".claude" / "skills"
    if skills.is_dir():
        cands += sorted(skills.glob("*/SKILL.md"))
    cands.append(data_dir(root) / "learned.md")
    return [str(p) if not str(p).startswith(str(root)) else rel(p, root) for p in cands if p.exists()]


def agent_files(root):
    """Définitions d'agents Claude (projet puis utilisateur). Projet prioritaire à nom égal."""
    found = {}
    for base in (Path.home() / ".claude" / "agents", root / ".claude" / "agents"):
        if base.is_dir():
            for f in sorted(base.glob("*.md")):
                found[f.stem] = f
    return found


PREAMBLE = {
    "read": (
        "ROLE: you are a delegate of Claude Code working on the same repository. READ-ONLY: do not "
        "modify any file. Report findings with file:line evidence, state what you did NOT verify, "
        "and give a confidence level. Do not guess: say 'unknown' when you cannot establish a fact."
    ),
    "write": (
        "ROLE: you are a delegate of Claude Code working on the same repository. Stay strictly "
        "inside the scope below. Do NOT commit, push, or change git config. Do NOT touch .env*, "
        ".claude/settings*, CI workflows or credentials. Run the relevant tests/linters and report "
        "their real output. Finish with: files changed, commands run, what you are unsure about."
    ),
    "review": (
        "ROLE: independent adversarial reviewer for Claude Code. Your job is to try to FALSIFY the "
        "conclusion below, not to confirm it. Verify each claim against the actual code, data or "
        "configuration (not against the summary). Do not be polite. If you agree, list exactly "
        "what you checked, an 'agree' without evidence is worthless. Do not widen the scope. "
        "Answer strictly in the requested JSON schema."
    ),
    "blind": (
        "ROLE: independent analyst for Claude Code. Form your OWN conclusion from the sources, "
        "without assuming what the answer should be. Give conclusion, evidence (file:line), "
        "assumptions, and what would change your mind."
    ),
}

FOOTER = (
    "\n\nSHARED KNOWLEDGE BASE: the files listed above are maintained by Claude Code for this "
    "project. Read the ones relevant to the task before starting; they override your own "
    "assumptions. @imports inside CLAUDE.md are not expanded automatically: follow them if needed. "
    "Answer in the language of the brief. End with a line 'Brief feedback:' (1-3 lines) saying what "
    "was missing or ambiguous in this brief."
)


def build_prompt(mode, brief, root, agent=None):
    pointers = knowledge_pointers(root)
    pl = "\n".join(f"- {p}" for p in pointers) or "- (none found)"
    agents = agent_files(root)
    roles = ""
    if agents:
        rl = "\n".join(f"- {n}: {rel(f, root) if str(f).startswith(str(root)) else f}" for n, f in agents.items())
        roles = ("\n\nROLE DEFINITIONS (Claude Code agents, written as prompts). Adopt one ONLY if the brief "
                 "or ASSIGNED ROLE names it. Use their instructions and criteria; ignore any 'tools', "
                 "'model' or 'permissions' frontmatter, which does not apply to you and never widens "
                 "your sandbox:\n" + rl)
    assigned = f"\n\nASSIGNED ROLE: {agent} (read its definition above and follow it)." if agent else ""
    return (f"{PREAMBLE[mode]}\n\nKNOWLEDGE FILES:\n{pl}{roles}{assigned}"
            f"\n\n=== BRIEF ===\n{brief.strip()}\n=== END BRIEF ==={FOOTER}\n")


def scan_secrets(text):
    return [pat for pat in SECRET_PATTERNS if re.search(pat, text)]


# --------------------------------------------------------------------------- codex


def build_codex_cmd(mode, cfg, tier, result_path, sandbox, resume_id, is_git, model_override):
    """Seul endroit qui connaît la syntaxe de Codex CLI. Adapter ici si les flags changent."""
    cmd = ["codex", "exec"]
    if resume_id:
        cmd += ["resume", resume_id]
    cmd += ["--json", "-o", str(result_path), "-s", sandbox]
    fb = json.dumps(cfg.get("fallback_docs", ["CLAUDE.md"]))
    cmd += ["-c", f"project_doc_fallback_filenames={fb}"]
    t = cfg["tiers"].get(tier, cfg["tiers"]["standard"])
    model = model_override or t.get("model")
    if model:
        cmd += ["-m", model]
    if t.get("effort"):
        cmd += ["-c", f'model_reasoning_effort="{t["effort"]}"']
    if mode == "review" and not resume_id:
        cmd += ["--output-schema", str(REVIEW_SCHEMA)]
    if not is_git:
        cmd += ["--skip-git-repo-check"]
    cmd += ["-"]  # prompt sur stdin
    return cmd


def parse_events(path):
    usage = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    thread_id, last_msg = None, None
    if not path.exists():
        return usage, thread_id, last_msg
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = thread_id or e.get("thread_id") or e.get("session_id")
        if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict):
            u = e["usage"]
            usage["input"] += u.get("input_tokens", 0)
            usage["cached"] += u.get("cached_input_tokens", 0)
            usage["output"] += u.get("output_tokens", 0)
            usage["reasoning"] += u.get("reasoning_output_tokens", 0)
        item = e.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
            last_msg = item["text"]
    return usage, thread_id, last_msg


def git_out(args, cwd):
    r = sh(["git"] + args, cwd=cwd)
    return r.stdout.strip() if r and r.returncode == 0 else ""


# --------------------------------------------------------------------------- commandes


def ensure_init(root):
    """Init paresseux et idempotent de <projet>/.claude/codex-local/. Ne réécrit jamais l'existant."""
    d = data_dir(root)
    (d / "runs").mkdir(parents=True, exist_ok=True)
    cfg_p = d / "config.json"
    if not cfg_p.exists():
        # overrides propres au projet uniquement; les modèles se règlent en global (`config set --global`)
        cfg_p.write_text('{\n  "tiers": {}\n}\n', encoding="utf-8")
    lp = d / "learned.md"
    if not lp.exists():
        lp.write_text(LEARNED_TEMPLATE, encoding="utf-8")
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("runs/\nworktrees/\n", encoding="utf-8")
    return d, cfg_p


def cmd_doctor(a):
    """Diagnostic: Codex, auth, docs d'instructions, taille. Initialise aussi si besoin."""
    root, is_git = project_root()
    d, cfg_p = ensure_init(root)
    problems = []

    codex = shutil.which("codex")
    print(f"Projet            : {root} ({'git' if is_git else 'pas un dépôt git'})")
    print(f"Données           : {rel(d, root)}/")
    if codex:
        v = sh(["codex", "--version"])
        print(f"Codex CLI         : {codex} ({v.stdout.strip() if v and v.stdout else 'version inconnue'})")
        ls = sh(["codex", "login", "status"])
        status = (ls.stdout or ls.stderr).strip().splitlines()[0] if ls and (ls.stdout or ls.stderr).strip() else "inconnu"
        print(f"Authentification  : {status}")
    else:
        problems.append("Codex CLI introuvable dans le PATH (npm i -g @openai/codex ou brew install codex)")
    if not is_git:
        problems.append("pas un dépôt git: mode write et worktree indisponibles, --skip-git-repo-check sera utilisé")
    docs = [n for n in ("CLAUDE.md", "AGENTS.md") if (root / n).exists()]
    print(f"Docs d'instructions: {', '.join(docs) or 'aucun'}")
    for n in docs:
        size = (root / n).stat().st_size
        if size > 32 * 1024:
            problems.append(f"{n} fait {size // 1024} KiB: Codex tronque au-delà de 32 KiB (project_doc_max_bytes)")
    ag = agent_files(root)
    print(f"Agents Claude exposés: {', '.join(ag) or 'aucun'}")
    if "AGENTS.md" in docs:
        print("  note: si AGENTS.md existe dans un dossier, Codex ignore le CLAUDE.md du même dossier "
              "(fallback). Le brief liste CLAUDE.md quand même.")
    if not docs:
        problems.append("ni CLAUDE.md ni AGENTS.md à la racine: Codex partira sans contexte projet")
    print("Modèles par tier  : `config show` (réglage: `config set --tier fast --model NOM --global`)")
    if problems:
        print("\nÀ corriger:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("\nOK.")
    return 0 if codex else 1


def cmd_run(a):
    root, is_git = project_root(a.cd)
    d, _ = ensure_init(root)
    cfg = load_config(root)
    if not shutil.which("codex"):
        print("Codex CLI introuvable dans le PATH. Lance /codex-local doctor.", file=sys.stderr)
        return 2

    brief = sys.stdin.read() if a.brief == "-" else Path(a.brief).read_text(encoding="utf-8")
    if not brief.strip():
        print("Brief vide.", file=sys.stderr)
        return 2
    hits = scan_secrets(brief)
    if hits:
        print("Brief refusé: motif ressemblant à un secret détecté. Retire-le et référence le "
              "fichier à la place.", file=sys.stderr)
        return 2

    mode = a.mode
    ok, why = budget_check(mode, a.stakes, cfg)
    if ok:
        ok, why = quota_check(mode, a.stakes, cfg)
    if not ok and not a.override_budget:
        print(f"[codex-local] REFUSÉ: {why}. Faire la tâche soi-même, la découper, ou demander à "
              f"l'utilisateur (--override-budget seulement avec son accord explicite).", file=sys.stderr)
        return 3
    sandbox = "workspace-write" if mode == "write" else "read-only"
    if mode == "write" and not is_git:
        print("Mode write refusé hors dépôt git (aucun filet de sécurité).", file=sys.stderr)
        return 2

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    rdir = d / "runs" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    workdir = root
    base_sha = git_out(["rev-parse", "HEAD"], root) if is_git else ""

    if mode == "write":
        # les données du plugin (ledger, config) ne comptent pas comme arbre sale
        dirty = git_out(["status", "--porcelain", "--", ".", ":(exclude).claude/codex-local"], root)
        if a.worktree:
            wt = d / "worktrees" / run_id
            r = sh(["git", "worktree", "add", "-b", f"codex/{run_id}", str(wt), "HEAD"], cwd=root)
            if not r or r.returncode != 0:
                print(f"git worktree a échoué: {r.stderr.strip() if r else 'git absent'}", file=sys.stderr)
                return 2
            workdir = wt
        elif dirty and not a.allow_dirty:
            print("Arbre git non propre. Commit/stash, ou utilise --worktree, ou --allow-dirty.", file=sys.stderr)
            return 2

    resume_id = None
    if a.resume:
        prev = ledger_runs(root).get(a.resume)
        resume_id = (prev or {}).get("thread_id") or "--last"

    if a.agent and a.agent not in agent_files(root):
        print(f"Agent inconnu: {a.agent}. Disponibles: {', '.join(agent_files(root)) or 'aucun'}", file=sys.stderr)
        return 2
    prompt = build_prompt(mode, brief, root, a.agent)
    (rdir / "brief.md").write_text(prompt, encoding="utf-8")
    result_path = rdir / "result.md"
    cmd = build_codex_cmd(mode, cfg, a.tier, result_path, sandbox, resume_id, is_git, a.model)

    t0 = time.time()
    status = "ok"
    with (rdir / "events.jsonl").open("w") as ev, (rdir / "stderr.log").open("w") as er:
        try:
            p = subprocess.run(cmd, input=prompt, text=True, stdout=ev, stderr=er, cwd=workdir,
                               timeout=a.timeout or cfg["timeout_s"])
            code = p.returncode
            if code != 0:
                status = "error"
        except subprocess.TimeoutExpired:
            code, status = 124, "timeout"
    dur = round(time.time() - t0, 1)

    usage, thread_id, last_msg = parse_events(rdir / "events.jsonl")
    result = result_path.read_text(encoding="utf-8") if result_path.exists() else (last_msg or "")

    ledger_append(root, {
        "type": "run", "id": run_id, "ts": now_iso(), "mode": mode, "tier": a.tier,
        "task_type": a.task_type, "stakes": a.stakes, "model": a.model or cfg["tiers"].get(a.tier, {}).get("model"),
        "effort": cfg["tiers"].get(a.tier, {}).get("effort"), "duration_s": dur, "exit_code": code,
        "status": status, "tokens": usage, "thread_id": thread_id, "base_sha": base_sha,
        "workdir": rel(workdir, root), "resumed_from": a.resume, "agent": a.agent,
    })

    print(f"[codex-local] run={run_id} mode={mode} tier={a.tier} type={a.task_type} stakes={a.stakes} "
          f"status={status} exit={code} durée={dur}s tokens(in/cache/out)={usage['input']}/{usage['cached']}/{usage['output']}")
    eff = eff_tokens(usage)
    usage_append({"ts": now_iso(), "project": root.name, "id": run_id, "mode": mode, "tier": a.tier,
                  "task_type": a.task_type, "eff": eff})
    rl = read_rate_limits(rdir / "events.jsonl")
    if not rl and thread_id:
        rl = read_rate_limits(find_session_file(thread_id))
    snap = snapshot_from_rl(rl) if rl else None
    if snap:
        quota_append({"ts": now_iso(), "source": "auto", "run": run_id,
                      "cum_eff": sum(r.get("eff", 0) for r in usage_rows()), **snap})
        print("[codex-local] " + " | ".join(quota_lines(cfg)[:2]))
    st = budget_state(cfg)
    b = st["b"]
    lim = f"/{b['short_soft']}" if b.get("short_soft") else ""
    print(f"[codex-local] coût effectif={eff} tokens | conso {b['short_hours']}h={st['short']}{lim} "
          f"semaine={st['week']}" + (f"/{b['week_soft']}" if b.get("week_soft") else ""))
    print(f"[codex-local] détail: {rel(rdir, root)}/")
    if status != "ok":
        tail = (rdir / "stderr.log").read_text(errors="replace")[-800:]
        print(f"[codex-local] stderr (fin):\n{tail}")

    if mode == "review" and result.strip():
        try:
            print(json.dumps(json.loads(result), indent=2, ensure_ascii=False))
        except json.JSONDecodeError:
            print("[codex-local] ATTENTION: sortie non conforme au schéma, brute ci-dessous")
            print(result)
    else:
        print("----- RÉSULTAT CODEX -----")
        print(result.strip() or "(aucun message final)")

    if mode == "write":
        print("----- MODIFICATIONS -----")
        print(git_out(["diff", "--stat"], workdir) or "(aucun fichier suivi modifié)")
        porcelain = [l for l in git_out(["status", "--porcelain"], workdir).splitlines()
                     if ".claude/codex-local" not in l]
        untracked = [l[3:] for l in porcelain if l.startswith("??")]
        if untracked:
            print("Nouveaux fichiers: " + ", ".join(untracked[:20]))
        flagged = [l[3:] for l in porcelain if SENSITIVE_TOUCHED.search(l[3:])]
        if flagged:
            print("!! FICHIERS SENSIBLES TOUCHÉS, À REVOIR AVANT TOUT: " + ", ".join(flagged))
        if a.worktree:
            print(f"Worktree: {rel(workdir, root)} (branche codex/{run_id}). Intégrer: git merge/cherry-pick, "
                  f"puis git worktree remove.")

    print(f"\n[codex-local] OBLIGATOIRE après vérification: "
          f"codex_local.py record {run_id} --outcome <{'|'.join(OUTCOMES)}> --note \"...\"")
    return 0 if status == "ok" else 1


def cmd_record(a):
    root, _ = project_root()
    runs = ledger_runs(root)
    if a.id not in runs:
        print(f"Run inconnu: {a.id}", file=sys.stderr)
        return 2
    ledger_append(root, {"type": "outcome", "id": a.id, "ts": now_iso(), "outcome": a.outcome, "note": a.note or ""})
    resolved = sum(1 for r in ledger_runs(root).values() if r.get("outcome"))
    print(f"Verdict enregistré: {a.id} -> {a.outcome}")
    if resolved and resolved % 10 == 0:
        print(f"{resolved} verdicts: lance `stats` et mets à jour .claude/codex-local/learned.md.")
    return 0


def level_for(rs):
    # `incomplete` (abstention honnête) est neutre pour la confiance: ni accepté ni rejeté
    rs = sorted((r for r in rs if r["outcome"] != "incomplete"), key=lambda r: r["ts"])
    n = len(rs)
    acc = sum(r["outcome"] == "accepted" for r in rs)
    if n < 5:
        return "L2", f"{n}/5 verdicts minimum pour promotion"
    if any(r["outcome"] == "rejected" for r in rs[-3:]):
        return "L2", "rejet dans les 3 derniers runs"
    if any(r["outcome"] == "rejected" and r.get("stakes") == "high" for r in rs):
        return "L2", "rejet passé sur enjeu élevé"
    if acc / n >= 0.8:
        return "L3", f"{acc}/{n} acceptés sans retouche"
    return "L2", f"{acc}/{n} acceptés (< 80%)"


def cmd_stats(a):
    root, _ = project_root()
    runs = list(ledger_runs(root).values())
    if not runs:
        print("Ledger vide.")
        return 0
    out = {"total_runs": len(runs), "delegation": {}, "challenge": {}, "tokens": {}}
    for r in runs:
        m = r["mode"]
        t = out["tokens"].setdefault(m, {"runs": 0, "input": 0, "output": 0, "duration_s": 0.0})
        t["runs"] += 1
        t["input"] += r.get("tokens", {}).get("input", 0)
        t["output"] += r.get("tokens", {}).get("output", 0)
        t["duration_s"] = round(t["duration_s"] + r.get("duration_s", 0), 1)

    deleg = [r for r in runs if r["mode"] in ("read", "write")]
    for tt in sorted({r["task_type"] for r in deleg}):
        rs = [r for r in deleg if r["task_type"] == tt]
        res = [r for r in rs if r.get("outcome")]
        c = {o: sum(r["outcome"] == o for r in res) for o in ("accepted", "modified", "rejected", "incomplete")}
        w = [r for r in res if r["mode"] == "write"]
        lvl = level_for(w) if w else ("L1", "lecture seule uniquement")
        inc_tiers = sorted({r["tier"] for r in res if r["outcome"] == "incomplete"})
        hint = ""
        if inc_tiers:
            hint = f"abstentions au tier {'/'.join(inc_tiers)}: monter d'un tier ou découper la tâche"
        out["delegation"][tt] = {"runs": len(rs), "resolved": len(res), **c,
                                 "level": lvl[0], "why": lvl[1], "hint": hint}

    chal = [r for r in runs if r["mode"] in ("review", "blind") and r.get("outcome")]
    for tt in sorted({r["task_type"] for r in chal}):
        rs = [r for r in chal if r["task_type"] == tt]
        c = {o: sum(r["outcome"] == o for r in rs) for o in ("codex_right", "claude_right", "both_partly", "inconclusive")}
        dec = c["codex_right"] + c["claude_right"]
        c["precision_codex"] = round(c["codex_right"] / dec, 2) if dec else None
        c["n"] = len(rs)
        out["challenge"][tt] = c

    if a.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    print(f"Runs: {out['total_runs']}")
    print("\nDélégation (niveaux: L1 lecture, L2 écriture en worktree + revue complète, L3 écriture directe + revue du diff)")
    for tt, c in out["delegation"].items():
        print(f"  {tt:<22} runs={c['runs']:<3} verdicts={c['resolved']:<3} ok={c['accepted']} modif={c['modified']} "
              f"rejet={c['rejected']} abstention={c['incomplete']}  -> {c['level']} ({c['why']})"
              + (f"\n{'':<26}! {c['hint']}" if c["hint"] else ""))
    print("\nContradiction (précision = objections de Codex confirmées / objections tranchées)")
    for tt, c in out["challenge"].items():
        p = "n/a" if c["precision_codex"] is None else f"{int(c['precision_codex'] * 100)}%"
        verdict = ""
        if c["precision_codex"] is not None and c["n"] >= 5:
            verdict = ("  -> utile: garder en second avis systématique" if c["precision_codex"] >= 0.5
                       else "  -> bruyant: réserver aux enjeux élevés" if c["precision_codex"] < 0.25 else "")
        print(f"  {tt:<22} n={c['n']:<3} codex_right={c['codex_right']} claude_right={c['claude_right']} "
              f"partiel={c['both_partly']} non_tranché={c['inconclusive']} précision={p}{verdict}")
    print("\nCoût (tokens Codex, pas ceux de Claude)")
    for m, t in out["tokens"].items():
        print(f"  {m:<7} runs={t['runs']:<3} in={t['input']:<10} out={t['output']:<8} durée={t['duration_s']}s")
    return 0


def cmd_config(a):
    root, _ = project_root()
    if a.action == "show":
        cfg, src = load_config(root, with_sources=True)
        print("Sources (ordre croissant de priorité): défauts" + "".join(f" < {x}" for x in src))
        for t, v in cfg["tiers"].items():
            print(f"  {t:<9} model={v.get('model') or '(défaut de ~/.codex/config.toml)'}  effort={v.get('effort')}")
        print(f"  timeout_s={cfg['timeout_s']}  fallback_docs={cfg['fallback_docs']}")
        print(f"  budget={cfg['budget']}")
        return 0
    if not a.budget and not (a.tier and (a.model or a.effort or a.clear)):
        print("Usage: config set --tier fast|standard|deep [--model NOM] [--effort ...] [--clear] [--global]\n"
              "       config set --budget short_soft=N --budget week_soft=N [--budget short_hard=N ...] [--global]\n"
              "       clés budget: short_hours short_soft short_hard week_soft week_hard reserve_pct",
              file=sys.stderr)
        return 2
    p = global_config_path() if a.use_global else data_dir(root) / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if a.budget:
        keys = set(DEFAULT_CONFIG["budget"])
        for kv in a.budget:
            k, _, v = kv.partition("=")
            if k not in keys or not v.lstrip("-").isdigit():
                print(f"--budget {kv}: clé inconnue ou valeur non entière (clés: {', '.join(sorted(keys))})", file=sys.stderr)
                return 2
            cur.setdefault("budget", {})[k] = int(v)
        p.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{'global' if a.use_global else 'projet'}: budget -> {cur['budget']}  ({p})")
        return 0
    t = cur.setdefault("tiers", {}).setdefault(a.tier, {})
    if a.clear:
        t.pop("model", None)
    if a.model:
        t["model"] = a.model
    if a.effort:
        t["effort"] = a.effort
    p.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{'global' if a.use_global else 'projet'}: {a.tier} -> {t}  ({p})")
    return 0


def cmd_quota(a):
    root, _ = project_root()
    cfg = load_config(root)
    if a.action == "set":
        row = {"ts": now_iso(), "source": "manual",
               "cum_eff": sum(r.get("eff", 0) for r in usage_rows())}
        for kind in ("short", "week"):
            left = getattr(a, f"{kind}_left")
            if left is None:
                continue
            rs = getattr(a, f"{kind}_resets")
            ts = parse_when(rs) if rs else None
            if rs and ts is None:
                print(f"--{kind}-resets illisible: {rs} (formats: HH:MM ou 'AAAA-MM-JJ HH:MM', heure locale)", file=sys.stderr)
                return 2
            row[kind] = {"left": float(left), "resets": ts}
        if "short" not in row and "week" not in row:
            print("Usage: quota set --short-left N [--short-resets HH:MM] --week-left N "
                  "[--week-resets 'AAAA-MM-JJ HH:MM']  (valeurs de /status dans Codex)", file=sys.stderr)
            return 2
        quota_append(row)
        print("Relevé enregistré.")
    for line in quota_lines(cfg):
        print(line)
    ok_r, why_r = quota_check("read", "low", cfg)
    ok_v, why_v = quota_check("review", "high", cfg)
    print(f"Délégation ordinaire: {'oui' if ok_r else 'NON, ' + why_r}")
    print(f"Seconde opinion enjeux élevés: {'oui' if ok_v else 'NON, ' + why_v}")
    # calibration approximative: tokens effectifs du pont par point de quota hebdo consommé
    rows = [r for r in quota_rows() if r.get("week")]
    ratios = []
    for x, y in zip(rows, rows[1:]):
        if x["week"].get("resets") != y["week"].get("resets"):
            continue
        dp = x["week"]["left"] - y["week"]["left"]
        de = y.get("cum_eff", 0) - x.get("cum_eff", 0)
        if dp >= 1 and de > 0:
            ratios.append(de / dp)
    if ratios:
        ratios.sort()
        print(f"Calibration approximative: ~{int(ratios[len(ratios) // 2])} tokens effectifs du pont par point de "
              f"quota hebdo (n={len(ratios)}; surestimé si d'autres usages de Codex pèsent sur le même compte).")
    return 0


def cmd_budget(a):
    root, _ = project_root()
    cfg = load_config(root)
    st = budget_state(cfg)
    b = st["b"]
    for line in quota_lines(cfg):
        print(line)
    print("\nConso Codex via le pont (tokens effectifs = entrée hors cache + sortie, tous projets):")
    print(f"  {b['short_hours']}h glissantes : {st['short']}   soft={b.get('short_soft')} hard={b.get('short_hard')}")
    print(f"  7 jours         : {st['week']}   soft={b.get('week_soft')} hard={b.get('week_hard')}")
    print(f"  réserve secondes opinions : {b.get('reserve_pct')}% du soft")
    ok_r, why_r = budget_check("read", "low", cfg)
    ok_v, why_v = budget_check("review", "high", cfg)
    print(f"  délégation ordinaire possible : {'oui' if ok_r else 'NON, ' + why_r}")
    print(f"  seconde opinion enjeux élevés : {'oui' if ok_v else 'NON, ' + why_v}")
    agg = {}
    for r in st["rows"]:
        k = (r.get("task_type", "?"), r.get("tier", "?"))
        n, tot = agg.get(k, (0, 0))
        agg[k] = (n + 1, tot + r.get("eff", 0))
    if agg:
        print("\nCoût moyen observé par type de tâche et tier (pour estimer avant de déléguer):")
        for (tt, tier), (n, tot) in sorted(agg.items()):
            print(f"  {tt:<22} {tier:<9} runs={n:<3} moyenne={tot // n} tokens effectifs")
    # les limites en tokens sont facultatives: le garde-fou principal est le % de quota réel (quota set)
    return 0


def cmd_pending(a):
    root, _ = project_root()
    pend = [r for r in ledger_runs(root).values() if not r.get("outcome") and r.get("status") != "error"]
    if not pend:
        print("Aucun run sans verdict.")
        return 0
    for r in sorted(pend, key=lambda r: r["ts"]):
        print(f"{r['id']}  {r['mode']:<6} {r['task_type']:<20} stakes={r['stakes']}  {r['ts']}")
    return 0


def cmd_hook_session(a):
    try:
        sys.stdin.read()
    except Exception:
        pass
    root, _ = project_root()
    d = data_dir(root)
    codex = shutil.which("codex")
    if not codex:
        return 0
    shim = str(Path(__file__).resolve().parent / "codex-local")  # wrapper exécutable, un seul chemin
    envf = os.environ.get("CLAUDE_ENV_FILE")
    if envf:
        try:
            with open(envf, "a", encoding="utf-8") as f:
                f.write(f"export CODEX_LOCAL='{shim}'\n")
        except OSError:
            pass
    lines = [
        "[codex-local] Codex CLI " + (f"disponible ({codex})." if codex else "absent du PATH."),
        f"Pont: \"$CODEX_LOCAL\" (= {shim}), à appeler entre guillemets. Sous-commandes: run, record, stats, pending, quota, budget, config, doctor.",
        "Skills: codex-delegation (quoi/quand/comment déléguer), codex-second-opinion (contradiction "
        "obligatoire sur les enjeux élevés), codex-learning (verdicts et règles apprises).",
    ]
    cfg = load_config(root)
    qs = quota_lines(cfg)
    lines.append(qs[0] if len(qs) == 1 and qs[0].startswith("Quota Codex réel") else " ".join(qs))
    ok_q, why_q = quota_check("read", "low", cfg)
    if not ok_q:
        lines.append(f"Délégation ordinaire BLOQUÉE ({why_q}). Seules les secondes opinions à enjeux élevés passent.")
    rows = usage_rows()
    if rows and any(cfg["budget"].get(k) for k in ("short_soft", "short_hard", "week_soft", "week_hard")):
        st = budget_state(cfg)
        b = st["b"]
        if any(b.get(k) for k in ("short_soft", "short_hard", "week_soft", "week_hard")):
            ok_r, why_r = budget_check("read", "low", cfg)
            lines.append(f"Budget Codex {b['short_hours']}h: {st['short']}/{b.get('short_soft')}, semaine: "
                         f"{st['week']}/{b.get('week_soft')}. " + ("" if ok_r else f"Délégation ordinaire bloquée ({why_r}). ")
                         + "Détail: `\"$CODEX_LOCAL\" budget`.")
    if d.exists():
        pend = [r for r in ledger_runs(root).values() if not r.get("outcome") and r.get("status") != "error"]
        if pend:
            lines.append(f"{len(pend)} run(s) Codex sans verdict: `$CODEX_LOCAL pending` puis `record`.")
        if (d / "learned.md").exists():
            lines.append("Règles apprises pour ce projet: .claude/codex-local/learned.md (à lire avant de déléguer).")
    # pas d'init requise: `run` crée .claude/codex-local/ au premier usage
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                            "additionalContext": "\n".join(lines)}}))
    return 0


# --------------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(prog="codex_local.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="diagnostic (Codex, auth, docs, taille)").set_defaults(fn=cmd_doctor)

    r = sub.add_parser("run", help="déléguer une tâche à Codex")
    r.add_argument("--mode", choices=["read", "write", "review", "blind"], required=True)
    r.add_argument("--tier", choices=["fast", "standard", "deep"], default="standard")
    r.add_argument("--task-type", default="general", help="ex: inventory, tests, refactor, security-review")
    r.add_argument("--stakes", choices=["low", "high"], default="low")
    r.add_argument("--brief", default="-", help="fichier de brief, ou - pour stdin")
    r.add_argument("--model", help="force un modèle (sinon tier/config)")
    r.add_argument("--worktree", action="store_true", help="(write) isole dans un git worktree")
    r.add_argument("--allow-dirty", action="store_true", help="(write) accepte un arbre non propre")
    r.add_argument("--agent", help="nom d'un agent .claude/agents/<nom>.md que Codex doit endosser")
    r.add_argument("--resume", metavar="RUN_ID", help="reprend la session Codex d'un run précédent")
    r.add_argument("--override-budget", action="store_true",
                   help="ignore le budget Codex (seulement avec l'accord explicite de l'utilisateur)")
    r.add_argument("--timeout", type=int)
    r.add_argument("--cd", help="répertoire du projet")
    r.set_defaults(fn=cmd_run)

    rc = sub.add_parser("record", help="enregistrer le verdict sur un run")
    rc.add_argument("id")
    rc.add_argument("--outcome", choices=list(OUTCOMES), required=True)
    rc.add_argument("--note")
    rc.set_defaults(fn=cmd_record)

    st = sub.add_parser("stats")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_stats)

    cf = sub.add_parser("config", help="voir ou régler modèles/effort par tier")
    cf.add_argument("action", choices=["show", "set"])
    cf.add_argument("--tier", choices=["fast", "standard", "deep"])
    cf.add_argument("--budget", action="append", metavar="CLE=N", help="limite de tokens (répétable)")
    cf.add_argument("--model")
    cf.add_argument("--effort", choices=["low", "medium", "high", "xhigh"])
    cf.add_argument("--clear", action="store_true", help="retire le modèle (retour au défaut Codex)")
    cf.add_argument("--global", dest="use_global", action="store_true", help="tous les projets")
    cf.set_defaults(fn=cmd_config)

    q = sub.add_parser("quota", help="quota Codex réel en % (valeurs de /status), planchers, calibration")
    q.add_argument("action", choices=["show", "set"])
    q.add_argument("--short-left", type=float, help="% restant, fenêtre 5h")
    q.add_argument("--short-resets", help="HH:MM ou 'AAAA-MM-JJ HH:MM' (heure locale)")
    q.add_argument("--week-left", type=float, help="% restant, fenêtre hebdo")
    q.add_argument("--week-resets", help="'AAAA-MM-JJ HH:MM' (heure locale)")
    q.set_defaults(fn=cmd_quota)
    sub.add_parser("budget", help="conso Codex, limites, coût moyen par type de tâche").set_defaults(fn=cmd_budget)
    sub.add_parser("pending").set_defaults(fn=cmd_pending)
    sub.add_parser("hook-session").set_defaults(fn=cmd_hook_session)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
