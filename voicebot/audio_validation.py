"""Bounded local audio inspection/normalization. No network or shell commands."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import tempfile
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from pathlib import Path


class AudioError(ValueError):
    def __init__(self,message,code='invalid-audio'):
        super().__init__(message)
        self.code=code


@dataclass
class PreparedAudio:
    path: Path
    duration: Decimal
    audio_format: str
    owner: object = None

    @property
    def seconds(self):
        return int(self.duration.to_integral_value(rounding=ROUND_CEILING))

    def close(self):
        if self.owner is not None:
            self.owner.cleanup()


def magic_format(path):
    with path.open('rb') as source:
        head=source.read(64)
    if head.startswith(b'RIFF') and head[8:12]==b'WAVE':return 'wav'
    if head.startswith(b'OggS'):return 'ogg'
    if head.startswith(b'fLaC'):return 'flac'
    if head.startswith(b'ID3'):return 'mp3'
    if len(head)>=2 and head[0]==255 and head[1]&224==224:
        return 'aac' if head[1]&6==0 else 'mp3'
    if head[4:8]==b'ftyp':return 'mov'
    if head.startswith(b'\x1aE\xdf\xa3'):return 'matroska'
    raise AudioError('Не удалось подтвердить поддерживаемый аудиоформат файла.')


async def _run(binary, args, max_bytes):
    limiter=shutil.which('prlimit')
    executable=shutil.which(binary)
    if os.name!='posix' or not limiter or not executable:
        raise AudioError('Безопасная проверка аудио сейчас недоступна.')
    proc=await asyncio.create_subprocess_exec(limiter,'--as=536870912','--cpu=20',
                    '--fsize='+str(max_bytes),'--nofile=64','--nproc=256','--',executable,*args,
                    stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,start_new_session=True,
                    env={'PATH':os.defpath,'LANG':'C','LC_ALL':'C','OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1'})
    async def bounded(stream, cap):
        result=bytearray()
        while chunk:=await stream.read(8192):
            result.extend(chunk)
            if len(result)>cap:raise AudioError('Проверка аудио превысила допустимый объём вывода.')
        return bytes(result)
    readers=[asyncio.create_task(bounded(proc.stdout,1024*1024)),asyncio.create_task(bounded(proc.stderr,65536))]
    async def collect():
        output,errors=await asyncio.gather(*readers)
        return output,errors,await proc.wait()
    try:
        output,errors,code=await asyncio.wait_for(collect(),timeout=45)
        if code!=0:
            reason='codec-or-resource-limit'
            for needles,label in (((b'pthread_create',b'Resource temporarily unavailable',b'Error creating thread'),'thread-limit'),
                                  ((b'Cannot allocate memory',b'failed to map',b'Out of memory'),'memory-limit'),
                                  ((b'Unrecognized option',b'Option not found'),'unsupported-option')):
                if any(token in errors for token in needles):reason=label;break
            raise AudioError('Аудиофайл повреждён, не поддерживается или превышает предел проверки.',code=reason)
        return output
    except BaseException:
        with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGKILL)
        await proc.wait()
        raise
    finally:
        for task in readers:
            if not task.done():task.cancel()
        await asyncio.gather(*readers,return_exceptions=True)


async def probe(path, demuxer, max_bytes):
    extra=['-enable_drefs','0','-use_absolute_path','0'] if demuxer=='mov' else []
    raw=await _run('ffprobe',['-v','error','-max_alloc','67108864','-protocol_whitelist','file,pipe',
               '-f',demuxer,*extra,'-show_entries','format=duration:stream=codec_type,codec_name,sample_rate,channels',
               '-of','json',str(path)],max_bytes)
    try:
        value=json.loads(raw,parse_float=Decimal)
        streams=value['streams']
        if len(streams)!=1 or streams[0]['codec_type']!='audio' or streams[0].get('codec_name') in {None,'unknown'}:
            raise ValueError()
        if not 1<=int(streams[0]['sample_rate'])<=192000 or not 1<=int(streams[0]['channels'])<=8:
            raise ValueError()
        duration=Decimal(value['format']['duration'])
        if not duration.is_finite() or not 0<duration<=86400:
            raise ValueError()
    except (ValueError,KeyError,TypeError,ArithmeticError):
        raise AudioError('Требуется один подтверждённый аудиопоток с известной длительностью, без видео.') from None
    return duration


async def prepare_audio(path, *, max_seconds, max_bytes, audio_format='ogg'):
    path=Path(path)
    max_bytes=min(max_bytes,20*1024*1024)
    if not path.is_file() or path.is_symlink() or not 0<path.stat().st_size<=max_bytes:
        raise AudioError('Аудиофайл превышает допустимый размер или пуст.')
    codecs={'ogg':('libopus',['-b:a','24k']),'mp3':('libmp3lame',['-b:a','64k']),
            'wav':('pcm_s16le',[]),'flac':('flac',[])}
    if audio_format not in codecs:
        raise AudioError('Для выбранной модели безопасная подготовка этого формата недоступна.')
    demuxer=magic_format(path)
    duration=await probe(path,demuxer,max_bytes)
    if duration>Decimal(max_seconds)+Decimal('0.1'):
        raise AudioError('Фактическая длительность аудио превышает разрешённый предел.')
    owner=tempfile.TemporaryDirectory(prefix='unbazar_audio_')
    output=Path(owner.name)/('prepared.'+audio_format)
    codec,options=codecs[audio_format]
    extra=['-enable_drefs','0','-use_absolute_path','0'] if demuxer=='mov' else []
    try:
        await _run('ffmpeg',['-v','error','-nostdin','-max_alloc','67108864','-protocol_whitelist','file,pipe',
                    '-threads','1','-filter_threads','1','-filter_complex_threads','1','-f',demuxer,*extra,'-i',str(path),
                    '-map','0:a:0','-vn','-sn','-dn','-map_metadata','-1','-t',str(max_seconds+1),
                    '-ar','16000','-ac','1','-c:a',codec,*options,'-threads','1','-y',str(output)],max_bytes)
        if not output.is_file() or not 0<output.stat().st_size<=max_bytes:
            raise AudioError('Подготовленный аудиофайл превышает допустимый размер.')
        output.chmod(0o600)
        actual=await probe(output,audio_format,max_bytes)
        if actual>Decimal(max_seconds)+Decimal('0.1'):
            raise AudioError('Проверка декодированного аудио обнаружила превышение длительности.')
        return PreparedAudio(output,actual,audio_format,owner)
    except BaseException:
        owner.cleanup()
        raise
