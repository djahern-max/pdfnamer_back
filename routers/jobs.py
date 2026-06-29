"""
routers/jobs.py  –  Job / Project Management
---------------------------------------------
Per-tenant job list. Invoices flagged as job costs link to a job via
PdfNaming.job_id; non-job costs are classified as Overhead.

Routes:
  GET    /api/jobs                 List jobs (?active_only=true to filter)
  POST   /api/jobs                 Create (or reactivate) a job
  PATCH  /api/jobs/{job_id}        Archive / restore a job (is_active)
  DELETE /api/jobs/{job_id}        Hard-delete a job (invoice links set null)
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from auth import require_tenant
from models.tenant import Job, Tenant

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])


class JobCreateRequest(BaseModel):
    name: str
    customer: str | None = None


class JobPatchRequest(BaseModel):
    is_active: bool


class JobResponse(BaseModel):
    id: int
    name: str
    customer: str | None
    is_active: bool


@router.get("", response_model=list[JobResponse])
async def list_jobs(
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
    active_only: bool = Query(default=False),
):
    stmt = select(Job).where(Job.tenant_id == tenant.id)
    if active_only:
        stmt = stmt.where(Job.is_active.is_(True))
    stmt = stmt.order_by(Job.name)
    result = await db.execute(stmt)
    return [
        JobResponse(id=j.id, name=j.name, customer=j.customer, is_active=j.is_active)
        for j in result.scalars().all()
    ]


@router.post("", response_model=JobResponse)
async def create_job(
    body: JobCreateRequest,
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Create a job. If the name already exists for this tenant, reactivate it."""
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Job name is required.")

    result = await db.execute(
        select(Job).where(Job.tenant_id == tenant.id, Job.name == name)
    )
    job = result.scalar_one_or_none()
    if job:
        job.is_active = True
        if body.customer is not None:
            job.customer = body.customer.strip() or None
    else:
        job = Job(
            tenant_id=tenant.id,
            name=name,
            customer=(body.customer.strip() or None) if body.customer else None,
        )
        db.add(job)
    await db.commit()
    await db.refresh(job)
    return JobResponse(
        id=job.id, name=job.name, customer=job.customer, is_active=job.is_active
    )


@router.patch("/{job_id}", response_model=JobResponse)
async def patch_job(
    job_id: int,
    body: JobPatchRequest,
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.tenant_id == tenant.id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found.")
    job.is_active = body.is_active
    await db.commit()
    await db.refresh(job)
    return JobResponse(
        id=job.id, name=job.name, customer=job.customer, is_active=job.is_active
    )


@router.delete("/{job_id}")
async def delete_job(
    job_id: int,
    tenant: Tenant = Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.tenant_id == tenant.id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found.")
    await db.delete(job)
    await db.commit()
    return {"deleted": job_id}
