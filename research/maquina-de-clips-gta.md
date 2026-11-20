# Máquina de clips GTA — Research y arquitectura

_Research: 19-nov-2026. Meta: 100M de vistas sumando TikTok, Instagram Reels y YouTube Shorts._

---

## 0. El dato más importante: el momento es AHORA

- **GTA 6 sale hoy, 19 de noviembre de 2026** (PS5 / Xbox Series; PC llega después).
- El lanzamiento ya es **el más visto en la historia del streaming**: se reportan picos de **~3.0–3.3M de espectadores simultáneos en Twitch** y **~7–7.9M sumando Twitch + YouTube + Kick**, según el tracker. Streamers con picos enormes: HoneyMad (~576k), alanzoka (~313k), xQc (~99k).
- Las próximas **4–8 semanas** son la ventana de más demanda de contenido GTA que va a haber en años. La gente busca clips, bugs, secretos, física, "cosas que no sabías"… Lo que se publique esta semana compite con poco catálogo y mucha demanda.

**Conclusión:** no construyas la máquina perfecta antes de publicar. Un pipeline semimanual que saque clips esta semana vale más que uno automático listo en enero.

---

## 1. Cómo se están haciendo hoy los reels automáticos de clips

### a) La "economía del clipping"
- Muchos streamers (sobre todo de Kick) **pagan a clippers** por vistas, a través de marketplaces como **Whop Content Rewards** o directo por Discord.
- Tarifas típicas: **~$1–3 por cada 1.000 vistas**. La mediana de 707 campañas activas en Content Rewards (10-nov-2026) era **~$1.09 / 1k vistas**; YouTube es la plataforma que mejor paga (mediana ~$1.43 / 1k).
- **Esto te resuelve el problema legal *y* el de monetización:** clipear streamers que *tienen campaña activa* = tienes permiso + te pagan por vista. 100M de vistas × $1/1k = **~$100k** solo en campañas, además del fondo de creadores de cada plataforma.

### b) Herramientas SaaS (rápidas, pero genéricas)
| Herramienta | Comentario |
|---|---|
| **Eklipse** | La más recomendada para gaming (detecta momentos por juego). |
| **StreamLadder** | Enfocada en VODs de Twitch/Kick/YouTube, plantillas facecam+gameplay. |
| **CutBench** | App de Windows, pago único (~$35 en Steam), corre local. |
| **OpusClip** | Tiene API (Twitch VOD → clips) y flujo "clipear donde explotó el chat". |
| Klap, Vizard, Submagic, Nexus Clips | Mejores para contenido hablado; Klap flojea con gameplay sin voz. |

Sirven para empezar hoy, pero todos sacan clips parecidos. **Tu ventaja tiene que ser la señal propia (chat) + criterio editorial + volumen.**

