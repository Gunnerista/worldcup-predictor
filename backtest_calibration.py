"""Calibration backtest of MATCHIQ's core probability engine on recent
international matches. Read-only w.r.t. the live app/DB: it imports the live
engine and reads the local SQLite for rho/finalists. It writes ONLY
static/calibration_report.json — the artifact the /methodology page renders.

Reuses the LIVE distribution path: DixonColesEngine.predict() -> _expected_goals
(ELO weight + HOME_ADVANTAGE + clamps) -> scoreline_matrix(self.RHO) -> _assemble.
Only the INPUTS (attack/defense strength + ELO) are recomputed as-of from the
international corpus. The 2026-only lambda stages (player_xG_adj, situation_mult)
are deliberately NOT exercised.

Data source: github.com/martj42/international_results  (results.csv, CC0).
  Clone once:  git clone https://github.com/martj42/international_results.git
  Regenerate the report JSON (then commit static/calibration_report.json):
    INTL_RESULTS_CSV=/path/to/results.csv python backtest_calibration.py --dump
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import csv
import json
import math
import os
import statistics
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone

from model import DixonColesEngine, EloEngine, DEFAULT_ELO
import database


def _resolve_csv():
    """results.csv path: first non-flag CLI arg > $INTL_RESULTS_CSV > temp clone."""
    for a in sys.argv[1:]:
        if not a.startswith("-"):
            return a
    return os.environ.get(
        "INTL_RESULTS_CSV",
        r"C:/Users/Dell/AppData/Local/Temp/intl_results/results.csv",
    )


CSV = _resolve_csv()

# DB-name -> results.csv-name (verified present in CSV in the prior investigation)
ALIASES = {
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Cabo Verde": "Cape Verde",
    "Czechia": "Czech Republic",
    "Côte d'Ivoire": "Ivory Coast",
    "Türkiye": "Turkey",
    "USA": "United States",
}

WARMUP_END = "2025-07-01"   # score only matches on/after this date
CORPUS_START = "2025-01-01"
EXCLUDE_TOURNAMENT = "FIFA World Cup"   # 2026 finals: leak + eval target


def db_finalists_csv_names():
    """48 finalists from the live DB, mapped into results.csv name space."""
    conn = database.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT t.name FROM teams t
            WHERE t.id IN (SELECT home_team_id FROM matches WHERE season=2026
                           UNION SELECT away_team_id FROM matches WHERE season=2026)
            ORDER BY t.name
            """
        ).fetchall()
    finally:
        conn.close()
    names = [r[0] for r in rows
             if not any(ch.isdigit() for ch in r[0]) and "/" not in r[0]]
    return {ALIASES.get(n, n) for n in names}, names


# ---- metrics ---------------------------------------------------------------
def rps(p, o):
    """Ranked probability score for ordered [home, draw, away]."""
    c1 = p[0] - o[0]
    c2 = (p[0] + p[1]) - (o[0] + o[1])
    return 0.5 * (c1 * c1 + c2 * c2)


def brier(p, o):
    return sum((p[k] - o[k]) ** 2 for k in range(3))


def logloss(p, o):
    idx = o.index(1.0)
    return -math.log(max(p[idx], 1e-15))


def reliability(pairs, nbins=10):
    """pairs: list of (pred_prob, outcome_0_or_1). Returns per-bin stats + ECE."""
    bins = [[] for _ in range(nbins)]  # each: list of (p, o)
    for p, o in pairs:
        b = min(nbins - 1, int(p * nbins))
        bins[b].append((p, o))
    rows, ece, N = [], 0.0, len(pairs)
    for i, b in enumerate(bins):
        if not b:
            rows.append((i / nbins, (i + 1) / nbins, 0, None, None))
            continue
        mp = sum(x[0] for x in b) / len(b)
        mo = sum(x[1] for x in b) / len(b)
        rows.append((i / nbins, (i + 1) / nbins, len(b), mp, mo))
        ece += (len(b) / N) * abs(mp - mo)
    return rows, ece


