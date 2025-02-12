#! /usr/bin/env python3
import json
import pathlib
import re
import traceback
from abc import ABC
from typing import Tuple, List, Optional

import yt_dlp.utils
from sqlalchemy.orm import Session
from yt_dlp import YoutubeDL
from yt_dlp.extractor import YoutubeTabIE  # noqa
from yt_dlp.utils import UnsupportedError, DownloadError

from wrolpi.cmd import which
from wrolpi.common import logger, extract_domain, get_media_directory
from wrolpi.dates import now
from wrolpi.db import get_db_session, get_db_curs
from wrolpi.db import optional_session
from wrolpi.downloader import Downloader, Download, DownloadResult
from wrolpi.errors import UnknownChannel, ChannelURLEmpty, UnrecoverableDownloadError
from wrolpi.vars import PYTEST
from .channel.lib import create_channel, get_channel
from .common import apply_info_json, get_no_channel_directory, get_videos_directory
from .lib import upsert_video, refresh_channel_videos, get_downloader_config
from .models import Video, Channel
from .schema import ChannelPostRequest
from .video_url_resolver import video_url_resolver

logger = logger.getChild(__name__)
ydl_logger = logger.getChild('youtube-dl')

YDL = YoutubeDL()
YDL.params['logger'] = ydl_logger
YDL.add_default_info_extractors()

# Channels are handled differently than a single video.
ChannelIEs = {
    YoutubeTabIE,
}

PREFERRED_VIDEO_EXTENSION = 'mp4'
PREFERRED_VIDEO_FORMAT = ','.join([
    'res:720',  # Use the best 720p available first
    '136+140',  # 130=720p video-only, 140= high quality audio only
    '22',  # 720p video with audio
    '18',  # 360p video with audio
    'bestvideo+bestaudio',  # Download the highest resolution as a last resort (1080p is quite large).
])


def extract_info(url: str, ydl: YoutubeDL = YDL, process=False) -> dict:
    """Get info about a video.  Separated for testing."""
    return ydl.extract_info(url, download=False, process=process)


def prepare_filename(entry: dict, ydl: YoutubeDL = YDL) -> str:
    """Get filename from YoutubeDL.  Separated for testing."""
    return ydl.prepare_filename(entry)


class ChannelDownloader(Downloader, ABC):
    """Handles downloading of videos in a Channel or Playlist."""
    name = 'video_channel'
    pretty_name = 'Video Channel'
    listable = False

    def __repr__(self):
        return f'<ChannelDownloader>'

    @classmethod
    def valid_url(cls, url) -> Tuple[bool, None]:
        for ie in ChannelIEs:
            if ie.suitable(url):
                return True, None
        logger.debug(f'{cls.__name__} not suitable for {url}')
        return False, None

    @staticmethod
    def is_a_playlist(info: dict):
        # A playlist will have an id different from its channel.
        return info['id'] != info['channel_id']

    async def do_download(self, download: Download) -> DownloadResult:
        """Update a Channel's catalog, then schedule downloads of every missing video."""
        info = extract_info(download.url, process=False)
        # Resolve the entries generator.
        info['entries'] = list(info['entries'])
        download.info_json = info
        if session := Session.object_session(download):
            # May not have a session during testing.
            session.commit()

        name = info['uploader']
        channel_source_id = info['channel_id']
        channel = get_or_create_channel(channel_source_id, download.url, name)
        channel.dict()  # get all attributes while we have the session.

        location = f'/videos/channel/{channel.id}/video' if channel and channel.id else None

        is_a_playlist = self.is_a_playlist(info)
        try:
            if is_a_playlist:
                downloads = self.get_playlist_downloads(download)
            else:
                downloads = self.get_channel_downloads(download, info, channel)
            return DownloadResult(success=True, location=location, downloads=downloads)
        except Exception:
            name = 'playlist' if is_a_playlist else 'channel'
            logger.warning(f'Failed to update catalog of {name} {download.url}')
            return DownloadResult(success=False, location=location, error=str(traceback.format_exc()))

    @staticmethod
    def get_channel_downloads(download: Download, info: dict, channel: Channel) -> List[str]:
        """Get a list of all videos in a Channel, schedule downloads for any missing videos."""
        update_channel_catalog(channel, info)

        domain = extract_domain(download.url)
        missing_videos = find_all_missing_videos(channel.id)
        downloads = []
        for _, _, missing_video in missing_videos:
            url = missing_video.get('webpage_url') or video_url_resolver(domain, missing_video)
            downloads.append(url)
        return downloads

    @staticmethod
    def get_playlist_downloads(download: Download) -> List[str]:
        """Get a list of all videos in a playlist, schedule downloads for any missing videos."""
        # Get all videos in the playlist
        downloads = [i['url'] for i in download.info_json['entries']]
        # Only download those that have not yet been downloaded.
        downloads = [i for i in downloads if not video_downloader.already_downloaded(i)]
        return downloads


