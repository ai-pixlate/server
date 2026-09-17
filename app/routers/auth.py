"""AUTH — 인증/셀러 (API-AUTH-01~05, 🟢9월). 모두 mock 응답."""
from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(tags=["Auth"])


class LoginRequest(BaseModel):
    email: str = Field(examples=["seller@example.com"])
    password: str = Field(examples=["password123!"])


class LoginResponse(BaseModel):
    accessToken: str = Field(examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock"])
    seller: dict = Field(examples=[{"id": "seller-001", "email": "seller@example.com", "name": "테스트 셀러"}])


class MeResponse(BaseModel):
    id: str = "seller-001"
    email: str = "seller@example.com"
    name: str = "테스트 셀러"
    brandId: str = "brand-001"


class ConsentRequest(BaseModel):
    agreed: bool = True


class ConsentResponse(BaseModel):
    consentId: str = "consent-001"
    consentType: str = "liability_limit"
    consentedAt: str = "2026-09-16T00:00:00Z"


@router.post("/auth/login", response_model=LoginResponse, summary="API-AUTH-01 셀러 로그인/JWT 발급")
def login(body: LoginRequest):
    # refresh 토큰은 실제로는 httpOnly 쿠키. mock 이라 본문만 반환.
    return LoginResponse(
        accessToken="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock",
        seller={"id": "seller-001", "email": body.email, "name": "테스트 셀러"},
    )


@router.post("/auth/refresh", summary="API-AUTH-02 액세스 토큰 갱신")
def refresh():
    return {"accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.refreshed.mock"}


@router.post("/auth/logout", summary="API-AUTH-03 로그아웃/토큰 무효화")
def logout():
    return {"success": True}


@router.get("/auth/me", response_model=MeResponse, summary="API-AUTH-04 현재 셀러 조회")
def me():
    return MeResponse()


@router.post("/consents", response_model=ConsentResponse, summary="API-AUTH-05 책임 한계 동의 기록")
def create_consent(body: ConsentRequest):
    return ConsentResponse()
