"""OCR 실측(배포 OCR 환경 그대로). 새 CLI 자식 프로세스 1개의 최대 RSS·시간.

    cd <e1291ab 등 배포 SHA 소스> && <OCR venv python> ocr_measure.py <split.json> <section> <out> [limit_GiB]

- 같은 pod·호스트의 다른 프로세스를 보호하려고 자식(+후손) RSS 합이 limit_GiB(기본 20)를 넘으면 즉시 SIGKILL 하고 'watchdog_kill'로 기록한다.
- OCR 원문 텍스트·로그는 출력하지 않는다(종료 코드·시간·메모리·영역 수·score 범위·실행 정보만).
"""
import glob, hashlib, json, os, resource, signal, subprocess, sys, time
from importlib.metadata import version

split, section, out = sys.argv[1], sys.argv[2], sys.argv[3]
limit = float(sys.argv[4]) if len(sys.argv) > 4 else 20.0
sp = json.load(open(split))
sec = next(s for s in sp["sections"] if s["section_key"] == section)
img = os.path.join(os.path.dirname(split), sec["image_path"])
print("python", sys.version.split()[0], "| paddlepaddle", version("paddlepaddle"), "| paddleocr", version("paddleocr"), "| paddlex", version("paddlex"))
from PIL import Image
w, h = Image.open(img).size
print("input", f"{w}x{h}", "sha256", hashlib.sha256(open(img, "rb").read()).hexdigest())


def tree_rss_kib(pid):
    """pid 와 후손의 RSS 합(KiB)."""
    kids, total = {pid}, 0
    try:
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    st = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()
                    if int(st[1]) in kids:
                        kids.add(int(d))
                except OSError:
                    pass
        for k in kids:
            try:
                for line in open(f"/proc/{k}/status"):
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1])
            except OSError:
                pass
    except OSError:
        pass
    return total


cache = os.path.expanduser("~/.paddlex")
du = lambda p: sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(p) for f in fs) if os.path.isdir(p) else 0
print("cache_before_MiB", round(du(cache) / 2**20, 1))
t = time.monotonic()
p = subprocess.Popen([sys.executable, "-m", "pipeline.run", "ocr", "--split", split, "--section", section, "--out", out],
                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
peak_sampled, killed = 0, False
import threading
err = []
threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True).start()
while p.poll() is None:
    r = tree_rss_kib(p.pid)
    peak_sampled = max(peak_sampled, r)
    if r > limit * 1024 * 1024:
        p.send_signal(signal.SIGKILL)
        killed = True
        break
    time.sleep(0.2)
p.wait()
el = time.monotonic() - t
ru = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
print("exit", p.returncode, "| watchdog_kill", killed, "| elapsed_s", round(el, 3),
      "| child_maxrss_MiB", round(ru / 1024, 2), "| sampled_tree_peak_MiB", round(peak_sampled / 1024, 2))
if p.returncode and not killed:
    time.sleep(0.5)
    tail = (err[0] if err else b"").decode("utf-8", "replace").strip().splitlines()[-1:]
    print("stderr_last_type", tail[0].split(":")[0][:120] if tail else "")
print("cache_after_MiB", round(du(cache) / 2**20, 1))
for f in glob.glob(os.path.join(out, "ocr", "*.json")):
    regs = json.load(open(f)).get("regions", [])
    sc = [x.get("score") for x in regs]
    ok = all(isinstance(s, (int, float)) and 0 <= s <= 1 for s in sc)
    print("regions", len(regs), "| all_scores_0_1", ok, "| score_min", round(min(sc), 4) if sc else None, "| score_max", round(max(sc), 4) if sc else None)
for f in glob.glob(os.path.join(out, "**", "run.json"), recursive=True):
    r = json.load(open(f))
    def find(d, k):
        if isinstance(d, dict):
            for kk, v in d.items():
                if kk == k: yield v
                yield from find(v, k)
        elif isinstance(d, list):
            for v in d: yield from find(v, k)
    print("run.json device", list(find(r, "device")), "| enable_mkldnn", list(find(r, "enable_mkldnn")), "| platform", list(find(r, "platform"))[:1])
