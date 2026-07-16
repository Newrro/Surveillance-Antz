"""identity_repo/employees.py — employees table."""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Employee

from .labels import _serialize_label_allocation


# ---------------------------------------------------------------------------
# employees
# ---------------------------------------------------------------------------
async def next_employee_seq(session: AsyncSession, year: int) -> int:
    await _serialize_label_allocation(session, "employee")
    result = await session.execute(
        select(func.max(Employee.employee_seq)).where(Employee.year == year)
    )
    return (result.scalar() or 0) + 1


async def insert_employee(
    session: AsyncSession,
    identity_id: int,
    employee_seq: int,
    year: int,
    name: str,
    department: str,
    email: Optional[str] = None,
    external_id: Optional[str] = None,
) -> Employee:
    employee = Employee(
        identity_id=identity_id,
        employee_seq=employee_seq,
        year=year,
        name=name,
        department=department,
        email=email,
        external_id=external_id,
    )
    session.add(employee)
    await session.flush()
    return employee


async def delete_employee(session: AsyncSession, identity_id: int) -> int:
    result = await session.execute(
        Employee.__table__.delete().where(Employee.identity_id == identity_id)
    )
    return result.rowcount or 0


async def fetch_employee(session: AsyncSession, identity_id: int) -> Optional[Employee]:
    return await session.get(Employee, identity_id)


async def list_employees(session: AsyncSession, limit: int = 500, offset: int = 0) -> List[Employee]:
    """All employees — GET /employees."""
    stmt = select(Employee).order_by(Employee.year.desc(), Employee.employee_seq.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def fetch_employee_by_external_id(session: AsyncSession, external_id: str) -> Optional[Employee]:
    result = await session.execute(
        select(Employee).where(Employee.external_id == external_id)
    )
    return result.scalar_one_or_none()