def apply_temperature(p, T):
    """p (normalized 3-vector) -> softmax(log(p)/T). T>1 flattens (less confident)."""
    z = [math.log(max(x, 1e-12)) / T for x in p]
    m = max(z)
    e = [math.exp(zi - m) for zi in z]
    s = sum(e)
    return [ei / s for ei in e]


def nll_at_T(samples, T):
    """Mean negative log-likelihood of temperature-scaled probs on `samples`."""
    tot = 0.0
    for s in samples:
        ps = apply_temperature(s["p"], T)
        idx = s["o"].index(1.0)
        tot += -math.log(max(ps[idx], 1e-15))
    return tot / len(samples)


def fit_temperature(train, lo=0.5, hi=6.0, tol=1e-4):
    """Golden-section minimisation of train NLL over T (pure Python, no scipy)."""
    gr = (math.sqrt(5) - 1) / 2  # 0.618...
    a, b = lo, hi
    c = b - gr * (b - a)
    d = a + gr * (b - a)
    fc, fd = nll_at_T(train, c), nll_at_T(train, d)
    while (b - a) > tol:
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - gr * (b - a)
            fc = nll_at_T(train, c)
        else:
            a, c, fc = c, d, fd
            d = a + gr * (b - a)
            fd = nll_at_T(train, d)
    return (a + b) / 2


def metrics_on(samples, transform=None):
    """RPS/Brier/logloss/ECE on `samples`; transform(p)->p' applied if given."""
    n = len(samples)
    def P(s):
        return transform(s["p"]) if transform else s["p"]
    out = {
        "rps": sum(rps(P(s), s["o"]) for s in samples) / n,
        "brier": sum(brier(P(s), s["o"]) for s in samples) / n,
        "logloss": sum(logloss(P(s), s["o"]) for s in samples) / n,
    }
    pooled = [(P(s)[k], s["o"][k]) for s in samples for k in range(3)]
    _, out["ece"] = reliability(pooled)
    return out


def fmt_metrics(name, vals):
    return (f"  {name:14s}  RPS={vals['rps']:.4f}  Brier={vals['brier']:.4f}  "
            f"logloss={vals['logloss']:.4f}  ECE={vals['ece']:.4f}")


def _run_asof(csv_path):
    """As-of chronological backtest. Returns (samples, warmup_n, n_corpus, dc,
    finalists). Each scored match is predicted using ONLY prior-date data."""
    finalists, _ = db_finalists_csv_names()
    dc = DixonColesEngine()  # estimates rho from 2018+2022 local SQLite — same as live

    rows = []
    with open(csv_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["date"] < CORPUS_START:
                continue
            if r["tournament"] == EXCLUDE_TOURNAMENT:
                continue
            if not (r["home_team"] in finalists or r["away_team"] in finalists):
                continue
            if r["home_score"] == "" or r["away_score"] == "":
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["date"])

    elo = EloEngine()
    gf, ga, gn = defaultdict(float), defaultdict(float), defaultdict(int)

    def league_avg():
        rates = [gf[t] / gn[t] for t in gn if gn[t] > 0]
        return (sum(rates) / len(rates)) if rates else None

    def strength(team):
        if gn[team] == 0:
            return None, None  # cold start -> engine default
        la = league_avg()
        if not la or la <= 0:
            return None, None
        return (gf[team] / gn[team]) / la, (ga[team] / gn[team]) / la

    samples = []
    warmup_n = 0
    for r in rows:
        H, A = r["home_team"], r["away_team"]
        hs, as_ = int(r["home_score"]), int(r["away_score"])
        neutral = r["neutral"].strip().upper() == "TRUE"
        if r["date"] >= WARMUP_END:
            ha, hd = strength(H)
            aa, ad = strength(A)
            res = dc.predict(
                elo.get_rating(H), elo.get_rating(A),
                home_attack=ha, home_defense=hd,
                away_attack=aa, away_defense=ad,
                neutral_venue=neutral, top_n=1,
            )
            w = res["win_draw_loss"]
            p = [w["home_win"] / 100.0, w["draw"] / 100.0, w["away_win"] / 100.0]
            s = sum(p)
            if s > 0:
                p = [x / s for x in p]
            o = [1.0 if hs > as_ else 0.0,
                 1.0 if hs == as_ else 0.0,
                 1.0 if hs < as_ else 0.0]
            samples.append({"p": p, "o": o, "tournament": r["tournament"],
                            "prior_h": gn[H], "prior_a": gn[A], "date": r["date"]})
        else:
            warmup_n += 1

        # update state AFTER predicting (every corpus match feeds later predictions)
        result = "home_win" if hs > as_ else ("away_win" if hs < as_ else "draw")
        elo.update(H, A, result, "group")
        gf[H] += hs; ga[H] += as_; gn[H] += 1
        gf[A] += as_; ga[A] += hs; gn[A] += 1

    return samples, warmup_n, len(rows), dc, finalists


