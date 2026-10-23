"""컨테이너 안에서 GPU·PyTorch·스토리지 상태를 한 번에 확인한다.

사용법: python /opt/pixlate/check_gpu.py
"""

import os
import shutil
import subprocess
import sys

import torch


def section(title):
    print(f"\n== {title}")


def main():
    ok = True

    section("PyTorch")
    print(f"torch          : {torch.__version__}")
    print(f"CUDA (빌드)    : {torch.version.cuda}")
    arch_list = torch._C._cuda_getArchFlags().split() if torch.version.cuda else []
    print(f"지원 아키텍처  : {' '.join(arch_list) or '-'}")

    section("nvidia-smi")
    if shutil.which("nvidia-smi"):
        subprocess.run(["nvidia-smi"], check=False)
    else:
        print("nvidia-smi 없음 (GPU가 할당되지 않은 컨테이너일 수 있음)")

    section("GPU")
    if not torch.cuda.is_available():
        print("torch.cuda.is_available() = False")
        print("→ Workload의 GPU Limit, 드라이버 버전(README의 호환 표)을 확인하세요.")
        ok = False
    else:
        for i in range(torch.cuda.device_count()):
            major, minor = torch.cuda.get_device_capability(i)
            arch = f"sm_{major}{minor}"
            supported = arch in arch_list
            mem_gb = torch.cuda.get_device_properties(i).total_memory / 1024**3
            print(f"[{i}] {torch.cuda.get_device_name(i)} · {mem_gb:.1f}GB · {arch} "
                  f"({'지원' if supported else '미지원 — 다른 CUDA 빌드 필요'})")
            ok &= supported

        if ok:
            x = torch.randn(2048, 2048, device="cuda")
            y = (x @ x).sum().item()
            torch.cuda.synchronize()
            print(f"행렬곱 연산 테스트 통과 (sum={y:.3e})")

    section("스토리지")
    data_dir = os.environ.get("DATA_DIR", "/data")
    if os.path.ismount(data_dir) or os.path.isdir(data_dir):
        total, used, free = shutil.disk_usage(data_dir)
        print(f"{data_dir}: 전체 {total / 1024**3:.0f}GB · 사용 {used / 1024**3:.0f}GB · 여유 {free / 1024**3:.0f}GB")
        if not os.path.ismount(data_dir):
            print(f"경고: {data_dir} 가 볼륨으로 마운트되어 있지 않습니다. 재시작 시 데이터가 사라집니다.")
    else:
        print(f"{data_dir} 없음")
        ok = False

    print("\n결과:", "정상" if ok else "확인 필요")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
