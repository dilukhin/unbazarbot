from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import mimetypes
import tempfile

from aiogram import Bot
from aiogram.types import Message


@dataclass
class MediaRef:
    file_id: str
    file_unique_id: str
    kind: str
    duration: int | None
    file_size: int | None
    file_name: str | None
    mime_type: str | None


def extract_media(message: Message, *, extended=False) -> MediaRef | None:
    if message.voice:
        return MediaRef(
            file_id=message.voice.file_id,
            file_unique_id=message.voice.file_unique_id,
            kind="voice",
            duration=message.voice.duration,
            file_size=message.voice.file_size,
            file_name=None,
            mime_type=message.voice.mime_type,
        )
    if extended:
        item=message.audio or message.document
        if item:
            return MediaRef(item.file_id,item.file_unique_id,'audio' if message.audio else 'document',
                            getattr(item,'duration',None),item.file_size,getattr(item,'file_name',None),item.mime_type)
    return None

def guess_audio_format(media: MediaRef, telegram_file_path: str | None, forced: str | None = None) -> str:
    if forced:
        return forced.strip(".").lower()

    candidates = [media.file_name, telegram_file_path]
    for candidate in candidates:
        if not candidate:
            continue
        suffix = Path(candidate).suffix.lower().strip(".")
        if suffix == "oga":
            return "ogg"
        if suffix in {"ogg", "opus", "mp3", "wav", "m4a", "mp4", "webm", "flac", "aac"}:
            return suffix

    if media.mime_type:
        ext = mimetypes.guess_extension(media.mime_type)
        if ext:
            ext = ext.strip(".").lower()
            if ext == "oga":
                return "ogg"
            return ext

    if media.kind == "voice":
        return "ogg"
    return "mp3"


async def download_media_to_temp(bot: Bot, media: MediaRef, suffix: str | None = None, max_bytes=20*1024*1024) -> tuple[Path, str | None]:
    tg_file = await bot.get_file(media.file_id)
    file_path = tg_file.file_path
    final_suffix = suffix or (Path(file_path or "audio.ogg").suffix or ".ogg")
    tmp = tempfile.NamedTemporaryFile(prefix="unbazarbot_", suffix=final_suffix, delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        class LimitedWriter:
            def __init__(self,stream):self.stream,self.size=stream,0
            def write(self,data):
                self.size+=len(data)
                if self.size>max_bytes:raise ValueError('Downloaded audio exceeds size limit')
                return self.stream.write(data)
            def seek(self,*args):return self.stream.seek(*args)
            def flush(self):return self.stream.flush()
        with tmp_path.open('wb') as stream:
            await bot.download_file(file_path,destination=LimitedWriter(stream))
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path, file_path

