"""deploy/bin/pixlate-postcheck — 공개 후 동작 확인이 health·digest·ping·재시작·모드·기록 실패를 각각 실패로 돌려주는지(구현안 6판 2.2 D11·2.2a)."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

if shutil.which("bash") is None or os.name == "nt":
    pytest.skip("bash 리눅스 환경 전용", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "deploy" / "bin" / "pixlate-postcheck"
SHA = "c" * 40
DG = "sha256:" + "3" * 64
MANAGED = ("pixlate-api", "pixlate-worker-cpu", "pixlate-recovery")

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
p = os.environ["FAKE_STATE"]
st = json.load(open(p))
a = sys.argv[1:]
if a[:2] == ["container", "inspect"]:
    c = st["containers"].get(a[4])
    if c is None:
        print("Error: No such container", file=sys.stderr); sys.exit(1)
    if a[4] in st.get("restarting", []):
        c["restarts"] += 1; c["started"] = "t%d" % c["restarts"]; json.dump(st, open(p, "w"))
    print("|".join([c["status"], c["image"], str(c["restarts"]), "true" if c["oom"] else "false", c["started"]]))
    sys.exit(0)
if a[:2] == ["exec", "pixlate-worker-cpu"]:
    if a[2] == "hostname":
        print("abc123"); sys.exit(0)
    if "ping" in a:
        if st.get("ping_ok") and "cpu@abc123" in a:
            print("-> cpu@abc123: OK\n        pong"); sys.exit(0)
        print("Error: No nodes replied within time constraint"); sys.exit(1)
sys.exit(2)
'''


def _exe(path: Path, body: str) -> str:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def pc(tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "release.conf").write_text(f"RELEASE_SHA={SHA}\nAPI_DIGEST={DG}\n")
    (conf / "deploy.mode").write_text("live\n")
    (conf / "deploy.mode.applied").write_text("live\n")
    state = tmp_path / "docker.json"
    state.write_text(json.dumps({"ping_ok": True, "containers": {
        c: {"status": "running", "image": f"reg/pixlate-api@{DG}", "restarts": 0, "oom": False, "started": "t0"} for c in MANAGED}}))
    rec = tmp_path / "record.json"
    rec.write_text(json.dumps({"run_id": "r1", "state": "incomplete", "step": "live"}))
    transition = _exe(tmp_path / "fake-transition",
                      "#!/usr/bin/env python3\nimport json,sys\nr=json.load(open(%r))\nprint(r.get(sys.argv[3], ''))\n" % str(rec))
    env = {**os.environ, "CONF_DIR": str(conf), "DOCKER": _exe(tmp_path / "fake-docker", FAKE_DOCKER),
           # -w '%{http_code}' 흉내: 파일에 적힌 "응답 코드 [종료 코드]" 를 따른다(없으면 200·0). 000 = 연결 실패(curl 종료 7)
           "CURL": _exe(tmp_path / "fake-curl", "#!/bin/sh\nset -- $(cat \"$FAKE_HEALTH_CODE\" 2>/dev/null || echo 200)\n"
                                                "printf '%s' \"$1\"\n[ \"$1\" = 000 ] && exit 7\nexit ${2:-0}\n"),
           "TRANSITION": transition, "SETTLE_S": "0", "FAKE_STATE": str(state), "FAKE_HEALTH_CODE": str(tmp_path / "health-code")}

    class PC:
        def run(self, *extra):
            return subprocess.run(["bash", str(TOOL), "--sha", SHA, "--digest", DG, *extra], env=env, capture_output=True, text=True)

        def docker(self, fn):
            st = json.loads(state.read_text())
            fn(st)
            state.write_text(json.dumps(st))

    o = PC()
    o.conf, o.rec, o.tmp = conf, rec, tmp_path
    return o


def _fails(r):
    return sorted(line.split()[1] for line in r.stdout.splitlines() if line.startswith("CHECK ") and line.split()[2] == "fail")


def test_all_ok(pc):
    r = pc.run("--run-id", "r1")
    assert r.returncode == 0, r.stdout
    assert r.stdout.strip().splitlines()[-1] == "POSTCHECK ok"


@pytest.mark.parametrize("code", ["500", "302", "204", "000"])
def test_health_must_be_exactly_200(pc, code):
    (pc.tmp / "health-code").write_text(code)  # curl -f 였다면 302·204 는 통과했다
    r = pc.run()
    assert r.returncode == 1 and _fails(r) == ["health"]
    assert f"HTTP {code}" in r.stdout


@pytest.mark.parametrize("curl_exit", ["28", "18", "56"])
def test_health_200_but_curl_failed_is_failure(pc, curl_exit):
    (pc.tmp / "health-code").write_text(f"200 {curl_exit}")  # 200 헤더 뒤 시간 초과·전송 중단
    r = pc.run()
    assert r.returncode == 1 and _fails(r) == ["health"]
    assert f"curl 종료 {curl_exit}" in r.stdout


def test_digest_mismatch_and_not_running(pc):
    pc.docker(lambda st: st["containers"]["pixlate-api"].update(image="reg/pixlate-api@sha256:" + "9" * 64))
    pc.docker(lambda st: st["containers"]["pixlate-recovery"].update(status="exited"))
    r = pc.run()
    assert r.returncode == 1
    assert {"container:pixlate-api", "container:pixlate-recovery"} <= set(_fails(r))


def test_ping_failure(pc):
    pc.docker(lambda st: st.update(ping_ok=False))
    r = pc.run()
    assert r.returncode == 1 and _fails(r) == ["ping"]


def test_restart_during_settle_and_oom(pc):
    pc.docker(lambda st: st.update(restarting=["pixlate-worker-cpu"]))
    pc.docker(lambda st: st["containers"]["pixlate-api"].update(oom=True))
    r = pc.run()
    assert r.returncode == 1
    assert {"stable:pixlate-worker-cpu", "container:pixlate-api"} <= set(_fails(r))


def test_mode_and_release_mismatch(pc):
    (pc.conf / "deploy.mode.applied").write_text("live-incomplete\n")
    (pc.conf / "release.conf").write_text(f"RELEASE_SHA={'d' * 40}\nAPI_DIGEST={DG}\n")
    r = pc.run()
    assert r.returncode == 1 and {"mode", "release"} <= set(_fails(r))


def test_record_checks(pc):
    assert pc.run("--run-id", "other").returncode == 1
    r = pc.run("--run-id", "r1", "--resume")  # 기록은 incomplete — resume 은 live-confirmed 를 기대
    assert r.returncode == 1 and _fails(r) == ["record"]
    pc.rec.write_text(json.dumps({"run_id": "r1", "state": "live-confirmed", "step": "live"}))
    assert pc.run("--run-id", "r1", "--resume").returncode == 0


def test_container_absent_is_failure(pc):
    pc.docker(lambda st: st["containers"].pop("pixlate-recovery"))
    r = pc.run()
    assert r.returncode == 1 and "container:pixlate-recovery" in _fails(r)


def test_usage(pc):
    r = subprocess.run(["bash", str(TOOL), "--sha", "x", "--digest", DG], capture_output=True, text=True)
    assert r.returncode == 2
