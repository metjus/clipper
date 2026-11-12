# AI clipper

Vložíš linky na Twitch klipy, dostaneš hotové vertikálne video pre TikTok:
kompilácia v štýle veľkých clip kanálov (CaseOh, xQc…) s titulkami slovo po
slove, zoomami, otrasmi, zvukovými efektmi, hookom, popisom a hashtagmi.

Všetko beží lokálne na tvojom počítači. Jediná externá služba je Claude
(Anthropic API), ktorý robí „režiséra“: vyberá poradie klipov, strihy, momenty
na efekty, hook a hashtagy. Bez API kľúča to funguje tiež, len s jednoduchšou
heuristikou.

## Ako to funguje

1. **yt-dlp** stiahne klipy (Twitch, Kick, YouTube alebo súbor z disku).
2. **faster-whisper** (lokálne) prepíše reč so začiatkom a koncom každého slova.
3. Analýza zvuku nájde najhlasnejšie momenty (výkriky, rage, smiech) a detektor
   tvárí nájde facecam.
4. **Claude** dostane prepisy, peaky a metadáta a vráti plán strihu
   (JSON validovaný cez Pydantic): poradie, orezy, zoomy, otrasy, SFX, flashe,
   zvýraznené slová, hook, popis a hashtagy.
5. **FFmpeg** vyrenderuje 1080×1920 video: layout facecam hore / hra dole
   (alebo rozmazané pozadie), jump-cuty ticha, farebný look, titulky (libass),
   zoompan efekty, mix SFX, hudba na pozadí stíšená pod rečou, progress bar,
   normalizácia hlasitosti na -14 LUFS.

Výstup: `~/.clipper/output/<názov>.mp4` + `<názov>.jpg` (cover) +
`<názov>.json` s popisom a hashtagmi.

## Čo vie

**Vstup**
- Twitch klipy, Kick klipy, YouTube, súbory z disku (drag & drop do UI).
- 🔎 *Top klipy streamera*: zadáš meno kanála (Twitch / Kick) a obdobie, vyberieš
  klipy zo zoznamu s počtom videní.
- Ak má Twitch klip aj vertikálnu verziu od tvorcu (portrait), použije sa.

**Kvalita videa**
- Jump-cuty: ticho dlhšie ako ~0,6 s sa vystrihne (momenty s krikom a zoomom sú chránené).
- Titulky slovo po slove, 4 animácie: *pop* (MrBeast), *karaoke*, *bounce*, *minimal*;
  3 fonty, farba zvýrazneného slova a dôrazu, živý náhľad v UI.
- Zoomy, otrasy, flashe a SFX podľa Claude plánu a najhlasnejších momentov.
- Farebný look: punchy / warm / cold / vintage.
- Hudba na pozadí z `~/.clipper/music/` so sidechain duckingom pod rečou.
- Progress bar na spodnom okraji, cover obrázok z najlepšieho momentu.
- Oddeľovacia linka medzi facecamom a hrou, normalizácia na -14 LUFS.

**UI**
- Editor facecamu: potiahneš rámček na webkameru, ak ju detekcia netrafila.
- Editor plánu: poradie klipov, orezy, layout, rýchlosť, hook, popis, hashtagy,
  plus JSON pre zoomy/SFX. Prerender bez opakovania prepisu.
- Zrušenie bežiaceho jobu, história po reštarte, test API kľúča,
  tlačidlo na otvorenie výstupného priečinka, zapamätané nastavenia.

## Spustenie na Windows

1. Nainštaluj [Python 3.11+](https://www.python.org/downloads/) (zaškrtni *Add python.exe to PATH*).
2. Nainštaluj ffmpeg: v PowerShelli `winget install -e --id Gyan.FFmpeg` (alebo nechaj `run.bat`, nech to skúsi sám).
3. Stiahni / naklonuj toto repo a spusti **`run.bat`**. Otvorí sa prehliadač na `http://127.0.0.1:8787`.
4. V ⚙ Nastaveniach vlož Anthropic API kľúč (uloží sa len lokálne do `%USERPROFILE%\.clipper\config.json`).
5. Vlož linky na klipy, nastav dĺžku a štýl, klikni **Vyrobiť video**.

Prvý beh stiahne Whisper model (`small` ≈ 480 MB). Na CPU trvá prepis
30-sekundového klipu zhruba 10–20 s; s NVIDIA GPU je to pár sekúnd, treba
`pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` a v nastaveniach
zariadenie `cuda`. Render používa `h264_nvenc`, ak je k dispozícii.

### Linux / macOS

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
clipper web            # alebo: clipper run <url> <url> --target 45
```

## Príkazový riadok

```bash
clipper doctor                                   # skontroluje ffmpeg, whisper, API kľúč
clipper settings --set anthropic_api_key=sk-...  # alebo premenná prostredia ANTHROPIC_API_KEY / súbor .env
clipper discover xqc --range 7d                  # top klipy kanála
clipper run https://clips.twitch.tv/... https://clips.twitch.tv/... --target 45 --preset karaoke --look warm --music random
clipper web --port 8787
```

## Nastavenia

| Kľúč | Čo robí | Predvolené |
|---|---|---|
| `anthropic_api_key` | Claude API kľúč | – |
| `claude_model` | model pre režiséra | `claude-opus-5-5` |
| `whisper_model` | `tiny`/`base`/`small`/`medium`/`large-v3` | `small` |
| `whisper_device` | `auto`/`cpu`/`cuda` | `auto` |
| `video_encoder` | `auto`/`libx264`/`h264_nvenc` | `auto` |
| `output_dir` | kam sa ukladajú videá | `~/.clipper/output` |

Vlastné SFX: hoď `boom.wav`, `whoosh.wav`, `ding.wav`, `pop.wav`, `riser.wav`,
`record_scratch.wav` alebo `bass_drop.wav` do `~/.clipper/sfx/` a nahradia
vstavané (syntetizované) zvuky.

## Úprava plánu

Po renderi sa v UI dá rozbaliť plán strihu (JSON), upraviť ho (napr. iný hook,
posunúť zoom, vyhodiť klip) a kliknúť **Prerenderovať**. Sťahovanie ani prepis
sa neopakujú.

## Štruktúra

```
clipper/
  downloader.py   yt-dlp
  transcribe.py   faster-whisper, slová s časom
  media.py        ffprobe, peaky hlasitosti, detekcia facecam (OpenCV YuNet)
  director.py     Claude (structured output -> EditPlan) + heuristická záloha
  silence.py      detekcia ticha + jump-cut remapovanie časov
  captions.py     ASS titulky slovo po slove (presety pop/karaoke/bounce/minimal)
  sfx.py          syntetizované zvukové efekty
  render.py       FFmpeg filtergraph: layout, zoompan, flash, ass, amix
  pipeline.py     orchestrácia jobov
  web/            FastAPI + jednostránkové UI
  cli.py
```

## Plánované

- Automatický upload na TikTok s popisom a hashtagmi (TikTok Content Posting API vyžaduje schválenú appku).
- Výber najlepších momentov z celého VOD.
