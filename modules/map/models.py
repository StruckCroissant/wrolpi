from pathlib import Path

from sqlalchemy import Column, Integer, Boolean, BigInteger

from wrolpi import dates
from wrolpi.common import Base, ModelHelper
from wrolpi.media_path import MediaPathType

RPI4_PBF_BYTES_PER_SECOND = 3963


def seconds_to_import(path: Path, size: int) -> int:
    """Attempt to predict how long it will take an RPi4 to import a given PBF file."""
    if str(path).endswith('.osm.pbf'):
        return max(int(size // RPI4_PBF_BYTES_PER_SECOND), 0)

    # Can't calculate an unknown file.
    return 0


class MapFile(Base, ModelHelper):
    __tablename__ = 'map_file'
    id = Column(Integer, primary_key=True)

    path = Column(MediaPathType, unique=True, nullable=False)
    imported = Column(Boolean, default=False, nullable=False)
    size = Column(BigInteger)

    def __repr__(self):
        return f'<MapFile {self.path} imported={self.imported}>'

    def __json__(self):
        return {
            'id': self.id,
            'path': self.path,
            'imported': self.imported,
            'size': self.size,
            'time_to_import': dates.seconds_to_timestamp(seconds_to_import(self.path.path, self.size)),
        }
