"""Rotate console log files; prune only families explicitly marked closed."""
import logging
from pathlib import Path
import re
import time

log = logging.getLogger('ocr-retention')


def prune_closed_logs(directory, prefix, keep_runs=20, days=7):
    directory = Path(directory).resolve()
    pattern = re.compile(re.escape(prefix) + r'_\d{8}_\d{6}_\d{6}_(?:api|coordinator)_\d+\.log\.closed')
    markers = []
    for path in directory.glob(prefix + '_*.log.closed'):
        try:
            if pattern.fullmatch(path.name) and not path.is_symlink() and path.is_file():
                markers.append((path, path.stat().st_mtime))
        except OSError:
            continue
    markers.sort(key=lambda entry: entry[1], reverse=True)
    cutoff = time.time() - days * 86400
    removed = 0
    for index, (marker, modified) in enumerate(markers):
        if index < keep_runs and modified >= cutoff:
            continue
        base = marker.with_suffix('')
        family = [base, marker] + [p for p in directory.glob(base.name + '.*')
                                  if p.name[len(base.name) + 1:].isdigit()]
        if any(p.is_symlink() or p.resolve().parent != directory for p in family):
            continue
        try:
            for path in family:
                path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            log.warning('Could not prune a closed service log family')
    return removed


class ConsoleLogFile:
    """File-like text sink, shared by stdout/stderr under ConsoleTee's lock."""
    def __init__(self, path, max_bytes=0, backups=3, keep_runs=20, days=7, prefix='oracle'):
        if max_bytes < 0 or backups < 1 or keep_runs < 1 or days < 1:
            raise ValueError('Invalid log rotation/retention limits')
        self.path, self.max_bytes, self.backups = Path(path), max_bytes, backups
        self.keep_runs, self.days, self.prefix = keep_runs, days, prefix
        if max_bytes:
            prune_closed_logs(self.path.parent, prefix, keep_runs, days)
        self.stream = self.path.open('a', encoding='utf-8', buffering=1, newline='')
        self.size = self.path.stat().st_size

    @property
    def closed(self):
        return self.stream.closed

    def write(self, text):
        size = len(text.encode('utf-8'))
        if self.max_bytes and self.size and self.size + size > self.max_bytes:
            self.stream.close()
            self.path.with_name(self.path.name + f'.{self.backups}').unlink(missing_ok=True)
            for number in range(self.backups - 1, 0, -1):
                source = self.path.with_name(self.path.name + f'.{number}')
                if source.exists():
                    source.replace(self.path.with_name(self.path.name + f'.{number + 1}'))
            self.path.replace(self.path.with_name(self.path.name + '.1'))
            self.stream = self.path.open('a', encoding='utf-8', buffering=1, newline='')
            self.size = 0
        result = self.stream.write(text)
        self.size += size
        return result

    def flush(self):
        self.stream.flush()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()
        if self.max_bytes:
            try:
                self.path.with_name(self.path.name + '.closed').write_text('closed\n', encoding='ascii')
                prune_closed_logs(self.path.parent, self.prefix, self.keep_runs, self.days)
            except OSError:
                log.warning('Could not mark/prune a closed service log')


def add_log_arguments(parser):
    parser.add_argument('--log-max-mb', type=int, default=10)
    parser.add_argument('--log-backups', type=int, default=3)
    parser.add_argument('--log-retain-runs', type=int, default=20)
    parser.add_argument('--log-retain-days', type=int, default=7)


def log_arguments(args):
    if (not 1 <= args.log_max_mb <= 1024 or not 1 <= args.log_backups <= 20 or
            not 1 <= args.log_retain_runs <= 1000 or not 1 <= args.log_retain_days <= 3650):
        raise ValueError('Invalid log retention limits: max MB 1..1024, backups 1..20, runs 1..1000, days 1..3650')
    return dict(max_bytes=args.log_max_mb * 1024 * 1024, backups=args.log_backups,
                keep_runs=args.log_retain_runs, days=args.log_retain_days)
