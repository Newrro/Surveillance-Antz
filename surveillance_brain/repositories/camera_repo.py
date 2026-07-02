"""
repositories/camera_repo.py
===========================
Thin wrapper over the `cameras` table.

The `is_exit_camera` flag is the critical piece — session_tracker uses it
to decide whether a detection should CLOSE an open presence session
(person leaving the facility) vs. merely refresh the Redis presence
hash (person moving between zones).
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Camera


async def fetch_camera_by_uid(session: AsyncSession, camera_uid: str) -> Optional[Camera]:
    """Look up by the stable string identifier used in AI payloads."""
    result = await session.execute(select(Camera).where(Camera.camera_uid == camera_uid))
    return result.scalar_one_or_none()


async def fetch_camera_by_id(session: AsyncSession, camera_id: int) -> Optional[Camera]:
    """Look up by primary key."""
    return await session.get(Camera, camera_id)


async def fetch_all_cameras(session: AsyncSession, active_only: bool = True) -> List[Camera]:
    """Return all cameras — used by the seed/bootstrap script and admin UI."""
    stmt = select(Camera).order_by(Camera.camera_uid)
    if active_only:
        stmt = stmt.where(Camera.is_active.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def insert_camera(
    session: AsyncSession,
    camera_uid: str,
    name: str,
    zone_id: str,
    is_exit_camera: bool = False,
    stream_url: Optional[str] = None,
    is_active: bool = True,
) -> Camera:
    """Insert a single camera row."""
    cam = Camera(
        camera_uid=camera_uid,
        name=name,
        zone_id=zone_id,
        is_exit_camera=is_exit_camera,
        stream_url=stream_url,
        is_active=is_active,
    )
    session.add(cam)
    await session.flush()
    return cam


async def is_exit_camera(session: AsyncSession, camera_uid: str) -> bool:
    """
    Convenience — returns True if the camera is an exit camera.

    Returns False for unknown cameras (defensive — unknown cameras should
    not have been able to send events in the first place).
    """
    cam = await fetch_camera_by_uid(session, camera_uid)
    return bool(cam and cam.is_exit_camera)