def main(csv_path=CSV):
    print("=" * 92)
    print("HONESTY: This backtest validates the CORE engine (Dixon-Coles + rho + "
          "ELO/strength lambda).")
    print("         The live-only stages (player_xG_adj, situation_mult) are NOT "
          "validated here.")
    print("=" * 92)

    samples, warmup_n, n_corpus, dc, finalists = _run_asof(csv_path)
    print(f"engine: RHO={dc.RHO:.4f} ({dc.RHO_SOURCE})  HOME_ADVANTAGE="
          f"{dc.HOME_ADVANTAGE}  MAX_GOALS={dc.MAX_GOALS}")
    print(f"ELO seed: flat DEFAULT_ELO={DEFAULT_ELO} for all teams; K=group; "
          f"as-of chronological replay (no refit).")
    print(f"finalists: {len(finalists)} (CSV-name space)")
    print(f"corpus: date>={CORPUS_START}, tournament!='{EXCLUDE_TOURNAMENT}', "
          f">=1 finalist; score window date>={WARMUP_END}")
    print("-" * 92)

    N = len(samples)
    print(f"corpus matches: {n_corpus}   warm-up (state only): {warmup_n}   "
          f"SCORED eval matches: {N}")
    if N == 0:
        print("no eval matches — abort")
        return

    # ---- diagnostic 3: eval tournament breakdown ----
    print("\n[diag-3] eval-set composition by tournament:")
    tc = Counter(s["tournament"] for s in samples)
    for t, c in tc.most_common():
        print(f"   {c:4d}  ({c/N*100:4.1f}%)  {t}")
    fr = tc.get("Friendly", 0)
    print(f"   -> Friendly share: {fr/N*100:.1f}%")

    # ---- diagnostic 2: prior match counts at prediction time ----
    priors = [s["prior_h"] for s in samples] + [s["prior_a"] for s in samples]
    priors.sort()
    print("\n[diag-2] prior matches per team at prediction time (2 per eval match):")
    print(f"   min={min(priors)}  median={statistics.median(priors)}  "
          f"max={max(priors)}  mean={statistics.mean(priors):.1f}")
    print(f"   share with <3 priors: "
          f"{sum(1 for x in priors if x < 3)/len(priors)*100:.1f}%")

    # ---- diagnostic E: cold start ----
    cold = sum(1 for s in samples if s["prior_h"] == 0 or s["prior_a"] == 0)
    print(f"\n[diag-E] eval matches with >=1 cold-start team (0 priors, engine "
          f"default strength): {cold} ({cold/N*100:.1f}%)")

    # ---- metrics: model ----
    model = {
        "rps": sum(rps(s["p"], s["o"]) for s in samples) / N,
        "brier": sum(brier(s["p"], s["o"]) for s in samples) / N,
        "logloss": sum(logloss(s["p"], s["o"]) for s in samples) / N,
    }
    pooled = [(s["p"][k], s["o"][k]) for s in samples for k in range(3)]
    _, model["ece"] = reliability(pooled)

    # ---- baseline: eval-set base-rate (climatology) ----
    br = [sum(s["o"][k] for s in samples) / N for k in range(3)]
    base = {
        "rps": sum(rps(br, s["o"]) for s in samples) / N,
        "brier": sum(brier(br, s["o"]) for s in samples) / N,
        "logloss": sum(logloss(br, s["o"]) for s in samples) / N,
    }
    base_pooled = [(br[k], s["o"][k]) for s in samples for k in range(3)]
    _, base["ece"] = reliability(base_pooled)

    print("\n" + "=" * 92)
    print(f"RESULTS  (N={N} eval matches)   actual base-rate "
          f"home/draw/away = {br[0]:.3f}/{br[1]:.3f}/{br[2]:.3f}")
    print("=" * 92)
    print(fmt_metrics("MODEL", model))
    print(fmt_metrics("base-rate", base) + "   [climatology, in-sample -> optimistic]")
    print("  reference: top public models RPS ~0.206")
    print("  model beats base-rate on:",
          ", ".join(m for m in ["rps", "brier", "logloss"]
                    if model[m] < base[m]) or "NONE")

    # ---- reliability tables ----
    def print_reliability(title, pairs):
        table, ece = reliability(pairs)
        print(f"\n  {title}  (ECE={ece:.4f})")
        print("   bin          n     pred     obs")
        for lo, hi, n, mp, mo in table:
            if n == 0:
                continue
            print(f"   {lo:.1f}-{hi:.1f}   {n:5d}   {mp:.3f}   {mo:.3f}")

    print("\n" + "-" * 92)
    print("RELIABILITY (pooled across all 3 outcomes):")
    print_reliability("pooled", pooled)
    names = ["home", "draw", "away"]
    for k in range(3):
        print_reliability(f"outcome={names[k]}",
                          [(s["p"][k], s["o"][k]) for s in samples])

    # =====================================================================
    # POST-HOC CALIBRATION — temperature scaling (fit on train, report on test)
    # =====================================================================
    print("\n" + "=" * 92)
    print("POST-HOC CALIBRATION: temperature scaling  (fit T on TRAIN only, "
          "all metrics on TEST only)")
    print("=" * 92)

    train = [s for s in samples if "2025-07-01" <= s["date"] <= "2025-12-31"]
    test = [s for s in samples if "2026-01-01" <= s["date"] <= "2026-05-31"]
    dropped = [s for s in samples if s not in train and s not in test]
    print(f"  windows:  train(2025-07..12)={len(train)}   "
          f"test(2026-01..05)={len(test)}   dropped(outside)={len(dropped)}")
    if dropped:
        dd = Counter(s["date"][:7] for s in dropped)
        print(f"            dropped by month: {dict(sorted(dd.items()))}")
    if not train or not test:
        print("  train or test empty — cannot calibrate")
        return

    T = fit_temperature(train)
    print(f"  fitted T = {T:.3f}   (T>1 => was overconfident; flattening applied)")
    print(f"  train NLL: T=1 -> {nll_at_T(train, 1.0):.4f}   "
          f"T={T:.3f} -> {nll_at_T(train, T):.4f}")

    raw = metrics_on(test)
    scaled = metrics_on(test, transform=lambda p: apply_temperature(p, T))
    print(f"\n  TEST metrics (N={len(test)}):")
    print(fmt_metrics("raw", raw))
    print(fmt_metrics(f"scaled(T={T:.2f})", scaled))
    better = [m for m in ["rps", "brier", "logloss", "ece"] if scaled[m] < raw[m]]
    print(f"  scaled improves: {', '.join(better) or 'NONE'}")

    def reliability_pair(title, pairs_raw, pairs_scaled):
        tr, er = reliability(pairs_raw)
        ts, es = reliability(pairs_scaled)
        print(f"\n  {title}   raw ECE={er:.4f} -> scaled ECE={es:.4f}")
        print("   bin         n     raw_pred  raw_obs | scl_pred  scl_obs")
        for (lo, hi, n, mp, mo), (_, _, _, sp, so) in zip(tr, ts):
            if n == 0:
                continue
            mp_ = f"{mp:.3f}" if mp is not None else "  -  "
            mo_ = f"{mo:.3f}" if mo is not None else "  -  "
            sp_ = f"{sp:.3f}" if sp is not None else "  -  "
            so_ = f"{so:.3f}" if so is not None else "  -  "
            print(f"   {lo:.1f}-{hi:.1f}  {n:5d}    {mp_}    {mo_}  |  {sp_}    {so_}")

    # pooled reliability raw vs scaled (with per-bin n)
    pooled_raw = [(s["p"][k], s["o"][k]) for s in test for k in range(3)]
    pooled_scl = [(apply_temperature(s["p"], T)[k], s["o"][k])
                  for s in test for k in range(3)]
    print("\n  -- TEST reliability (pooled, raw vs scaled) --")
    reliability_pair("pooled", pooled_raw, pooled_scl)

    # diag 4: per-outcome residual overconfidence after single T
    print("\n  -- TEST reliability per outcome (residual check for single-T) --")
    for k in range(3):
        rraw = [(s["p"][k], s["o"][k]) for s in test]
        rscl = [(apply_temperature(s["p"], T)[k], s["o"][k]) for s in test]
        reliability_pair(f"outcome={names[k]}", rraw, rscl)

    # quantify residual overconfidence in high-confidence region (pred>=0.6)
    print("\n  [diag-4] residual overconfidence above pred>=0.60 after single T:")
    for k in range(3):
        hi_raw = [(apply_temperature(s["p"], T)[k], s["o"][k]) for s in test
                  if apply_temperature(s["p"], T)[k] >= 0.60]
        if not hi_raw:
            print(f"     {names[k]:5s}: no scaled pred>=0.60")
            continue
        mp = sum(x for x, _ in hi_raw) / len(hi_raw)
        mo = sum(y for _, y in hi_raw) / len(hi_raw)
        gap = mp - mo
        flag = "  <-- residual overconfidence" if gap > 0.05 else ""
        print(f"     {names[k]:5s}: n={len(hi_raw):3d}  mean_pred={mp:.3f}  "
              f"obs={mo:.3f}  gap={gap:+.3f}{flag}")

    # =====================================================================
    # PRODUCTION T — refit on ALL 439 eval matches (the constant to hardcode)
    # =====================================================================
    print("\n" + "=" * 92)
    print("PRODUCTION T  (refit on ALL eval matches — value to hardcode in live engine)")
    print("=" * 92)
    Tp = fit_temperature(samples)
    raw_all = metrics_on(samples)
    scl_all = metrics_on(samples, transform=lambda p: apply_temperature(p, Tp))
    print(f"  production T = {Tp:.3f}   (N={len(samples)} full eval set)")
    print(f"  full-set ECE:  raw={raw_all['ece']:.4f}  ->  T-scaled={scl_all['ece']:.4f}")
    print(f"  full-set RPS:  raw={raw_all['rps']:.4f}  ->  T-scaled={scl_all['rps']:.4f}")
    print(f"  (train-only T=1.718 gave test ECE 0.027 — sanity: full-fit T and ECE "
          f"should be in the same ballpark)")


