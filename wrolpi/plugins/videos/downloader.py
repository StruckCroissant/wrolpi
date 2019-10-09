#! /usr/bin/env python3
import glob
import hashlib
import json
import logging
import pathlib
import re
from datetime import datetime
from typing import Tuple

from dictorm import DictDB, Dict, Or, Table
from youtube_dl import YoutubeDL

from wrolpi.common import get_db_context
from wrolpi.plugins.videos.common import get_downloader_config, resolve_project_path

logger = logging.getLogger('wrolpi:downloader')
ch = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

ydl_logger = logging.getLogger('wrolpi:ydl')


def get_channel_info(channel: Dict) -> dict:
    """
    Get the YoutubeDL info_extractor information dictionary.  This is built using many http requests.

    :param channel: dictorm Channel table
    :return:
    """
    ydl = YoutubeDL()
    # ydl.params['logger'] = ydl_logger
    ydl.add_default_info_extractors()

    logger.info(f'Downloading video list for {channel["url"]}  This may take several minutes.')
    info = ydl.extract_info(channel['url'], download=False, process=False)
    if 'url' in info:
        url = info['url']
        info = ydl.extract_info(url, download=False, process=False)

    # Resolve all entries to dictionaries
    info['entries'] = list(info['entries'])
    return info


def update_channels(db_conn, db, oldest_date=None):
    """Get all information for each channel.  (No downloads performed)"""
    Channel = db['channel']
    oldest_date = oldest_date or datetime.now().date()

    # Update all outdated channel info json columns
    remote_channels = Channel.get_where(
        Channel['url'].IsNotNull(),
        Channel['url'] != '',
        Or(
            Channel['info_date'] < oldest_date,
            Channel['info_date'].IsNull()
        ),
    )
    remote_channels = list(remote_channels)
    logger.debug(f'Getting info for {len(remote_channels)} channels')
    for channel in remote_channels:
        info = get_channel_info(channel)
        channel['info_json'] = info
        channel['info_date'] = datetime.now()
        channel.flush()
        db_conn.commit()


VIDEO_EXTENSIONS = ['mp4', 'webm', 'flv']


def find_matching_video_files(directory, search_str) -> str:
    """Create a generator which returns any video files containing the search string."""
    for ext in VIDEO_EXTENSIONS:
        yield from glob.glob(f'{directory}/*{search_str}*{ext}')


def find_missing_channel_videos(channel: Dict) -> dict:
    info_json = json.loads(channel['info_json'])
    entries = info_json['entries']
    for entry in entries:
        source_id = entry['id']
        matching_video_files = find_matching_video_files(channel['directory'], source_id)
        try:
            next(matching_video_files)
            # Some video file was found, move onto the next
            continue
        except StopIteration:
            pass
        if re.match(channel['match_regex'], entry['title']):
            yield entry
        else:
            logger.debug(f'Skipping {entry}, title does not match regex.')


def get_channel_video_count(channel: Dict) -> int:
    """Count all video files in a channel's directory."""
    return len(list(find_matching_video_files(channel['directory'], '')))


def find_all_missing_videos(db: DictDB, max_downloads_per_channel: int = None) -> Tuple[Dict, dict]:
    """Search recursively for each video identified in the channel's JSON package.  If a video's file can't
    be found, yield it.

    If max_downloads_per_channel is provided, this will only yield missing videos when there are less video
    files than this number."""
    Channel = db['channel']
    channels = Channel.get_where(Channel['info_json'].IsNotNull())
    for channel in channels:
        if max_downloads_per_channel:
            # Don't download more videos than the max limit
            video_count = get_channel_video_count(channel)
            if video_count > max_downloads_per_channel:
                continue

        video_count = 0
        for missing_video in find_missing_channel_videos(channel):
            if max_downloads_per_channel and video_count >= max_downloads_per_channel:
                logger.debug(f'Video count in {channel["directory"]} exceeds max downloads per channel.')
                break
            yield channel, missing_video
            video_count += 1


def download_video(channel: dict, video: dict) -> pathlib.Path:
    """
    Download a video (and associated thumbnail/etc) to it's channel's directory.

    :param channel: A DictORM Channel entry
    :param video: A YoutubeDL info entry dictionary
    :return:
    """
    # YoutubeDL expects specific options, add onto the default options
    config = get_downloader_config()
    options = dict(config)
    directory = resolve_project_path(channel['directory'], mkdir=True)
    options['outtmpl'] = f'{directory}/{config["file_name_format"]}'

    ydl = YoutubeDL(options)
    ydl.add_default_info_extractors()
    source_id = video['id']
    url = f'https://www.youtube.com/watch?v={source_id}'
    entry = ydl.extract_info(url, download=True, process=True)
    final_filename = ydl.prepare_filename(entry)
    final_filename = pathlib.Path(final_filename)
    return final_filename


