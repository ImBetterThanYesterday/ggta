"""Sound effects for edit accents.

Licensed files dropped in clipsmith/assets/sfx/<name>.wav|mp3 win (list their licence in
assets/sfx/MANIFEST.md). Otherwise each effect is synthesized with ffmpeg, so it is our own
sound and can't trigger Content ID.
"""

from __future__ import annotations

import subprocess  # nosec B404
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[3] / "assets" / "sfx"

# name -> (lavfi source, extra filters, gain in dB when mixed under speech)
_SYNTH: dict[str, tuple[str, str, float]] = {
    "whoosh": ("anoisesrc=d=0.55:c=pink:a=0.7:r=48000",
               "highpass=f=350,lowpass=f=5000,afade=t=in:d=0.35:curve=exp,afade=t=out:st=0.35:d=0.2", -9.0),
    "boom": ("aevalsrc='0.95*sin(2*PI*(62-34*t)*t)*exp(-4.5*t)+0.25*(random(0)-0.5)*exp(-60*t)':d=0.9:s=48000",
             "lowpass=f=900", -5.0),
    "riser": ("aevalsrc='0.35*sin(2*PI*(180+700*t*t)*t)*min(1,t*1.2)':d=1.1:s=48000",
              "afade=t=out:st=0.95:d=0.15", -12.0),
    "pop": ("aevalsrc='0.8*sin(2*PI*(950-650*t)*t)*exp(-28*t)':d=0.18:s=48000", "", -8.0),
}


def gain_db(name: str) -> float:
    return _SYNTH.get(name, ("", "", -8.0))[2]


def sfx_path(name: str, cache_dir: Path) -> Path | None:
    for ext in ("wav", "mp3"):
        f = ASSETS / f"{name}.{ext}"
        if f.exists():
            return f
    if name not in _SYNTH:
        return None
    out = cache_dir / f"sfx_{name}.wav"
    if out.exists():
        return out
    cache_dir.mkdir(parents=True, exist_ok=True)
    src, filt, _ = _SYNTH[name]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-ac", "2"]
    if filt:
        cmd += ["-af", filt]
    subprocess.run([*cmd, str(out)], check=True)  # nosec B603
    return out
