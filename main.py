#! /usr/bin/env python3
import argparse
import asyncio
import inspect
import logging
import sys

import pytz
from sanic import Sanic
from sanic.signals import Event

from wrolpi import root_api, BEFORE_STARTUP_FUNCTIONS, after_startup, limit_concurrent, admin
from wrolpi.common import logger, get_config, import_modules, check_media_directory
from wrolpi.dates import set_timezone
from wrolpi.downloader import download_manager
from wrolpi.vars import PROJECT_DIR, DOCKERIZED, PYTEST
from wrolpi.version import get_version_string

logger = logger.getChild('wrolpi-main')


def db_main(args):
    """
    Handle database migrations.  Currently this uses Alembic, supported commands are "upgrade" and "downgrade".
    """
    from alembic.config import Config
    from alembic import command
    from wrolpi.db import uri

    config = Config(PROJECT_DIR / 'alembic.ini')
    # Overwrite the Alembic config, the is usually necessary when running in a docker container.
    config.set_main_option('sqlalchemy.url', uri)

    logger.warning(f'DB URI: {uri}')

    if args.command == 'upgrade':
        command.upgrade(config, 'head')
    elif args.command == 'downgrade':
        command.downgrade(config, '-1')
    else:
        print(f'Unknown DB command: {args.command}')
        return 2

    return 0


INTERACTIVE_BANNER = '''
This is the interactive WROLPi shell.  Use this to interact with the WROLPi API library.

Example (get the duration of every video file):
from modules.videos.models import Video
from modules.videos.common import get_video_duration
videos = session.query(Video).filter(Video.video_path != None).all()
videos = list(videos)
for video in videos:
    get_video_duration(video.video_path.path)

Check local variables:
locals().keys()

'''


def launch_interactive_shell():
    """Launches an interactive shell with a DB session."""
    import code
    from wrolpi.db import get_db_session

    modules = import_modules()
    with get_db_session() as session:
        code.interact(banner=INTERACTIVE_BANNER, local=locals())


async def main(loop):
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='count')
    parser.add_argument('--version', action='store_true', default=False)
    parser.add_argument('-c', '--check-media', action='store_true', default=False,
                        help='Check that the media directory is mounted and has the correct permissions.'
                        )
    parser.add_argument('-i', '--interactive', action='store_true', default=False,
                        help='Enter an interactive shell with some WROLPi tools')

    sub_commands = parser.add_subparsers(title='sub-commands', dest='sub_commands')

    # Add the API parser, this will allow the user to specify host/port etc.
    api_parser = sub_commands.add_parser('api')
    root_api.init_parser(api_parser)

    # DB Parser for running Alembic migrations
    db_parser = sub_commands.add_parser('db')
    db_parser.add_argument('command', help='Supported commands: upgrade, downgrade')

    args = parser.parse_args()

    if args.interactive:
        launch_interactive_shell()
        return 0

    if args.version:
        # Print out the relevant version information, then exit.
        print(get_version_string())
        return 0

    if args.check_media:
        # Run the media directory check.  Exit with informative return code.
        result = check_media_directory()
        if result is False:
            return 1
        print('Media directory is correct.')
        return 0

    logger.warning(f'Starting with: {sys.argv}')
    await set_log_level(args)
    logger.debug(get_version_string())

    if DOCKERIZED:
        logger.info('Running in Docker')

    # Run DB migrations before anything else.
    if args.sub_commands == 'db':
        return db_main(args)

    # Set the Timezone
    config = get_config()
    if config.timezone:
        tz = pytz.timezone(config.timezone)
        set_timezone(tz)

    # Hotspot/throttle are not supported in Docker containers.
    if not DOCKERIZED and config.hotspot_on_startup:
        admin.enable_hotspot()
    if not DOCKERIZED and config.throttle_on_startup:
        admin.throttle_cpu_on()

    check_media_directory()

    # Import the API in every module.  Each API should attach itself to `root_api`.
    import_modules()

    # Run the startup functions
    for func in BEFORE_STARTUP_FUNCTIONS:
        try:
            logger.debug(f'Calling {func} before startup.')
            coro = func()
            if inspect.iscoroutine(coro):
                await coro
        except Exception as e:
            logger.warning(f'Startup {func} failed!', exc_info=e)

    # Run the API.
    return root_api.main(loop, args)


async def set_log_level(args):
    """
    Set the level at the root logger so all children that have been created (or will be created) share the same level.
    """
    root_logger = logging.getLogger()
    sa_logger = logging.getLogger('sqlalchemy.engine')
    if args.verbose == 1:
        root_logger.setLevel(logging.INFO)
    elif args.verbose and args.verbose == 2:
        root_logger.setLevel(logging.DEBUG)
    elif args.verbose and args.verbose >= 3:
        root_logger.setLevel(logging.DEBUG)
        sa_logger.setLevel(logging.DEBUG)

    # Always warn about the log level so we know what will be logged
    effective_level = logger.getEffectiveLevel()
    level_name = logging.getLevelName(effective_level)
    logger.warning(f'Logging level: {level_name}')


@after_startup
@limit_concurrent(1)
def periodic_downloads(app: Sanic, loop):
    """A simple function that perpetually calls downloader_manager.do_downloads() after sleeping."""
    # Set all downloads to new.
    download_manager.reset_downloads()

    config = get_config()
    if config.wrol_mode:
        logger.warning(f'Not starting download worker because WROL Mode is enabled.')
        download_manager.kill()
        return
    if config.download_on_startup is False:
        logger.warning(f'Not starting download worker because Downloads are disabled on startup.')
        download_manager.kill()
        return

    logger.info('Starting download manager.')

    async def _periodic_download():
        download_manager.start_workers()
        await download_manager.do_downloads()
        await asyncio.sleep(60)
        app.add_task(_periodic_download())  # noqa

    app.add_task(_periodic_download())


@root_api.api_app.signal(Event.SERVER_SHUTDOWN_BEFORE)
def handle_server_shutdown(*args, **kwargs):
    """Stop downloads when server is shutting down."""
    if not PYTEST:
        download_manager.stop()


if __name__ == '__main__':
    loop_ = asyncio.get_event_loop()
    result_ = loop_.run_until_complete(main(loop_))
    sys.exit(result_)