def replace_extension(path: pathlib.Path, new_ext) -> pathlib.Path:
    """Swap the extension of a file's path.

    Example:
        >>> foo = pathlib.Path('foo.bar')
        >>> replace_extension(foo, 'baz')
        'foo.baz'
    """
    parent = path.parent
    existing_ext = path.suffix
    path = str(path)
    name, _, _ = path.rpartition(existing_ext)
    path = pathlib.Path(str(parent / name) + new_ext)
    return path


def find_meta_files(path: pathlib.Path) -> Tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    """Find all files that share a file's full path, except their extensions.  It is assumed that file with the
    same name, but different extension is related to that file.

    Example:
        >>> foo = pathlib.Path('foo.bar')
        >>> find_meta_files(foo)
        ('foo.jpg', 'foo.en.vtt')

    """
    suffix = path.suffix
    name, suffix, _ = str(path.name).rpartition(suffix)
    meta_file_exts = ('.jpg', '.description', '.en.vtt', '.info.json')
    for meta_ext in meta_file_exts:
        meta_path = replace_extension(path, meta_ext)
        meta_path = meta_path if meta_path.exists() else None
        yield meta_path


NAME_PARSER = re.compile(r'(.*?)_((?:\d+?)|(?:NA))_(?:(.{11})_)?(.*)\.'
                         r'(jpg|flv|mp4|part|info\.json|description|webm|..\.srt|..\.vtt)')


def insert_video(db: DictDB, video_path: pathlib.Path, channel: Table, idempotency: str = None):
    """Find an insert a video file's information into the DB."""
    Video = db['video']
    poster_path, description_path, caption_path, info_json_path = find_meta_files(video_path)
    name_match = NAME_PARSER.match(video_path.name)
    _ = upload_date = source_id = title = ext = None
    if name_match:
        _, upload_date, source_id, title, ext = name_match.groups()

    # Hash the video's path for easy and collision-free linking
    video_path_hash_long = hashlib.sha3_512(str(video_path).encode('UTF-8')).hexdigest()
    video_path_hash = video_path_hash_long[:10]

    # Increase video hash length until it no longer conflicts
    conflicting_hash = Video.get_one(video_path_hash=video_path_hash)
    while conflicting_hash:
        try:
            video_path_hash = video_path_hash_long[:len(video_path_hash) + 1]
        except IndexError:
            raise Exception("Video path hash conflicts, aren't you unlucky!")
        conflicting_hash = Video.get_one(video_path_hash=video_path_hash)

    video = Video(
        channel_id=channel['id'],
        description_path=str(description_path) if description_path else None,
        ext=ext,
        poster_path=str(poster_path) if poster_path else None,
        source_id=source_id,
        title=title,
        upload_date=upload_date,
        video_path=str(video_path),
        name=video_path.name,
        caption_path=str(caption_path) if caption_path else None,
        idempotency=idempotency,
        info_json_path=str(info_json_path) if info_json_path else None,
        video_path_hash=video_path_hash,
    )
    video.flush()
    return video


def download_all_missing_videos(db_conn, db, count_limit=0):
    """Find any videos identified by the info packet that haven't yet been downloaded, download them."""
    wrolpi_config = get_downloader_config()
    max_downloads_per_channel = int(wrolpi_config.get('max_downloads_per_channel', count_limit))
    missing_videos = find_all_missing_videos(db, max_downloads_per_channel=max_downloads_per_channel)
    for channel, missing_video in missing_videos:
        try:
            video_path = download_video(channel, missing_video)
        except Exception as e:
            logger.warning(f'Failed to download {missing_video} with exception: {e}')
            continue
        insert_video(db, video_path, channel, None)
        db_conn.commit()
    else:
        db_conn.rollback()
        print('No videos need to be downloaded today!')


def main(args=None):
    """Find and download any missing videos.  Parse any arguments passed by the cmd-line."""
    count_limit = 0
    if args.count_limit:
        count_limit = args.count_limit
    with get_db_context(commit=True) as (db_conn, db):
        update_channels(db_conn, db)
        download_all_missing_videos(db_conn, db, count_limit)
    return 0
