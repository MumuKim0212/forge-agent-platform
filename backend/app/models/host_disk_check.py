"""The worker's latest host disk measurement, one row per checked location.

`label` is a fixed name for the location (`docker_data_root`, `database_dir`, `local_git_root`,
`backup_dir`), so a check overwrites its own row and the table never grows. `state` is one of
`services/host_disk.DiskState`: a path that could not be measured is `UNKNOWN` with null sizes,
never `OK`. No row is "never measured"; the first worker pass inserts them, so the migration
invents nothing.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class HostDiskCheck(Base):
    __tablename__ = "host_disk_checks"

    label: Mapped[str] = mapped_column(primary_key=True)
    path: Mapped[str | None] = mapped_column(default=None)
    free_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    free_percent: Mapped[float | None] = mapped_column(Float, default=None)
    state: Mapped[str]
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
