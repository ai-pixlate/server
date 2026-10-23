"""AUTH — 인증/셀러 (API-AUTH-01~05, 🟢9월).

간이 인증(JWT HS256). 로그인은 이메일로 셀러를 find-or-create 후 토큰 발급한다.
비밀번호는 간이 인증이라 검증하지 않는다(seller 테이블에 비번 컬럼 없음 — 실제 인증은
Cognito 예정). refresh/me는 Bearer 토큰의 seller_id를 사용.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.security import create_token, get_current_seller

router = APIRouter(tags=["Auth"])


class LoginRequest(BaseModel):
    email: str = Field(examples=["seller@example.com"])
    password: str = Field(default="", examples=["password123!"])  # 간이 인증: 미검증


class ConsentRequest(BaseModel):
    agreed: bool = True


def _seller_by_id(db: Session, seller_id: int):
    return db.execute(
        text("SELECT id, email, account_type FROM seller WHERE id = :s"),
        {"s": seller_id},
    ).mappings().first()


@router.post("/auth/login", summary="API-AUTH-01 셀러 로그인/JWT 발급 (DB)")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail={"code": "INVALID_INPUT", "message": "email is required"})

    r = db.execute(
        text("SELECT id, email FROM seller WHERE email = :e"), {"e": email},
    ).mappings().first()
    if not r:  # 간이 인증: 최초 로그인 시 셀러 자동 생성(개발 편의)
        r = db.execute(
            text(
                "INSERT INTO seller (cognito_sub, email, account_type) "
                "VALUES (:sub, :e, 'individual') RETURNING id, email"
            ),
            {"sub": f"local:{email}", "e": email},
        ).mappings().one()
        db.commit()

    token = create_token(r["id"], r["email"])
    return {"accessToken": token, "seller": {"id": r["id"], "email": r["email"]}}


@router.post("/auth/refresh", summary="API-AUTH-02 액세스 토큰 갱신 (DB)")
def refresh(seller_id: int = Depends(get_current_seller), db: Session = Depends(get_db)):
    r = _seller_by_id(db, seller_id)
    if not r:
        raise HTTPException(status_code=404, detail={"code": "SELLER_NOT_FOUND", "message": "seller not found"})
    return {"accessToken": create_token(r["id"], r["email"])}


@router.post("/auth/logout", summary="API-AUTH-03 로그아웃/토큰 무효화")
def logout():
    # 무상태 JWT라 서버 저장소가 없다 — 클라이언트가 토큰을 폐기한다.
    return {"success": True}


@router.get("/auth/me", summary="API-AUTH-04 현재 셀러 조회 (DB)")
def me(seller_id: int = Depends(get_current_seller), db: Session = Depends(get_db)):
    r = _seller_by_id(db, seller_id)
    if not r:
        raise HTTPException(status_code=404, detail={"code": "SELLER_NOT_FOUND", "message": "seller not found"})
    brand_id = db.execute(
        text("SELECT id FROM brand WHERE seller_id = :s ORDER BY id LIMIT 1"),
        {"s": seller_id},
    ).scalar()
    return {"id": r["id"], "email": r["email"], "accountType": r["account_type"], "brandId": brand_id}


@router.post("/consents", summary="API-AUTH-05 책임 한계 동의 기록")
def create_consent(body: ConsentRequest, seller_id: int = Depends(get_current_seller)):
    # 동의 저장 테이블 미도입 — 간이 응답(추후 consent 테이블 연결).
    return {"consentId": f"consent-{seller_id}", "consentType": "liability_limit", "agreed": body.agreed}
