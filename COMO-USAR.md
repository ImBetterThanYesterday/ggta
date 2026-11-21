# Máquina de clips: cómo usarla

Le pasas un link de **Kick, YouTube o Twitch**. La máquina:

1. baja el video y el chat;
2. busca los mejores momentos (picos de chat, gritos del streamer, palabras como "jajaja" o "no way", y en YouTube el gráfico de "lo más visto");
3. le pide a Claude que elija los que se entienden solos y que les ponga título;
4. los corta en formato vertical 9:16 con subtítulos.

## Todos los streamers de una vez

```bash
./clip batch                                        # último directo de GTA terminado de cada uno en streamers.yaml
./clip batch --only Westcol --only Spreen --top 12  # solo algunos
```

Busca el último directo **terminado** de cada streamer en YouTube, Twitch y Kick, y lo procesa entero. Cada clip queda en **su propia carpeta**:

```
clipsmith/out/<streamer>_<directo>/<hora>_<titulo>/
    video.mp4      ← listo para subir
    publicar.txt   ← título, descripciones por red, hashtags, crédito, link al minuto exacto, checklist
```

El idioma del gancho, la voz, el título y los hashtags sale del campo `lang` de `streamers.yaml`: los streamers en inglés quedan en inglés. Los subtítulos siempre van en el idioma en que habla el streamer.

**Cantando:** cuando el chat se llena de "temazo", "🎵", "canta" o "song", la máquina lo marca como posible momento en que el streamer está cantando. Esos momentos se procesan primero porque es un formato muy viral.

## Directos terminados (recomendado)

```bash
./clip vod "https://www.youtube.com/watch?v=biUVW6Q0f2g" --top 8
./clip vod "<link>" --only-peaks        # solo muestra dónde explotó el chat, sin cortar nada
```

El proceso:

1. Baja **todo el chat** del directo. Pesa poco y tarda entre 5 y 20 min en un directo de 12 h.
2. Busca los picos del chat comparándolos con el ritmo normal de ese mismo stream. No cuenta los "participo" de los sorteos, el spam ni la gente diciendo su hora local, y da más peso a las risas y reacciones.
3. Baja **solo esos 2 minutos** de cada pico, en 1080p.
4. La IA lee la transcripción y lo que escribió el chat. Descarta lo flojo y corta lo bueno en 20–60 s.
5. Renderiza con la cara del streamer arriba, el juego abajo y subtítulos estilo TikTok.

Cada clip queda en su propia carpeta, `clipsmith/out/<canal>_<título>/<hora>_<titulo>/`, con `video.mp4` y `publicar.txt`.

Si YouTube solo guardó las primeras ~12 h de un directo más largo, la máquina ignora el chat posterior.

**Bulla (gritos, risas, explosiones):** está activada por defecto. La máquina baja **solo el audio** del directo en la calidad más baja (12 h ≈ 250 MB, unos 2–3 min) y busca dónde sube de golpe por encima de lo normal de ese stream. Con eso:

- Si el chat y la bulla coinciden, el momento pasa al principio de la lista.
- Si solo hay bulla, el momento entra como candidato extra.

Para desactivarla, usa `--no-bulla`.

**Repetir sin duplicar:** si vuelves a correr el mismo directo, la máquina se salta los momentos que ya procesó.

**Gancho:** la IA decide en cada clip entre dos tipos:

- **Voz + texto:** cuando el inicio está tranquilo y necesita contexto.
- **Solo texto:** cuando el streamer ya está gritando o hablando y una voz encima estorbaría.

Junto a cada video queda un `.txt` con título, descripción y hashtags listos para publicar.

**Cámara del streamer:** la máquina detecta el recuadro completo de la webcam (la parte de la imagen que casi no cambia alrededor de la cara), así que nunca corta la cara a la mitad. Si en un momento no encuentra la cara, usa la posición de la cámara que encontró en otro momento del mismo stream.

**Volver a renderizar** (después de una mejora, sin bajar nada ni gastar IA):

```bash
./clip rerender                 # todos los clips de clipsmith/out/
./clip rerender pileta grefg    # solo las carpetas que contengan esas palabras
```

Además de `video.mp4`, cada clip queda con un `video_web.mp4` de menos de 10 MB y la mejor calidad posible, para subirlo desde el navegador.

## Publicar (Upload-Post)

Una sola vez: pega tu API key en `clipsmith/.env`, en la línea `UPLOAD_POST_API_KEY=`.

```bash
./clip publish --dry-run --n 5                 # muestra qué subiría, sin subir nada
./clip publish                                 # sube el siguiente clip a YouTube + Facebook
./clip publish --n 4 --every 3                 # 4 clips, programados cada 3 horas
./clip publish --only spreen                   # solo clips cuya carpeta diga "spreen"
./clip publish -p youtube -p facebook -p instagram   # cuando Instagram quede conectado
./clip publish-status <request_id>             # cómo fue la subida en cada red
```

- Toma los clips que aún no se han subido y alterna entre streamers para que no salgan seguidos los del mismo.
- Al subir un clip, deja un `publicado.json` en su carpeta, así nunca sube el mismo dos veces.
- **Plan gratis de Upload-Post:** 10 subidas al mes y sin TikTok. Para subir todos los días hace falta el plan Basic (24 USD al mes).

## Otros comandos

```bash
cd ~/Documents/content-factory
./clip "https://kick.com/xqc/videos/<uuid>" --start 1:00:00 --end 2:00:00
./clip "https://www.youtube.com/watch?v=<id>" --start 30:00 --end 1:30:00
./clip "https://www.twitch.tv/videos/<id>"
```

