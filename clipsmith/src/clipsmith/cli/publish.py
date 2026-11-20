"""`clipsmith publish` — send packaged clips (out/<stream>/<clip>/) to Upload-Post."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import typer
from rich.console import Console

console = Console()
_API = "https://api.upload-post.com/api"
_DONE = "publicado.json"


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    env = Path(".env")
    if not val and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
    return val


def _api_key() -> str:
    key = _env("UPLOAD_POST_API_KEY")
    if not key:
        raise typer.BadParameter("Falta UPLOAD_POST_API_KEY en clipsmith/.env")
    return key


def _sections(txt: str) -> dict[str, str]:
    out, cur = {}, None
    for line in txt.splitlines():
        m = re.match(r"^=== (.+?) ===$", line.strip())
        if m:
            cur = m.group(1)
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _post_data(folder: Path) -> dict:
    s = _sections((folder / "publicar.txt").read_text(encoding="utf-8"))
    pick = lambda prefix: next((v for k, v in s.items() if k.startswith(prefix)), "")
    title = pick("TÍTULO").splitlines()[0][:100] if pick("TÍTULO") else folder.name
    tags = [t.lstrip("#") for t in pick("HASHTAGS").split() if t.startswith("#")]
    social = pick("TikTok")
    if "#shorts" not in pick("DESCRIPCIÓN YouTube").lower():
        yt_desc = pick("DESCRIPCIÓN YouTube") + " #shorts"
    else:
        yt_desc = pick("DESCRIPCIÓN YouTube")
    return {"title": title, "yt_desc": yt_desc[:4900], "social": social[:2100], "tags": tags[:15]}


def _done_platforms(folder: Path) -> set[str]:
    f = folder / _DONE
    if not f.exists():
        return set()
    d = json.loads(f.read_text(encoding="utf-8"))
    return set(d.get("ok") or d.get("platforms") or [])


def _pending(out_dir: Path, only: list[str], platforms: list[str] | None = None) -> list[Path]:
    want = set(platforms or [])
    folders = [p.parent for p in out_dir.glob("*/*/video.mp4")
               if (p.parent / "publicar.txt").exists()
               and not (want or {"_"}) <= _done_platforms(p.parent)]
    if only:
        folders = [f for f in folders if any(o.lower() in str(f).lower() for o in only)]
    # Interleave streams so consecutive posts aren't all the same streamer
    by_stream: dict[str, list[Path]] = {}
    for f in sorted(folders):
        by_stream.setdefault(f.parent.name, []).append(f)
    order = []
    while any(by_stream.values()):
        for k in list(by_stream):
            if by_stream[k]:
                order.append(by_stream[k].pop(0))
    return order


def publish_cmd(
    n: int = typer.Option(1, "--n", help="Cuántos clips publicar en esta corrida"),
    platform: list[str] = typer.Option(["youtube", "facebook"], "--platform", "-p",
                                       help="youtube | facebook | instagram | tiktok (repetible)"),
    only: list[str] = typer.Option([], "--only", help="Solo carpetas que contengan este texto"),
    every_h: float = typer.Option(0, "--every", help="Programar: horas entre cada clip (0 = ya)"),
    delay_h: float = typer.Option(0, "--delay", help="Programar el primero dentro de N horas"),
    privacy: str = typer.Option("public", "--privacy", help="YouTube: public | unlisted | private"),
    user: str = typer.Option("default", "--user", help="Perfil de Upload-Post"),
    out_dir: Path = typer.Option(Path("out"), "--out"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo mostrar qué se subiría"),
) -> None:
    """Publish the next N unpublished clips to the connected networks."""
    todo = _pending(out_dir, only, platform)[:n]
    if not todo:
        console.print("[yellow]No hay clips pendientes de publicar.[/yellow]")
        return
    key = "" if dry_run else _api_key()
    start = datetime.now(UTC) + timedelta(minutes=10, hours=delay_h)
    for i, folder in enumerate(todo):
        d = _post_data(folder)
        when = start + timedelta(hours=every_h * i) if (every_h or delay_h) else None
        console.rule(f"[bold]{folder.parent.name}/{folder.name}[/bold]")
        console.print(f"Título: {d['title']}\nRedes: {', '.join(p for p in platform if p not in _done_platforms(folder))}"
                      + (f"\nProgramado: {when:%Y-%m-%d %H:%M} UTC" if when else ""))
        if dry_run:
            continue
        data: list[tuple[str, str]] = [("user", user), ("title", d["title"]),
                                       ("async_upload", "true"),
                                       ("external_id", f"{folder.parent.name}/{folder.name}"[:255])]
        done = _done_platforms(folder)
        targets = [p for p in platform if p not in done]
        data += [("platform[]", p) for p in targets]
        if "youtube" in platform:
            data += [("youtube_description", d["yt_desc"]), ("privacyStatus", privacy),
                     ("categoryId", "20")]  # 20 = Gaming
            data += [("tags[]", t) for t in d["tags"]]
        if "facebook" in platform:
            page = _env("UPLOAD_POST_FB_PAGE")
            if not page:
                raise typer.BadParameter("Falta UPLOAD_POST_FB_PAGE en .env (página de Facebook)")
            data += [("facebook_description", d["social"]), ("facebook_media_type", "REELS"),
                     ("facebook_page_id", page)]
        if "instagram" in platform or "tiktok" in platform:
            data += [("description", d["social"])]
        if when:
            data += [("scheduled_date", when.strftime("%Y-%m-%dT%H:%M:%SZ"))]
        form: dict[str, list[str]] = {}
        for k, v in data:
            form.setdefault(k, []).append(v)
        with open(folder / "video.mp4", "rb") as fh:
            r = httpx.post(f"{_API}/upload", headers={
                "Authorization": f"Apikey {key}",
                # one key per clip + target networks + attempt hour: retries of a failed
                # network must not be deduplicated into the original job
                "Idempotency-Key": re.sub(r"[^A-Za-z0-9_-]", "_",  # headers must be ASCII
                                          f"{folder.parent.name}_{folder.name}"
                                          f"_{'-'.join(targets)}_{datetime.now(UTC):%Y%m%d%H}")[-120:],
            }, data=form, files={"video": ("video.mp4", fh, "video/mp4")}, timeout=600)
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:500]}
        if r.status_code >= 400 or not body.get("success", False):
            console.print(f"[red]Error {r.status_code}:[/red] {json.dumps(body, ensure_ascii=False)[:600]}")
            if r.status_code in (401, 403, 429):
                break  # key/plan/limit problem: no point trying the rest
            continue
        rid = body.get("request_id") or body.get("job_id") or ""
        ok = sorted(done | set(targets)) if when else sorted(done | _wait_results(key, rid, targets))
        (folder / _DONE).write_text(json.dumps(
            {"at": datetime.now(UTC).isoformat(), "ok": ok,
             "scheduled": when.isoformat() if when else None, "request_id": rid},
            ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[green]Enviado[/green] {rid} — publicado en: {', '.join(ok) or 'ninguna'}")


def _wait_results(key: str, rid: str, targets: list[str]) -> set[str]:
    """Poll the async upload; return platforms that succeeded and print each result."""
    import time

    for _ in range(40):
        r = httpx.get(f"{_API}/uploadposts/status", params={"request_id": rid},
                      headers={"Authorization": f"Apikey {key}"}, timeout=60).json()
        if r.get("status") == "completed" or r.get("completed", 0) >= len(targets):
            ok = set()
            for res in r.get("results") or []:
                if res.get("success"):
                    ok.add(res["platform"])
                    console.print(f"  [green]✓ {res['platform']}[/green] {res.get('post_url') or ''}")
                else:
                    console.print(f"  [red]✗ {res['platform']}[/red] {res.get('error_message', '')[:160]}")
            return ok
        time.sleep(15)
    console.print("  [yellow]Sigue procesando; revisa con publish-status[/yellow]")
    return set()


def status_cmd(
    request_id: str = typer.Argument(..., help="request_id o job_id que devolvió publish"),
) -> None:
    """Check how an upload went on each network."""
    r = httpx.get(f"{_API}/uploadposts/status", params={"request_id": request_id},
                  headers={"Authorization": f"Apikey {_api_key()}"}, timeout=60)
    console.print_json(r.text)