YT_DLP_BIN = which(
    'yt-dlp',
    '/usr/local/bin/yt-dlp',  # Location in docker container
    '/opt/wrolpi/venv/bin/yt-dlp',  # Use virtual environment location
    warn=True,
)


class VideoDownloader(Downloader, ABC):
    """Downloads a single video.

    Store the video in its channel's directory, otherwise store it in `videos/NO CHANNEL`.
    """
    name = 'video'
    pretty_name = 'Videos'

    def __repr__(self):
        return f'<VideoDownloader>'

    @optional_session
    def already_downloaded(self, url: str, session: Session = None) -> bool:
        # We only consider a video record with a video file as "downloaded".
        return bool(session.query(Video).filter(Video.url == url, Video.video_path != None, ).count())  # noqa

    @classmethod
    def valid_url(cls, url) -> Tuple[bool, Optional[dict]]:
        """Match against all Youtube-DL Info Extractors, except those that match a Channel."""
        for ie in YDL._ies.values():
            if ie.suitable(url) and not ChannelDownloader.valid_url(url)[0]:
                try:
                    info = extract_info(url)
                    return True, info
                except UnsupportedError:
                    logger.debug(f'Video downloader extract_info failed for {url}')
                    return False, None
                except DownloadError:
                    logger.debug(f'Video downloader extract_info failed for {url}')
                    return False, None
        logger.debug(f'{cls.__name__} not suitable for {url}')
        return False, None

    async def do_download(self, download: Download) -> DownloadResult:
        if download.attempts >= 10:
            raise UnrecoverableDownloadError('Max download attempts reached')

        url = download.url
        info = download.info_json

        if not info:
            # Info was not fetched by the DownloadManager, lets get it.
            valid, info = self.valid_url(url)
            if not valid:
                raise UnrecoverableDownloadError(f'{self} cannot download {url}')
            session = Session.object_session(download)
            download.info_json = info
            session.commit()

        channel_name = info.get('channel')
        channel_id = info.get('channel_id')
        channel = None
        if channel_name or channel_id:
            channel_url = info.get('channel_url')
            channel = get_or_create_channel(source_id=channel_id, url=channel_url, name=channel_name)

        # Use the default directory if this video has no channel.
        out_dir = get_no_channel_directory()
        if channel:
            out_dir = channel.directory.path
        out_dir.mkdir(exist_ok=True, parents=True)

        logs = None  # noqa
        try:
            video_path, entry = self.prepare_filename(url, out_dir)
            # Do the real download.
            file_name_format = '%(uploader)s_%(upload_date)s_%(id)s_%(title)s.%(ext)s'
            cmd = (
                str(YT_DLP_BIN),
                '-cw',  # Continue downloads, do not clobber existing files.
                '-f', PREFERRED_VIDEO_FORMAT,
                '--match-filter', '!is_live',  # Do not attempt to download Live videos.
                '--write-subs',
                '--write-auto-subs',
                '--write-thumbnail',
                '--write-info-json',
                '--merge-output-format', PREFERRED_VIDEO_EXTENSION,
                '-o', file_name_format,
                '--no-cache-dir',
                '--compat-options', 'no-live-chat',
                url,
            )
            return_code, logs = await self.process_runner(url, cmd, out_dir)

            stdout = logs['stdout'].decode() if hasattr(logs['stdout'], 'decode') else logs['stdout']
            stderr = logs['stderr'].decode() if hasattr(logs['stderr'], 'decode') else logs['stderr']

            if return_code != 0:
                error = f'{stdout}\n\n\n{stderr}\n\nvideo downloader process exited with {return_code}'
                return DownloadResult(
                    success=False,
                    error=error,
                )

            if not video_path.is_file():
                error = f'{stdout}\n\n\n{stderr}\n\n' \
                        f'Video file could not be found!  {video_path}'
                return DownloadResult(
                    success=False,
                    error=error,
                )

            with get_db_session(commit=True) as session:
                # If the video is from a channel, it will already be in the database.
                source_id = entry['id']
                existing_video = session.query(Video).filter_by(source_id=source_id).one_or_none()
                video = upsert_video(session, video_path, channel, id_=existing_video.id if existing_video else None)
                video_id = video.id
        except UnrecoverableDownloadError:
            raise
        except yt_dlp.utils.UnsupportedError as e:
            raise UnrecoverableDownloadError('URL is not supported by yt-dlp') from e
        except Exception as e:
            logger.warning(f'VideoDownloader failed to download: {download.url}', exc_info=e)
            if _skip_download(e):
                # The video failed to download, and the error will never be fixed.  Skip it forever.
                try:
                    source_id = info.get('id')
                    logger.warning(f'Adding video "{source_id}" to skip list for this channel.  WROLPi will not '
                                   f'attempt to download it again.')

                    with get_db_session(commit=True) as session:
                        c = session.query(Channel).filter_by(id=channel.id).one()
                        c.add_video_to_skip_list(source_id)
                except Exception:
                    # Could not skip this video, it may not have a channel.
                    logger.warning(f'Could not skip video {url}')

                # Skipped downloads should not be tried again.
                raise UnrecoverableDownloadError() from e
            # Download did not succeed, try again later.
            if logs and (stderr := logs.get('stderr')):
                error = f'{stderr}\n\n{traceback.format_exc()}'
            else:
                error = str(traceback.format_exc())
            return DownloadResult(success=False, error=error)

        if channel:
            location = f'/videos/channel/{channel.id}/video/{video_id}'
        else:
            location = f'/videos/video/{video_id}'
        result = DownloadResult(
            success=True,
            location=location,
        )
        return result

    @staticmethod
    def prepare_filename(url: str, out_dir: pathlib.Path) -> Tuple[pathlib.Path, dict]:
        """Get the full path of a video file from its URL."""
        if not out_dir.is_dir():
            raise ValueError(f'Output directory does not exist! {out_dir=}')

        # YoutubeDL expects specific options, add onto the default options
        options = get_downloader_config().dict()
        options['outtmpl'] = f'{out_dir}/{options["file_name_format"]}'
        options['merge_output_format'] = PREFERRED_VIDEO_EXTENSION

        logger.debug(f'Downloading {url} to {out_dir}')

        # Create a new YoutubeDL for the output directory.
        ydl = YoutubeDL(options)
        ydl.params['logger'] = ydl_logger
        ydl.add_default_info_extractors()

        # Get the path where the video will be saved.
        entry = extract_info(url, ydl=ydl, process=True)
        final_filename = pathlib.Path(prepare_filename(entry, ydl=ydl)).absolute()
        if final_filename.suffix.lower() != f'.{PREFERRED_VIDEO_EXTENSION}':
            raise DownloadError(f'Cannot download video {url} because yt-dlp filename is invalid.')
        return final_filename, entry


