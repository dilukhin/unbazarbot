from pathlib import Path
import os


class InstanceLock:
    """Блокировка процесса на время работы; файл не удаляется при освобождении."""
    def __init__(self,path):
        self.path=Path(path)
        self.stream=None

    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        stream=self.path.open('a+b')
        try:
            if os.name=='nt':
                import msvcrt
                if self.path.stat().st_size==0:
                    stream.write(b'0');stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError('Другой экземпляр бота уже работает с этой базой. Остановите его перед запуском.') from None
        self.stream=stream
        return self

    def __exit__(self,*args):
        if self.stream:
            if os.name=='nt':
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(),fcntl.LOCK_UN)
            self.stream.close()
            self.stream=None