### c) Proyectos open source (la base para tu máquina)
| Repo | Qué hace | Por qué importa |
|---|---|---|
| **[clipsmith](https://github.com/ricardogr07/clipsmith)** (MIT) | VOD → transcripción **en español** (faster-whisper) → ranking por chat (densidad + menciones de clip + `!clip`) → LLM elige (soporta Claude) → corta 9:16 con facecam arriba / gameplay abajo → subtítulos karaoke. | **Es casi exactamente lo que quieres.** Mejor punto de partida que empezar de cero. |
| [clippy](https://github.com/hamzaskewl/clippy) | Detecta picos de chat **en vivo**, clasifica con IA y clipea. | Para clipear *mientras* el stream ocurre (velocidad = vistas). |
| [stream-clipper](https://github.com/nirvagold/stream-clipper) | Combina picos de audio + chat. | Buena idea: los gritos del streamer también son señal. |
| [HotClip](https://github.com/xixihhhh/hotclip) (AGPL) | Long video → shorts verticales, con detección por chat (Bilibili/Douyin). | Referencia de arquitectura; habría que adaptar el chat. |

---

## 2. TwitchDownloader: cómo usarlo bien

Repo: [lay295/TwitchDownloader](https://github.com/lay295/TwitchDownloader) (MIT). Tiene CLI multiplataforma (hay builds para macOS ARM). Última versión vista: 1.54.x (la 1.54.4 trajo cambios que rompen compatibilidad; `--silent` ahora es `--log-level None`).

```bash
# 1) Bajar SOLO el chat (rápido, pocos MB)
TwitchDownloaderCLI chatdownload --id <VOD_ID> -o chat.json

# 2) Bajar SOLO el tramo del clip (no el VOD entero de 8 horas)
TwitchDownloaderCLI videodownload --id <VOD_ID> -b 1:23:10 -e 1:24:00 -q 1080p60 -o clip.mp4
```

**Truco clave:** primero bajas el chat de muchos VODs (barato), detectas picos, y **solo descargas los 40–60 segundos que importan**. Así puedes procesar decenas de streams al día sin discos gigantes.

### Tu hipótesis ("donde más comenta el chat = momento viral") es correcta, con 4 ajustes
1. **Pico relativo, no absoluto.** Un stream de 300k espectadores siempre tiene mucho chat. Hay que comparar contra la media de ese mismo stream en los minutos alrededor (z-score).
2. **Contar chatters únicos**, no solo mensajes (un bot o un spammer inflan el conteo).
3. **Tipo de mensaje:** `KEKW`, `LUL`, `OMEGALUL`, `???`, `CLIP IT`, `JAJAJA`, `NO WAY`, `W`/`L`… separan risa/shock de chat normal. Cada idioma tiene los suyos.
4. **Retraso:** el chat reacciona 5–20 s *después* del momento (más el delay del stream). El clip tiene que empezar ~20–30 s antes del pico.

**Señales extra gratis:**
- **Clips que ya creó la comunidad** (Twitch API `Get Clips`): cada clip trae su `vod_offset`. Si 30 personas clipearon el mismo minuto, eso es oro validado por humanos.
- **Picos de audio** (gritos, risas) con ffmpeg/librosa.
- **Transcripción + LLM** para confirmar que el momento se entiende sin contexto (clave para shorts).

👉 **Prototipo ya hecho:** [tools/chat_peaks.py](../tools/chat_peaks.py) lee el `chat.json` de TwitchDownloader, combina esas señales y devuelve el top de momentos con los comandos `videodownload -b/-e` listos. Probado con datos sintéticos; falta probarlo con un VOD real.

```bash
python3 tools/chat_peaks.py chat.json --top 15
```

---

## 3. Arquitectura propuesta de la máquina

```
┌─ 1. DESCUBRIR ────────────────────────────────────────────────┐
│ Twitch Helix API: streams/VODs de la categoría GTA VI,       │
│ filtrados por idioma (es) y por streamers con permiso/campaña │
│ + Get Clips (clips más vistos de las últimas 24h)            │
└──────────────┬────────────────────────────────────────────────┘
┌─ 2. DETECTAR ─▼───────────────────────────────────────────────┐
│ TwitchDownloader chatdownload → chat_peaks.py                │
│ + vod_offset de clips de la comunidad + picos de audio       │
└──────────────┬────────────────────────────────────────────────┘
┌─ 3. EXTRAER ──▼───────────────────────────────────────────────┐
│ TwitchDownloader videodownload -b/-e (solo el tramo)         │
└──────────────┬────────────────────────────────────────────────┘
┌─ 4. ENTENDER ─▼───────────────────────────────────────────────┐
│ faster-whisper / WhisperX (timestamps por palabra)           │
│ Claude: ¿se entiende solo? score 1–10, punto exacto de        │
│ inicio/fin, gancho de texto (primeros 1–2 s), título, hashtags│
└──────────────┬────────────────────────────────────────────────┘
┌─ 5. EDITAR ───▼───────────────────────────────────────────────┐
│ ffmpeg: 9:16 1080×1920, facecam arriba / gameplay abajo      │
│ (plantilla por streamer; detección de cara opcional)         │
│ Subtítulos karaoke (.ass/libass), texto-gancho, 15–45 s      │
│ Alternativa más pro: Remotion (plantillas en React)          │
└──────────────┬────────────────────────────────────────────────┘
┌─ 6. REVISAR ──▼───────────────────────────────────────────────┐
│ Cola de aprobación humana (5 min/día): aprobar / descartar   │
│ Esto evita spoilers malos, contenido flojo y strikes         │
└──────────────┬────────────────────────────────────────────────┘
┌─ 7. PUBLICAR ─▼───────────────────────────────────────────────┐
│ YouTube Data API · TikTok Content Posting API (requiere      │
│ aprobación de app) · Instagram Graph API (cuenta business)   │
│ Mientras tanto: programador tipo Postiz (open source)        │
└──────────────┬────────────────────────────────────────────────┘
┌─ 8. APRENDER ─▼───────────────────────────────────────────────┐
│ Guardar vistas/retención por clip → ajustar pesos del score, │
│ qué streamers, qué tipo de momento, qué gancho funciona      │
└───────────────────────────────────────────────────────────────┘
```

**Recomendación:** forkear/estudiar **clipsmith** (ya hace 2–5 en español con Claude) y reemplazar su detección por `chat_peaks.py` + clips de la comunidad + TwitchDownloader para bajar solo tramos.

---

## 4. Qué tipos de video de GTA hacer (no solo clips de streamers)

Los clips de streamers dan volumen, pero **dependen de terceros** y YouTube no monetiza bien el contenido "reutilizado sin valor añadido". Conviene una mezcla:

| Formato | Fuente | Riesgo | Monetización |
|---|---|---|---|
| Clips de streamers (reacciones, fails, risas) | VODs + chat | Medio (necesitas permiso/campaña) | Campañas de clipping $ / 1k |
| "Cosas que no sabías de GTA 6" / easter eggs | Gameplay propio o clips + voz IA | Bajo | Fondos de creadores ✅ |
| Física / detalles GTA 6 vs GTA 5 | Gameplay propio, comparación lado a lado | Bajo | ✅ |
| Noticias/rumores (DLC, PC, Online) | Voz IA + b-roll | Bajo | ✅ |
| Compilados "Top 5 momentos del día" | Varios streamers | Medio | Parcial |

**Valor añadido = monetizable:** subtítulos, gancho propio, contexto, comentario, montaje. Un clip crudo resubido es lo que las plataformas penalizan.

---

## 5. Matemática de 100M de vistas

- 3 plataformas × 4 videos/día × 90 días ≈ **1.080 publicaciones**.
- Promedio necesario: **~93k vistas por publicación**. Realista no es "todos sacan 93k": en shorts manda la ley de potencias — unos **20–40 videos que pasan de 1M** hacen la mayor parte, el resto suma poco.
- Por eso la máquina tiene que optimizar **volumen + aprendizaje rápido** (qué funciona, repetir el formato).
- Palancas: idioma (español está menos saturado que inglés; luego replicar en portugués/inglés con la misma máquina), velocidad (publicar a horas del momento en vivo), ganchos en el primer segundo, retención > 70%.

---

## 6. Riesgos y reglas

- **Copyright / permiso de streamers:** clipea a quien tenga campaña de clipping o te dé permiso. Sin eso → reclamos, strikes, cuentas tiradas.
- **Políticas de plataformas:** YouTube restringe la monetización de contenido reutilizado/repetitivo sin aporte; TikTok e IG bajan el alcance de reposts sin edición. Varias cuentas casi idénticas publicando lo mismo pueden marcarse como spam.
- **Spoilers de la historia:** en semana de lanzamiento, los spoilers de final generan odio y denuncias. Filtra (el LLM puede marcarlos).
- **APIs de publicación:** TikTok exige revisión de la app para publicar públicamente vía API; mientras, publicación manual o programador.

---

## 7. Plan de arranque sugerido

**Hoy–esta semana (manual asistido, aprovechar el lanzamiento):**
1. Elegir 5–10 streamers de GTA 6 en español con campaña activa o permiso.
2. Bajar chats de sus VODs de lanzamiento → `chat_peaks.py` → bajar tramos.
3. Editar con StreamLadder/CapCut o el pipeline de clipsmith; publicar 3–5 por día en las 3 plataformas.

**Semanas 2–3 (automatizar):**
4. Script de descubrimiento con la API de Twitch + clips de la comunidad.
5. Pipeline automático 3→5 (corte, transcripción, Claude, render 9:16 con subtítulos).
6. Cola de aprobación + tracking de métricas.

**Mes 2+ (escalar):**
7. Formatos propios (curiosidades, comparaciones) con voz IA.
8. Replicar en otros idiomas; ajustar el score con datos reales de vistas.

---

## Fuentes
- Lanzamiento y récords: [Streams Charts](https://streamscharts.com/news/gta-6-streaming-records), [Streams Charts – récords en minutos](https://streamscharts.com/news/gta-vi-breaks-streaming-launch-records-within-minutes-its-release), [Wccftech](https://wccftech.com/grand-theft-auto-vi-twitch-viewers-record-league-of-legends/), [DualShockers](https://www.dualshockers.com/gta-6-just-broke-twitch-s-biggest-viewership-record-in-just-30-minutes/), [GamesRadar – fecha](https://www.gamesradar.com/games/grand-theft-auto/gta-6-delayed-6-more-months-as-rockstar-commits-to-november-2026-and-says-sorry)
- Clipping economy: [Content Rewards – cuánto ganan los clippers](https://contentrewards.com/articles/how-much-do-clippers-make), [OpusClip – Whop Content Rewards](https://www.opus.pro/blog/whop-content-rewards), [Digiday](https://digiday.com/media/the-case-for-and-against-clipping/), [Michigan Public](https://www.michiganpublic.org/2026-05-12/the-clipping-economy-how-short-form-video-clippers-are-overrunning-the-internet)
- Herramientas: [Eklipse – alternativas a OpusClip](https://eklipse.gg/compare/opusclip-alternatives/), [OpusClip – Twitch VOD API](https://www.opus.pro/blog/twitch-vod-to-clips-api), [OpusClip – picos de chat](https://www.opus.pro/mcp/inspiration/clip-the-moments-your-live-chat-exploded)
- Open source: [clipsmith](https://github.com/ricardogr07/clipsmith), [clippy](https://github.com/hamzaskewl/clippy), [stream-clipper](https://github.com/nirvagold/stream-clipper), [HotClip](https://gitee.com/xiaohaohu/hotclip)
- TwitchDownloader: [repo](https://github.com/lay295/TwitchDownloader), [CLI README](https://github.com/lay295/TwitchDownloader/blob/master/TwitchDownloaderCLI/README.md), [releases](https://github.com/lay295/TwitchDownloader/releases)