Los clips salen en `clipsmith/out/<id>/`.

**Usa siempre `--start` y `--end`.** Un stream de 8 horas pesa mucho y tarda mucho; es mejor procesarlo por tramos de 1 a 2 horas.

### Otras opciones útiles

| Opción | Para qué |
|---|---|
| `--only-fetch` | Solo baja el video y el chat (para revisar). |
| `--skip-select` | Calcula los candidatos, pero no llama a Claude ni corta. |
| `--max-height 720` | Baja a 720p (más rápido, y suficiente para vertical). |
| `--provider ollama` | Usa una IA local y gratis en vez de Claude. |

## Radar: quién está en vivo ahora

```bash
./clip radar                 # todos los de streamers.yaml jugando GTA
./clip radar --lang es       # solo habla hispana
./clip radar --game ""       # cualquier juego
```

La lista de streamers está en [streamers.yaml](streamers.yaml): unos 40 streamers grandes en inglés y en español, con sus canales de YouTube, Twitch y Kick ya verificados. Puedes agregar o quitar gente ahí.

## Modo en vivo: clipear mientras transmiten

```bash
./clip live                          # cada 20 min: los 3 streams GTA top, últimos 20 min de cada uno
./clip live --lang es --top 5        # solo hispanos, los 5 con más viewers
./clip live --only IShowSpeed --only Westcol --window 15 --every 15
./clip live --once                   # una sola pasada
```

Lleva la cuenta de qué tramos ya procesó (en `clipsmith/work/live_state.json`), así que nunca repite un tramo.

Cómo se obtiene el chat según la plataforma:

- **Kick y Twitch:** el chat del stream en curso se puede leer completo.
- **YouTube en vivo:** el chat no se puede leer hasta que el stream termina. Mientras está en vivo, la máquina detecta los momentos por los gritos del streamer y lo que dice.

## IA: usar tu suscripción en vez de pagar API

En `clipsmith/config.yaml`, bajo `llm.provider`:

| Proveedor | Usa | Estado |
|---|---|---|
| `codex_cli` | Tu suscripción de **ChatGPT** (Codex CLI) | ✅ Funciona; es el que está activo |
| `claude_cli` | Tu suscripción de **Claude** (Claude Code) | Necesita login: en una terminal corre `claude` y luego `/login` |
| `anthropic` / `openai` | Las API (se paga por uso) | Necesita una API key en `.env` |
| `ollama` | Una IA local y gratis (menos lista) | Necesita instalar Ollama |

Con la suscripción no pagas tokens, pero cada momento tarda unos 20 s y gasta del límite de uso de tu plan.

## Instalación (primera vez, en un computador nuevo)

```bash
git clone https://github.com/ImBetterThanYesterday/ggta.git
cd ggta/clipsmith
uv venv && uv pip install -e ".[dev]" "yt-dlp[default]" edge-tts pillow "opencv-python-headless<5"
cp .env.example .env
cd ..
./clip --help
```

Necesitas:

- **uv**, **ffmpeg** y **Node.js**. En Mac: `brew install uv ffmpeg node`.
- **Python:** `uv venv` usa Python 3.12 automáticamente. Python 3.14 todavía no funciona.
- **La primera vez** se baja el modelo de voz a texto, que pesa unos 500 MB.

## Configuración de una sola vez

1. **IA:** no hace falta nada si usas `codex_cli` (ya tienes sesión de ChatGPT). Para Claude, corre `claude` en una terminal y haz `/login`. Solo si quieres usar la API, crea `clipsmith/.env` con `ANTHROPIC_API_KEY=...`.
2. **Videos de YouTube con restricción de edad** (la mayoría de los de GTA): usa las cookies de tu navegador, donde ya tienes la sesión de YouTube iniciada:
   ```bash
   export CLIPSMITH_COOKIES_BROWSER=chrome   # o firefox, safari, brave
   ```
3. **Opcional, Twitch:** pon `TWITCH_CLIENT_ID` y `TWITCH_CLIENT_SECRET` en `.env`. Con eso la máquina usa también los clips que ya hizo la comunidad, que son la mejor señal que hay.

## Ajustes (clipsmith/config.yaml)

- `clip.min_seconds` / `max_seconds`: duración de cada clip (ahora entre 25 y 60 s).
- `caption.enabled`: subtítulos quemados en el video (activados).
- `reframe.mode`:
  - `center`: recorte vertical al centro.
  - `stacked`: cámara del streamer arriba y juego abajo. Primero hay que medir la posición de la cámara con `./clip detect-webcam <id>`.
- `candidates.hype_keywords`: palabras de emoción del streamer. Agrega las de cada idioma.

## Qué hace cada plataforma

| | Video | Chat | Señal extra |
|---|---|---|---|
| **Kick** | Sí (HLS directo de la API) | Sí (API de replay, ~4 peticiones/s: 1 h de chat ≈ 3 min) | — |
| **YouTube** | Sí (yt-dlp, H.264) | Sí, si fue un directo con chat guardado | "Lo más visto" (heatmap), cuando el video lo tiene |
| **Twitch** | Sí (yt-dlp) | Sí (GQL de Twitch) | Clips de la comunidad (requiere credenciales) |

## Cuidado

- Clipea a streamers que **te den permiso** o que tengan campaña de clipping (Whop / Content Rewards). Si no, te arriesgas a reclamos de copyright y a perder la cuenta.
- Revisa los clips antes de publicarlos. Ojo con los spoilers de la historia esta semana.
