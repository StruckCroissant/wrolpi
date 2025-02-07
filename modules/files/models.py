from sqlalchemy import Integer, Column, String, Computed
from sqlalchemy.orm import deferred

from wrolpi.common import Base, ModelHelper, get_media_directory, tsvector
from wrolpi.dates import TZDateTime
from wrolpi.media_path import MediaPathType


class File(ModelHelper, Base):
    __tablename__ = 'file'
    id = Column(Integer, primary_key=True)
    path = Column(MediaPathType)

    idempotency = Column(String)
    mimetype = Column(String)
    modification_datetime = Column(TZDateTime)
    size = Column(Integer)
    title = deferred(Column(String))  # used by textsearch column

    textsearch = deferred(
        Column(tsvector, Computed('''setweight(to_tsvector('english'::regconfig, title), 'A'::"char")''')))

    def __repr__(self):
        return f'<File id={self.id} path={self.path.relative_to(get_media_directory())} mime={self.mimetype}>'

    def __json__(self):
        path = self.path.path.relative_to(get_media_directory())
        d = dict(
            id=self.id,
            mimetype=self.mimetype,
            modified=self.modification_datetime,  # React browser expects this.
            path=path,
            size=self.size,
            key=path,
        )
        return d
