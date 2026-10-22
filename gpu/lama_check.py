"""학교 GPU 실제 워커 Python 점검 — torch import · CUDA · 가중치 MD5 · LaMa 모델 로드. 추론·큐 없음.

    cd /data/server && /data/venv/bin/python /data/pixlate/bin/lama_check.py
종료 코드: 0 전부 통과 / 1 하나라도 실패. 가중치 로드는 큐 통합 성공이 아니다.

LaMa 는 spawn 자식 프로세스에서 가중치를 읽는다. spawn 자식은 이 스크립트를 다시 import 하므로
점검 코드는 반드시 `if __name__ == "__main__":` 아래에서만 실행한다.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.getcwd())  # 스크립트는 레포 밖에 있으므로 작업 디렉터리(레포 루트)의 app·pipeline 을 import 한다


def main() -> int:
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            print(f"{name}: {fn()}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"{name}: FAIL {type(e).__name__}: {str(e)[:200]}")

    import torch

    check("torch", lambda: f"{torch.__version__} from {torch.__file__.split('/site-packages')[0]}")
    check("cuda", lambda: (torch.cuda.is_available() and torch.cuda.get_device_name(0)) or (_ for _ in ()).throw(RuntimeError("no cuda")))

    from pipeline.stages import inpaint, inpaint_lama

    p = inpaint_lama.default_weights_path()

    def md5():
        h = hashlib.md5()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        if h.hexdigest() != inpaint_lama.LAMA_MD5:
            raise ValueError("MD5 mismatch")
        return f"present, md5 ok ({p.name} under torch hub cache)"

    check("weights", md5)

    def load():
        from pipeline.config import load_config

        m = inpaint.build_model(load_config())  # spawn 자식에서 가중치 로드
        try:
            d = m.describe() if hasattr(m, "describe") else {}
            return f"loaded ({', '.join(sorted(d)) if isinstance(d, dict) else 'ok'})"
        finally:
            if hasattr(m, "close"):
                m.close()

    check("model_load", load)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
