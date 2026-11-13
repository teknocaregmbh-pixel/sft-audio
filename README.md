# sft-audio

Ready-made spoken audio for **swissfinancialtime.ch**, read by the free *Heart* voice
([Kokoro](https://huggingface.co/hexgrad/Kokoro-82M), Apache-2.0).

Every 30 minutes the workflow in `.github/workflows/audio.yml`:

1. reads the latest posts from the Blogger feed,
2. makes the news briefing (one MP3 per headline) and one MP3 per new article (`generate.py`),
3. publishes them on the `audio` branch, which GitHub Pages serves at
   `https://<user>.github.io/sft-audio/` (`index.json` lists all files).

The website's reader plays these files instantly. If an article has no file yet,
the reader makes the Heart voice in the visitor's browser instead.

Settings (environment variables in the workflow): `KEEP_ARTICLES` (default 40),
`NEW_PER_RUN` (default 12).
