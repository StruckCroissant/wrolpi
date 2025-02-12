import asyncio
import re
import subprocess
from functools import wraps
from pathlib import Path
from typing import List
from uuid import uuid4

import psycopg2
from sqlalchemy.orm import Session

from modules.files.models import File
from wrolpi.cmd import which
from wrolpi.common import get_media_directory, wrol_mode_check, walk, chunks, logger
from wrolpi.dates import from_timestamp
from wrolpi.db import get_db_session, get_db_curs, get_ranked_models
from wrolpi.errors import InvalidFile
from wrolpi.vars import PYTEST

logger = logger.getChild(__name__)

__all__ = ['list_files', 'delete_file', 'split_file_name', 'upsert_file', 'refresh_files', 'search', 'get_mimetype']


def filter_parent_directories(directories: List[Path]) -> List[Path]:
    """
    Remove parent directories if their children are in the list.

    >>> filter_parent_directories([Path('foo'), Path('foo/bar'), Path('baz')])
    [Path('foo/bar'), Path('baz')]
    """
    unique_children = set()
    for directory in sorted(directories):
        for parent in directory.parents:
            # Remove any parent of this child.
            if parent in unique_children:
                unique_children.remove(parent)
        unique_children.add(directory)

    # Restore the original order.
    new_directories = [i for i in directories if i in unique_children]
    return new_directories


def list_files(directories: List[str]) -> List[Path]:
    """
    List all files down to the directories provided.  This includes all parent directories of the directories.
    """
    media_directory = get_media_directory()

    # Always display the media_directory files.
    paths = list(media_directory.iterdir())

    if directories:
        directories = [media_directory / i for i in directories if i]
        directories = filter_parent_directories(directories)
        for directory in directories:
            directory = media_directory / directory
            for parent in directory.parents:
                is_relative_to = str(media_directory).startswith(str(parent))
                if parent == Path('.') or is_relative_to:
                    continue
                paths.extend(parent.iterdir())
            paths.extend(directory.iterdir())

    return paths


@wrol_mode_check
def delete_file(file: str):
    """Delete a file in the media directory."""
    file = get_media_directory() / file
    if file.is_dir() or not file.is_file():
        raise InvalidFile(f'Invalid file {file}')
    file.unlink()


FILE_NAME_REGEX = re.compile(r'[_ ]')


def split_file_name(path: Path) -> List[str]:
    """
    Split a file name into words.

    >>> split_file_name(Path('foo.mp4'))
    ['foo']
    >>> split_file_name(Path('foo bar.mp4'))
    ['foo', 'bar']
    """
    words = [str(i) for i in FILE_NAME_REGEX.split(path.stem)]
    return words


FILE_BIN = which('file', '/usr/bin/file')


def get_mimetype(path: Path) -> str:
    """Get the mimetype using the builtin `file` command."""
    cmd = (FILE_BIN, '--mime-type', str(path.absolute()))
    output = subprocess.check_output(cmd)
    output = output.decode()
    mimetype = output.split(' ')[-1].strip()
    return mimetype


def upsert_file(path: Path, session: Session) -> File:
    """Update/insert a File in the DB.  Gather metadata about it."""
    file = session.query(File).filter_by(path=path).one_or_none()
    if not file:
        file = File(path=path)
        session.add(file)
    if not file.mimetype:
        file.mimetype = get_mimetype(path)
    if not file.title:
        file.title = split_file_name(path)
    if not file.size or not file.modification_datetime:
        stat = path.stat()
        file.size = stat.st_size
        file.modification_datetime = file.modification_datetime or from_timestamp(stat.st_mtime)

    return file


def _refresh_files():
    """Find and index all files"""
    logger.info('Refreshing Files')
    # Mark all files as unverified.  Any records that are unverified after refresh will be deleted.
    with get_db_curs(commit=True) as curs:
        curs.execute('UPDATE file SET idempotency=null')  # noqa

    paths = filter(lambda i: i.is_file(), walk(get_media_directory()))
    idempotency = str(uuid4())
    for chunk in chunks(paths, 20):
        with get_db_session(commit=True) as session:
            for path in chunk:
                file = upsert_file(path, session)
                file.idempotency = idempotency

    # Remove any records where the file no longer exists.
    with get_db_curs(commit=True) as curs:
        curs.execute('DELETE FROM file WHERE idempotency IS NULL')

    logger.info('Done refreshing Files')


@wraps(_refresh_files)
def refresh_files():
    """Schedule a refresh task if not testing.  If testing, do a synchronous refresh."""
    if PYTEST:
        return _refresh_files()

    async def _():
        return _refresh_files()

    asyncio.create_task(_())


def search(search_str: str, limit: int, offset: int):
    """
    Search the "title" of each file.  The title is the words in the file's stem.
    """
    with get_db_curs() as curs:
        stmt = '''
            SELECT id, ts_rank_cd(textsearch, websearch_to_tsquery(%(search_str)s)), COUNT(*) OVER() AS total
            FROM file
            WHERE textsearch @@ websearch_to_tsquery(%(search_str)s)
            OFFSET %(offset)s LIMIT %(limit)s
        '''
        params = dict(search_str=search_str, offset=offset, limit=limit)
        curs.execute(stmt, params)

        try:
            results = list(map(dict, curs.fetchall()))
        except psycopg2.ProgrammingError:
            # No files.
            return [], 0

        total = results[0]['total'] if results else 0
        ranked_ids = [i['id'] for i in results]

    with get_db_session() as session:
        results = get_ranked_models(ranked_ids, File, session)
        results = [i.__json__() for i in results]

    return results, total
