"""presigned_get 의 다운로드 이름(Content-Disposition) — 네트워크 없이 서명만 한다(가짜 자격)."""
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.config import Config

from app import s3


@pytest.fixture
def offline_s3(monkeypatch):
    client = boto3.client("s3", region_name="ap-northeast-2", aws_access_key_id="AKIAEXAMPLEEXAMPLE00",
                          aws_secret_access_key="x" * 40, config=Config(signature_version="s3v4"))
    monkeypatch.setattr(s3, "_s3", client)
    monkeypatch.setattr(s3, "S3_BUCKET", "example-bucket")


def _query(url):
    return parse_qs(urlparse(url).query)


def test_download_name_sets_response_content_disposition(offline_s3):
    q = _query(s3.presigned_get("export/14/abc.zip", download_name="pixlate_export_14.zip"))
    assert q["response-content-disposition"] == [
        "attachment; filename=\"pixlate_export_14.zip\"; filename*=UTF-8''pixlate_export_14.zip"]
    assert "X-Amz-Signature" in q and q["X-Amz-SignedHeaders"] == ["host"]


def test_without_download_name_url_unchanged(offline_s3):
    # 화면 표시용(원본·산출물·로고) 호출은 attachment 를 붙이지 않는다
    assert "response-content-disposition" not in _query(s3.presigned_get("source/1/a.png"))
    assert s3.presigned_get(None) is None


@pytest.mark.parametrize("name,want", [
    ("pixlate_export_14.csv", "attachment; filename=\"pixlate_export_14.csv\"; filename*=UTF-8''pixlate_export_14.csv"),
    ('a"b\\c;d 한글.csv', "attachment; filename=\"a_b_c;d __.csv\"; filename*=UTF-8''a%22b%5Cc%3Bd%20%ED%95%9C%EA%B8%80.csv"),
    # 제어문자(CR·LF·탭·DEL·C1) 는 filename·filename* 양쪽에서 _ — 헤더 분리·파일명 오염 방지
    ("x\r\ny.zip", "attachment; filename=\"x__y.zip\"; filename*=UTF-8''x__y.zip"),
    ("a\tb\x7fc\x85d.csv", "attachment; filename=\"a_b_c_d.csv\"; filename*=UTF-8''a_b_c_d.csv"),
    ("보고서 최종.zip", "attachment; filename=\"___ __.zip\"; filename*=UTF-8''%EB%B3%B4%EA%B3%A0%EC%84%9C%20%EC%B5%9C%EC%A2%85.zip"),
])
def test_content_disposition_escapes_special_characters(name, want):
    got = s3.content_disposition(name)
    assert got == want
    assert "\r" not in got and "\n" not in got and "%0D" not in got and "%0A" not in got