channel_downloader = ChannelDownloader()
# Videos may match the ChannelDownloader, give it a higher priority.
video_downloader = VideoDownloader(40)


def get_or_create_channel(source_id: str = None, url: str = None, name: str = None) -> Channel:
    """
    Attempt to find a Channel using the provided params.  The params are in order of reliability.

    Creates a new Channel if one cannot be found.
    """
    try:
        channel = get_channel(source_id=source_id, url=url, name=name, return_dict=False)
        return channel
    except UnknownChannel:
        pass

    if not name:
        raise ValueError(f'Cannot create channel without a name')

    # Channel does not exist.  Create one in the video directory.
    channel_directory = get_videos_directory() / name
    if not channel_directory.is_dir():
        channel_directory.mkdir(parents=True)
    data = ChannelPostRequest(
        source_id=source_id,
        name=name,
        url=url,
        directory=str(channel_directory.relative_to(get_media_directory())),
    )
    channel = create_channel(data=data, return_dict=False)
    # Create the directory now that the channel is approved.
    channel_directory.mkdir(exist_ok=True)

    return channel


def update_channel_catalog(channel: Channel, info: dict):
    """
    Connect to the Channel's host website and pull a catalog of all videos.  Insert any new videos into the DB.

    It is expected that any missing videos will be downloaded later.
    """
    logger.info(f'Downloading video list for {channel.name} at {channel.url}  This may take several minutes.')

    # Resolve all entries to dictionaries.
    entries = info['entries'] = list(info['entries'])

    # yt-dlp may hand back a list of URLs, lets use the "Uploads" URL, if available.
    try:
        entries[0]['id']
    except Exception:
        logger.warning('yt-dlp did not return a list of URLs')
        for entry in entries:
            if entry['title'] == 'Uploads':
                logger.info('Youtube-DL gave back a list of URLs, found the "Uploads" URL and using it.')
                info = extract_info(entry['url'])
                entries = info['entries'] = list(info['entries'])
                break

    # This is all the source id's that are currently available.
    try:
        all_source_ids = {i['id'] for i in entries}
    except KeyError as e:
        logger.warning(f'No ids for entries!  Was the channel update successful?  Is the channel URL correct?')
        logger.warning(f'entries: {entries}')
        raise KeyError('No id key for entry!') from e

    # In order to store the Video's URL, we will need a quick lookup.
    urls = {i['id']: i.get('webpage_url') for i in entries}

    with get_db_session(commit=True) as session:
        # Get the channel in this new context.
        channel = session.query(Channel).filter_by(id=channel.id).one()

        channel.info_json = info
        channel.info_date = now()
        channel.source_id = info.get('id')

        with get_db_curs() as curs:
            # Get all known videos in this channel.
            query = 'SELECT source_id FROM video WHERE channel_id=%s AND source_id IS NOT NULL'
            curs.execute(query, (channel.id,))
            known_source_ids = {i[0] for i in curs.fetchall()}

        new_source_ids = all_source_ids.difference(known_source_ids)

        logger.info(f'Got {len(new_source_ids)} new videos for channel {channel.name}')
        channel_id = channel.id
        for source_id in new_source_ids:
            url = urls.get(source_id)
            session.add(Video(source_id=source_id, channel_id=channel_id, url=url))

    # Write the Channel's info to a JSON file.
    if channel.directory:
        info_json_path = channel.directory.path / f'{channel.name}.info.json'
        with info_json_path.open('wt') as fh:
            json.dump(info, fh, indent=2)

    # Update all view counts using the latest from the Channel's info_json.
    apply_info_json(channel_id)


