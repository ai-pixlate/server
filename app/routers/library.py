"""LIB — 보관함 (API-LIB-01, 🟢9월). 모두 mock 응답."""
from fastapi import APIRouter

router = APIRouter(tags=["Library"])


@router.get("/library", summary="API-LIB-01 보관함 카드 목록")
def library():
    return [
        {
            "jobId": "job-001",
            "productName": "수분 크림 50ml",
            "brandName": "Pixlate",
            "targetCountry": "US",
            "targetLanguage": "en",
            "specType": "original",
            "thumbnailUrl": "https://example-bucket.s3.amazonaws.com/thumb/job-001.png?presigned=mock",
            "savedAt": "2026-09-16T00:00:00Z",
        }
    ]
