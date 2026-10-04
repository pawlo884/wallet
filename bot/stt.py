"""Mowa → tekst lokalnie (faster-whisper na CPU). Nagrania nie opuszczają serwera.

Model pobiera się raz z Hugging Face do wolumenu danych (HF_HOME) i jest ładowany
w tle przy starcie, żeby pierwsza głosówka nie czekała na pobieranie.
"""

import asyncio
import io
import logging
import os

log = logging.getLogger(__name__)

# Podpowiedź dla Whispera: format kwot i typowe słowa — liczby wychodzą wtedy cyframi.
PROMPT = "Biedronka 54,30 zł. Paliwo 250 zł na Orlenie wczoraj. Kawa 14 zł i ciastko 9. Anthropic 15 dolarów."


class STT:
    def __init__(self, model: str, threads: int):
        self._name = model
        self._threads = threads
        self._model = None
        self._load_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()  # jedna transkrypcja naraz — CPU i RAM są ograniczone

    async def preload(self) -> None:
        try:
            await self._get()
        except Exception:
            log.exception("Nie udało się załadować modelu mowy %s", self._name)

    async def _get(self):
        async with self._load_lock:
            if self._model is None:
                log.info("Ładowanie modelu mowy %s (pierwszy raz pobiera ~0,5 GB)…", self._name)
                self._model = await asyncio.to_thread(self._load)
                log.info("Model mowy gotowy")
            return self._model

    def _load(self):
        from faster_whisper import WhisperModel  # import tu — ciężka biblioteka, niepotrzebna w testach

        return WhisperModel(
            self._name,
            device="cpu",
            compute_type="int8",
            cpu_threads=self._threads,
            download_root=os.getenv("STT_MODELS_DIR") or None,
        )

    async def transcribe(self, audio: bytes) -> str:
        model = await self._get()
        async with self._run_lock:
            return await asyncio.to_thread(self._run, model, audio)

    @staticmethod
    def _run(model, audio: bytes) -> str:
        segments, _ = model.transcribe(
            io.BytesIO(audio),  # ogg/opus z Telegrama i Discorda dekoduje PyAV — bez systemowego ffmpeg
            language="pl",
            beam_size=5,
            vad_filter=True,
            initial_prompt=PROMPT,
        )
        return " ".join(s.text.strip() for s in segments).strip()