def _find_all_missing_videos(channel_id: id) -> List[Tuple]:
    """Get all Video entries which don't have the required media files (i.e. hasn't been downloaded)."""
    with get_db_curs() as curs:
        query = f'''
            SELECT
                video.id, video.source_id, video.channel_id
            FROM
                video
                LEFT JOIN channel ON channel.id = video.channel_id
            WHERE
                channel.url IS NOT NULL
                AND channel.url != ''
                AND video.source_id IS NOT NULL
                AND channel_id = %s
                AND video.channel_id IS NOT NULL
                AND (video_path IS NULL OR video_path = '' OR poster_path IS NULL OR poster_path = '')
        '''
        params = (channel_id,)
        curs.execute(query, params)
        missing_videos = list(curs.fetchall())
        return missing_videos


def find_all_missing_videos(channel_id: int = None) -> Tuple[dict, dict]:
    """
    Find all videos that don't have a video file, but are found in the DB (taken from the channel's info_json).

    Yields a Channel Dict object, our Video id, and the "entry" of the video from the channel's info_json['entries'].
    """
    channel: Channel = get_channel(channel_id=channel_id, return_dict=False)
    if not channel.url:
        raise ChannelURLEmpty('No URL for this channel')

    # Check that the channel has some videos.  We can't be sure what is missing if we don't know what we have.
    if channel.refreshed is False:
        # Refresh this channel's videos.
        refresh_channel_videos(channel)

    match_regex = re.compile(channel.match_regex) if channel.match_regex else None

    # Convert the channel video entries into a form that allows them to be quickly retrieved without searching through
    # the entire entries list.
    channel_entries = {i['id']: i for i in channel.info_json['entries']}
    # Yield all videos not skipped.
    missing_videos = _find_all_missing_videos(channel_id)
    for video_id, source_id, channel_id in missing_videos:
        if channel.skip_download_videos and source_id in channel.skip_download_videos:
            # This video has been marked to skip.
            continue

        try:
            missing_video = channel_entries[source_id]
        except KeyError:
            logger.warning(f'Video {channel.name} / {source_id} is not in {channel.name} info_json')
            continue

        if not match_regex or (match_regex and missing_video['title'] and match_regex.match(missing_video['title'])):
            # No title match regex, or the title matches the regex.
            yield video_id, source_id, missing_video


UNRECOVERABLE_ERRORS = {
    '404: Not Found',
    'requires payment',
    'Content Warning',
    'Did not get any data blocks',
    'Sign in',
    'This live stream recording is not available.',
    'members-only content',
    "You've asked yt-dlp to download the URL",
}


def _skip_download(error):
    """Return True if the error is unrecoverable and the video should be skipped in the future."""
    error_str = str(error)
    for msg in UNRECOVERABLE_ERRORS:
        if msg in error_str:
            return True
    return False
