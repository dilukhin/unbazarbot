"""Validate the catalog and its sidecars before any runtime file is opened."""
from pathlib import Path


def validate_tariff_path(cache, *, database, config, lock=None, env_file=None, application_roots=()):
    cache = Path(cache)
    if not cache.is_absolute():
        raise ValueError('TARIFF_CACHE_PATH должен быть абсолютным')
    database = Path(database).resolve()
    protected = [database, Path(str(database) + '-wal'), Path(str(database) + '-shm'),
                 Path(config).resolve(), Path(lock or (str(database) + '.lock')).resolve()]
    if env_file:
        protected.append(Path(env_file).resolve())
    roots = [Path(root).resolve() for root in application_roots]
    for path in (cache, Path(str(cache) + '.lock'), Path(str(cache) + '.paused')):
        resolved = path.resolve()
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('Тарифный кеш и его маркеры должны быть обычными файлами')
        if any(resolved.is_relative_to(root) for root in roots):
            raise ValueError('Тарифный кеш должен находиться вне Git и выпусков')
        for target in protected:
            if resolved == target or (path.exists() and target.exists() and path.samefile(target)):
                raise ValueError('Тарифный кеш пересекается с рабочими файлами')
    return cache.resolve()
