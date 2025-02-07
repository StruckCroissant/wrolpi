import pathlib
from typing import List
from urllib.request import Request

from sanic import response
from sanic_ext import validate
from sanic_ext.extensions.openapi import openapi

from wrolpi.common import get_media_directory, api_param_limiter
from wrolpi.errors import InvalidFile
from wrolpi.root_api import get_blueprint, json_response
from . import lib, schema

bp = get_blueprint('Files', '/api/files')


def paths_to_files(paths: List[pathlib.Path]):
    """Convert Paths to what the React UI expects."""
    media_directory = get_media_directory()
    new_files = []
    for path in paths:
        stat = path.stat()
        key = path.relative_to(media_directory)
        modified = stat.st_mtime
        if path.is_dir():
            key = f'{key}/'
            new_files.append(dict(
                key=key,
                modified=modified,
                url=key,
                name=path.name,
            ))
        else:
            # A File should know it's size.
            new_files.append(dict(
                key=key,
                modified=modified,
                size=stat.st_size,
                url=key,
                name=path.name,
            ))
    return new_files


@bp.post('/')
@openapi.definition(
    summary='List files in a directory',
    body=schema.FilesRequest,
)
@validate(schema.FilesRequest)
async def get_files(_: Request, body: schema.FilesRequest):
    directories = body.directories or []

    files = lib.list_files(directories)
    files = paths_to_files(files)
    return json_response({'files': files})


@bp.post('/delete')
@openapi.definition(
    summary='Delete a single file.  Returns an error if WROL Mode is enabled.',
    body=schema.DeleteRequest,
)
@validate(schema.DeleteRequest)
async def delete_file(_: Request, body: schema.DeleteRequest):
    if not body.file:
        raise InvalidFile('file cannot be empty')
    lib.delete_file(body.file)
    return response.empty()


files_limit_limiter = api_param_limiter(100)
files_offset_limiter = api_param_limiter(100, 0)


@bp.post('/refresh')
@openapi.description('Find and index all files')
async def refresh(_: Request):
    lib.refresh_files()
    return response.empty()


@bp.post('/search')
@openapi.definition(
    summary='Search Files',
    body=schema.FilesSearchRequest,
)
@validate(schema.FilesSearchRequest)
async def search_files(_: Request, body: schema.FilesSearchRequest):
    files, total = lib.search(body.search_str, body.limit, body.offset)
    return json_response(dict(files=files, totals=dict(files=total)))
