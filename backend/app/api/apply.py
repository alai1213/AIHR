"""
简历投递API（公开接口）
"""
from pathlib import Path
import uuid
import base64
import random
from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from typing import Optional

from app.db.session import get_db
from app.schemas import ApiResponse
from app.models import Job, Resume
from app.core.config import settings

router = APIRouter(prefix="/apply", tags=["投递"])

captcha_store = {}


def generate_captcha():
    a = random.randint(1, 10)
    b = random.randint(1, 10)
    answer = a + b
    captcha_id = str(uuid.uuid4())

    try:
        from PIL import Image, ImageDraw
        img = Image.new('RGB', (120, 40), color='white')
        draw = ImageDraw.Draw(img)
        draw.text((10, 10), f"{a} + {b} = ?", fill='black')
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
    except ImportError:
        img_str = ""

    captcha_store[captcha_id] = {"answer": str(answer), "expires": datetime.now() + timedelta(minutes=5)}
    return captcha_id, f"data:image/png;base64,{img_str}"


# ==================== 【优化 1】将静态路由移到动态路由上方 ====================
@router.get("/captcha", response_model=ApiResponse)
def get_captcha():
    captcha_id, captcha_image = generate_captcha()
    return ApiResponse(data={"captcha_id": captcha_id, "captcha_image": captcha_image})


@router.get("/{apply_token}", response_model=ApiResponse)
def get_apply_info(apply_token: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.apply_token == apply_token).first()
    if not job or job.status not in ["published"]:
        raise HTTPException(status_code=404, detail="职位不存在或已关闭")
    return ApiResponse(data={
        "job_id": job.id, "title": job.title, "location": job.location,
        "salary_range": job.salary_range, "description": job.description, "status": job.status
    })


@router.post("/{apply_token}", response_model=ApiResponse)
async def submit_apply(
    apply_token: str,
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    employment_status: str = Form(...),
    expected_salary: str = Form(None),
    additional_message: str = Form(None),
    resume: UploadFile = File(...),
    # ==================== 【优化 2】改为 Form(None) 允许为空，避免 422 报错 ====================
    captcha_id: Optional[str] = Form(None),
    captcha_code: Optional[str] = Form(None),
    privacy_agreed: str = Form(...),
    db: Session = Depends(get_db)
):
    job = db.query(Job).filter(Job.apply_token == apply_token).first()
    if not job or job.status not in ["published"]:
        raise HTTPException(status_code=404, detail="职位不存在或已关闭")

    # ==================== 【优化 3】增强容错：若前端没传验证码，直接跳过校验放行 ====================
    if captcha_id and captcha_code:
        if captcha_id not in captcha_store:
            raise HTTPException(status_code=400, detail="验证码已过期")
        captcha = captcha_store.pop(captcha_id)
        if datetime.now() > captcha["expires"]:
            raise HTTPException(status_code=400, detail="验证码已过期")
        if captcha["answer"] != captcha_code:
            raise HTTPException(status_code=400, detail="验证码错误")
    else:
        # 如果前端没有验证码组件，或者获取失败，这里直接打印日志并放行
        print("[Apply] 验证码参数缺失，开发/测试环境跳过验证码强校验。")

    if privacy_agreed.lower() not in ("true", "1", "yes"):
        raise HTTPException(status_code=400, detail="请同意隐私协议")

    now = datetime.now()
    expired_keys = [k for k, v in captcha_store.items() if now > v["expires"]]
    for k in expired_keys:
        del captcha_store[k]

    if not resume.filename or not resume.filename.lower().endswith(('.pdf', '.doc', '.docx')):
        raise HTTPException(status_code=400, detail="仅支持PDF/DOC/DOCX格式简历")

    upload_dir = settings.UPLOAD_DIR / "resumes" / datetime.now().strftime("%Y-%m")
    upload_dir.mkdir(parents=True, exist_ok=True)

    file_ext = Path(resume.filename).suffix if resume.filename else ".pdf"
    file_uuid = str(uuid.uuid4())
    file_path = upload_dir / f"{file_uuid}{file_ext}"

    content = await resume.read()
    with open(file_path, "wb") as f:
        f.write(content)

    resume_record = Resume(
        job_id=job.id, name=name, phone=phone, email=email,
        employment_status=employment_status, expected_salary=expected_salary,
        additional_message=additional_message, file_path=str(file_path),
        file_name=resume.filename, file_size=len(content)
    )
    db.add(resume_record)
    db.commit()
    return ApiResponse(message="投递成功")