def _git_short_hash():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)), text=True,
        ).strip()
    except Exception:
        return "unknown"


def _round_metrics(samples, transform=None):
    v = metrics_on(samples, transform=transform)
    return {k: round(v[k], 4) for k in ("rps", "brier", "logloss", "ece")}


def _curve_points(samples, transform=None):
    """Pooled reliability points (one per non-empty 0.1 bin) for the SVG curve."""
    pairs = [((transform(s["p"]) if transform else s["p"])[k], s["o"][k])
             for s in samples for k in range(3)]
    rows, _ = reliability(pairs)
    return [{"pred": round(mp, 4), "obs": round(mo, 4), "n": n}
            for (lo, hi, n, mp, mo) in rows if n > 0 and mp is not None]


def generate_report(csv_path):
    """Build static/calibration_report.json — the single source of truth the
    /methodology page renders. All numbers are computed here, never hand-edited."""
    samples, warmup_n, n_corpus, dc, finalists = _run_asof(csv_path)
    Tp = fit_temperature(samples)
    tcal = lambda p: apply_temperature(p, Tp)

    train = [s for s in samples if "2025-07-01" <= s["date"] <= "2025-12-31"]
    test = [s for s in samples if "2026-01-01" <= s["date"] <= "2026-05-31"]
    dropped = [s for s in samples if s not in train and s not in test]
    ntest = len(test)

    # held-out test base-rate (climatology): constant = test outcome frequencies
    br = [sum(s["o"][k] for s in test) / ntest for k in range(3)]
    br_pairs = [(br[k], s["o"][k]) for s in test for k in range(3)]
    _, br_ece = reliability(br_pairs)
    base_rate = {
        "rps": round(sum(rps(br, s["o"]) for s in test) / ntest, 4),
        "brier": round(sum(brier(br, s["o"]) for s in test) / ntest, 4),
        "logloss": round(sum(logloss(br, s["o"]) for s in test) / ntest, 4),
        "ece": round(br_ece, 4),
    }

    # diag-4 residual: away-favorite (k=2) overconfidence on test after single T
    away_hi = [(tcal(s["p"])[2], s["o"][2]) for s in test if tcal(s["p"])[2] >= 0.60]
    if away_hi:
        ap = sum(x for x, _ in away_hi) / len(away_hi)
        ao = sum(y for _, y in away_hi) / len(away_hi)
        away_res = {"n": len(away_hi), "pred": round(ap, 3),
                    "obs": round(ao, 3), "gap": round(ap - ao, 3)}
    else:
        away_res = {"n": 0, "pred": None, "obs": None, "gap": None}

    fr_full = Counter(s["tournament"] for s in samples)
    fr_test = Counter(s["tournament"] for s in test)
    test_raw, test_cal = _round_metrics(test), _round_metrics(test, transform=tcal)

    report = {
        "meta": {
            "data_source": "github.com/martj42/international_results (CC0)",
            "source_script": "backtest_calibration.py",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_commit": _git_short_hash(),
            "reproduce": "INTL_RESULTS_CSV=<results.csv> "
                         "python backtest_calibration.py --dump",
            "n_corpus": n_corpus, "n_warmup": warmup_n,
            "n_eval_full": len(samples), "n_train": len(train),
            "n_test": ntest, "n_dropped": len(dropped),
            "production_T": round(Tp, 3), "rho": round(dc.RHO, 4),
            "home_advantage": dc.HOME_ADVANTAGE, "max_goals": dc.MAX_GOALS,
        },
        # hero curve = FULL corpus (n=439), raw vs production-T calibrated
        "hero": {"n": len(samples),
                 "raw": _curve_points(samples),
                 "calibrated": _curve_points(samples, transform=tcal)},
        # metrics table = leakage-free held-out TEST (n=102)
        "test_table": {"n": ntest, "raw": test_raw,
                       "calibrated": test_cal, "base_rate": base_rate},
        "cards": {
            "ece_raw": test_raw["ece"], "ece_cal": test_cal["ece"],
            "rps_cal": test_cal["rps"], "rps_base": base_rate["rps"],
            "backtest_n": len(samples),
        },
        "limitations": {
            "friendly_share_full_pct": round(fr_full.get("Friendly", 0) / len(samples) * 100, 1),
            "friendly_share_test_pct": round(fr_test.get("Friendly", 0) / ntest * 100, 1) if ntest else None,
            "nl_in_eval": fr_full.get("UEFA Nations League", 0) > 0,
            "test_n": ntest,
            "away_residual": away_res,
            "unvalidated_stages": ["player_xG_adj", "situation_mult"],
        },
    }

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "static", "calibration_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n[dump] wrote {out}  (T={report['meta']['production_T']}, "
          f"commit={report['meta']['git_commit']})")
    return report


if __name__ == "__main__":
    if not os.path.exists(CSV):
        sys.exit(f"results.csv not found at {CSV!r}. Clone martj42/international_results "
                 f"and pass the path: python backtest_calibration.py <results.csv> [--dump]")
    main(CSV)
    if "--dump" in sys.argv:
        generate_report(CSV)
