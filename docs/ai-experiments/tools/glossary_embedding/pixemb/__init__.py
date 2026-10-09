"""Pix/ate 용어집 RAG용 임베딩(BGE-M3) + Qdrant 테스트 패키지."""
import sys


def setup_console() -> None:
    """Windows 콘솔에서 한글이 깨지지 않도록 stdout/stderr를 UTF-8로 맞춘다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
