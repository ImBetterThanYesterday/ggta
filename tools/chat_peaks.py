#!/usr/bin/env python3
"""
chat_peaks.py — Encuentra los momentos más "virales" de un VOD de Twitch
a partir del chat descargado con TwitchDownloaderCLI.

Uso:
  TwitchDownloaderCLI chatdownload --id <VOD_ID> -o chat.json
  python3 chat_peaks.py chat.json --top 15 --window 10

Salida: lista de momentos (inicio/fin sugeridos del clip, score, ejemplos de
mensajes) + comandos `videodownload -b/-e` listos para bajar SOLO esos tramos.

Señales combinadas (no solo "cantidad de mensajes"):
  1. Densidad: mensajes por ventana, normalizada contra la media móvil del
     propio stream (z-score) -> un pico relativo, no absoluto.
  2. Chatters únicos por ventana -> evita que 1 bot/spammer infle el pico.
  3. Mensajes de "reacción" (KEKW, LUL, OMEGALUL, ???, CLIP, W, L, LMAO,
     JAJAJA...) -> distingue risa/shock de chat normal.
  4. Retraso de reacción: el chat reacciona ~5-20s DESPUÉS del evento, así
     que el clip arranca antes del pico.
"""
import argparse
import json
import re
import statistics
import sys
from collections import defaultdict

REACTION_PATTERNS = [
    r"\bKEKW\b", r"\bLUL\b", r"\bOMEGALUL\b", r"\bLULW\b", r"\bPogChamp\b", r"\bPOGGERS\b",
    r"\bPog\b", r"\bmonkaS\b", r"\bmonkaW\b", r"\bPepeLaugh\b", r"\bICANT\b", r"\bAware\b",
    r"\bCLIP\b", r"\bclip it\b", r"\bclipped\b", r"^\s*W\s*$", r"^\s*L\s*$", r"\bLMAO\b",
    r"\bLMFAO\b", r"\bWTF\b", r"\bNO WAY\b", r"\bja(ja)+\b", r"\bje(je)+\b", r"\bxd+\b",
    r"\bkk+k\b", r"\bpls\b", r"\?{3,}", r"!{3,}", r"\bDIOS\b", r"\bNOOO+\b", r"\bQUE\b",
]
REACTION_RE = re.compile("|".join(REACTION_PATTERNS), re.IGNORECASE)


def load_comments(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for c in data.get("comments", []):
        t = c.get("content_offset_seconds")
        if t is None:
            continue
        body = (c.get("message") or {}).get("body", "") or ""
        user = (c.get("commenter") or {}).get("_id") or (c.get("commenter") or {}).get("name", "?")
        out.append((float(t), user, body))
    out.sort(key=lambda x: x[0])
    return data, out


def fmt(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chat_json")
    ap.add_argument("--window", type=int, default=10, help="segundos por ventana")
    ap.add_argument("--baseline", type=int, default=300, help="segundos de media móvil")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--pre", type=int, default=25, help="segundos antes del pico")
    ap.add_argument("--post", type=int, default=15, help="segundos después del pico")
    ap.add_argument("--min-gap", type=int, default=90, help="separación mínima entre momentos")
    ap.add_argument("--json", action="store_true", help="salida JSON")
    args = ap.parse_args()

    data, comments = load_comments(args.chat_json)
    if not comments:
        sys.exit("No hay comentarios en el JSON.")

    W = args.window
    n_bins = int(comments[-1][0] // W) + 1
    msgs = [0] * n_bins
    reacts = [0] * n_bins
    users = defaultdict(set)
    samples = defaultdict(list)
    for t, u, body in comments:
        b = int(t // W)
        msgs[b] += 1
        users[b].add(u)
        if REACTION_RE.search(body):
            reacts[b] += 1
        if len(samples[b]) < 6:
            samples[b].append(body[:80])

    uniq = [len(users[b]) for b in range(n_bins)]
    k = max(1, args.baseline // W)

    def rolling_z(series):
        z = [0.0] * n_bins
        for i in range(n_bins):
            lo, hi = max(0, i - k), min(n_bins, i + k + 1)
            win = series[lo:i] + series[i + 1:hi]
            if len(win) < 3:
                continue
            mu = statistics.fmean(win)
            sd = statistics.pstdev(win) or 1.0
            z[i] = (series[i] - mu) / sd
        return z

    z_msgs, z_uniq, z_react = rolling_z(msgs), rolling_z(uniq), rolling_z(reacts)
    score = [0.35 * z_msgs[i] + 0.35 * z_uniq[i] + 0.30 * z_react[i] for i in range(n_bins)]

    # Picos con separación mínima (non-max suppression simple)
    order = sorted(range(n_bins), key=lambda i: score[i], reverse=True)
    picked, gap = [], args.min_gap // W
    for i in order:
        if score[i] <= 2.5:  # por debajo de esto suele ser ruido
            break
        if all(abs(i - j) > gap for j in picked):
            picked.append(i)
        if len(picked) >= args.top:
            break

    vod_id = (data.get("video") or {}).get("id", "<VOD_ID>")
    results = []
    for rank, i in enumerate(picked, 1):
        peak = i * W
        start, end = peak - args.pre, peak + W + args.post
        results.append({
            "rank": rank, "peak": fmt(peak), "start": fmt(start), "end": fmt(end),
            "score": round(score[i], 2), "msgs": msgs[i], "unique_chatters": uniq[i],
            "reactions": reacts[i], "samples": samples[i],
            "download": f"TwitchDownloaderCLI videodownload --id {vod_id} -b {fmt(start)} -e {fmt(end)} -q 1080p60 -o clip_{rank:02d}.mp4",
        })

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    print(f"{len(comments)} mensajes, {n_bins} ventanas de {W}s. Top {len(results)} momentos:\n")
    for r in results:
        print(f"#{r['rank']:>2}  pico {r['peak']}  clip {r['start']} → {r['end']}  "
              f"score {r['score']}  ({r['msgs']} msgs, {r['unique_chatters']} únicos, {r['reactions']} reacciones)")
        for s in r["samples"][:3]:
            print(f"       · {s}")
        print(f"       $ {r['download']}\n")


if __name__ == "__main__":
    main()
