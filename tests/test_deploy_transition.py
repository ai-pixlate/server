"""deploy/bin/pixlate-transition — 전환 기록·단계 요청·abort·recover 기록 갱신(구현안 6판 2.1). 가짜 docker·pixlate-mode 로 시험한다.

실제 Docker·systemd·부팅 경로(start-pixlate 연결)는 PR B 의 dind·systemd 시험 범위다. 여기서는 판정 표와 기록 쓰기·잠금만 본다.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fcntl")  # 리눅스 호스트 도구(flock)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "deploy" / "bin" / "pixlate-transition"
SHA_A, SHA_B = "a" * 40, "b" * 40
DG_A, DG_B = "sha256:" + "1" * 64, "sha256:" + "2" * 64
MANAGED = ("pixlate-api", "pixlate-worker-cpu", "pixlate-recovery")

FAKE_DOCKER = r'''#!{py}
import json, os, sys
state_path = os.environ["FAKE_DOCKER_STATE"]
st = json.load(open(state_path))
a = sys.argv[1:]
def save(): json.dump(st, open(state_path, "w"))
if os.path.exists(state_path + ".broken"):
    print("Cannot connect to the Docker daemon", file=sys.stderr); sys.exit(1)
if a[:2] == ["container", "inspect"]:
    fmt, name = a[3], a[4]
    c = st["containers"].get(name)
    if c is None:
        print(f"Error: No such container: {{name}}", file=sys.stderr); sys.exit(1)
    if "RestartPolicy" in fmt: print(c["policy"])
    else: print(c["status"], c["image"])
    sys.exit(0)
if a[0] == "update":
    pol, name = a[1].split("=", 1)[1], a[2]
    if name in os.environ.get("FAKE_DOCKER_FAIL_UPDATE", "").split(","): sys.exit(1)
    if os.environ.get("FAKE_DOCKER_BREAK_ON_UPDATE"): open(state_path + ".broken", "w").close()
    st["containers"][name]["policy"] = pol; save(); sys.exit(0)
sys.exit(2)
'''

FAKE_MODE = r'''#!{py}
# pixlate-mode + start-pixlate 흉내: 모드 기록 → (L2 가 비어 있는지 기록) → consume → 적용 결과 기록
import fcntl, os, subprocess, sys
mode = sys.argv[1]
conf = os.environ["PIXLATE_CONF_DIR"]
open(os.path.join(conf, "deploy.mode"), "w").write(mode + "\n")
fd = os.open(os.path.join(os.environ["PIXLATE_LOCK_DIR"], "pixlate-transition.lock"), os.O_RDWR | os.O_CREAT)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); probe = "free"; fcntl.flock(fd, fcntl.LOCK_UN)
except BlockingIOError:
    probe = "held"
os.close(fd)
open(os.environ["FAKE_LOCK_PROBE"], "a").write(probe + "\n")
rel = dict(l.strip().split("=", 1) for l in open(os.path.join(conf, "release.conf")) if "=" in l)
r = subprocess.run([sys.executable, os.environ["FAKE_TOOL"], "consume", "--mode", mode,
                    "--sha", rel.get("RELEASE_SHA", ""), "--digest", rel.get("API_DIGEST", "")], capture_output=True, text=True)
applied = mode if r.returncode == 0 else "maintenance"
if os.environ.get("FAKE_MODE_FAIL") == mode: applied = mode + "-incomplete"
open(os.path.join(conf, "deploy.mode.applied"), "w").write(applied + "\n")
sys.exit(0 if applied == mode else 1)
'''


def _script(path: Path, body: str) -> str:
    path.write_text(body.format(py=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.state, self.lock, self.conf = tmp / "state", tmp / "lock", tmp / "conf"
        for d in (self.state, self.lock, self.conf):
            d.mkdir()
        self.boot = tmp / "boot_id"
        self.boot.write_text("boot-1\n")
        self.docker_state = tmp / "docker.json"
        self.probe = tmp / "lock-probe"
        self.containers({c: {"policy": "unless-stopped", "status": "running", "image": f"reg/pixlate-api@{DG_A}"} for c in MANAGED})
        self.release(SHA_A, DG_A)
        (self.conf / "deploy.mode").write_text("live\n")
        (self.conf / "deploy.mode.applied").write_text("live\n")
        self.env = {
            **os.environ,
            "PIXLATE_STATE_DIR": str(self.state), "PIXLATE_LOCK_DIR": str(self.lock), "PIXLATE_CONF_DIR": str(self.conf),
            "PIXLATE_BOOT_ID_FILE": str(self.boot), "PIXLATE_LOCK_WAIT_S": "2",
            "DOCKER": _script(tmp / "fake-docker", FAKE_DOCKER), "PIXLATE_MODE": _script(tmp / "fake-mode", FAKE_MODE),
            "PIXLATE_HEALTH_CMD": "true", "FAKE_DOCKER_STATE": str(self.docker_state), "FAKE_LOCK_PROBE": str(self.probe),
            "FAKE_TOOL": str(TOOL),
        }

    def containers(self, cs: dict) -> None:
        self.docker_state.write_text(json.dumps({"containers": cs}))

    def policies(self) -> dict:
        return {k: v["policy"] for k, v in json.loads(self.docker_state.read_text())["containers"].items()}

    def release(self, sha: str, digest: str) -> None:
        (self.conf / "release.conf").write_text(f"RELEASE_SHA={sha}\nAPI_DIGEST={digest}\n")

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(TOOL), *args], env={**self.env, **extra}, capture_output=True, text=True)

    def record(self) -> dict | None:
        p = self.state / "transition.json"
        return json.loads(p.read_text()) if p.exists() else None

    def patch(self, **fields) -> None:
        rec = self.record()
        rec.update(fields)
        (self.state / "transition.json").write_text(json.dumps(rec))

    def begin(self, run_id: str = "r1", sequence: str = "sandbox", **extra) -> subprocess.CompletedProcess:
        return self.run("begin", "--run-id", run_id, "--kind", "deploy", "--sequence", sequence, "--sha", SHA_B, "--digest", DG_B, **extra)

    def to_maintenance(self, run_id: str = "r1") -> None:
        assert self.run("mark-stop", "--run-id", run_id).returncode == 0
        assert self.run("step", "--run-id", run_id, "--to", "maintenance").returncode == 0, self.record()

    def consume(self, mode: str, sha: str = SHA_B, digest: str = DG_B) -> subprocess.CompletedProcess:
        return self.run("consume", "--mode", mode, "--sha", sha, "--digest", digest)


@pytest.fixture
def e(tmp_path):
    return Env(tmp_path)


# ---- begin --------------------------------------------------------------------------------------------------
def test_begin_keeps_previous_release_then_sets_all_no(e):
    r = e.begin()
    assert r.returncode == 0, r.stdout
    rec = e.record()
    assert rec["state"] == "incomplete" and rec["step"] == "precheck" and rec["stop_started"] is False
    assert rec["prev"]["sha"] == SHA_A and rec["prev"]["digest"] == DG_A and rec["prev"]["applied_mode"] == "live"
    assert rec["prev"]["restart_policies"] == {c: "unless-stopped" for c in MANAGED}
    assert Path(rec["release_backup"]).read_text() == f"RELEASE_SHA={SHA_A}\nAPI_DIGEST={DG_A}\n"
    assert e.policies() == {c: "no" for c in MANAGED}
    assert rec["begin_boot_id"] == "boot-1"
    assert not (e.state / "transition.prep.json").exists()


def test_begin_rejected_while_record_open(e):
    assert e.begin().returncode == 0
    r = e.begin(run_id="r2")
    assert r.returncode == 10 and "끝나지 않은 기록" in r.stdout
    assert e.record()["run_id"] == "r1"


def test_begin_rejected_on_bad_release_changes_nothing(e):
    (e.conf / "release.conf").write_text("RELEASE_SHA=abc\n")
    r = e.begin()
    assert r.returncode == 10
    assert e.record() is None and e.policies() == {c: "unless-stopped" for c in MANAGED}


def test_begin_policy_inspect_failure_is_rejection(e):
    Path(str(e.docker_state) + ".broken").touch()
    r = e.begin()
    assert r.returncode == 10 and e.record() is None


def test_begin_update_failure_restores_and_reports_unchanged(e):
    r = e.begin(FAKE_DOCKER_FAIL_UPDATE="pixlate-api")
    assert r.returncode == 11 and "failed-unchanged" in r.stdout
    assert e.record() is None and e.policies() == {c: "unless-stopped" for c in MANAGED}
    assert not (e.state / "transition.prep.json").exists()


def test_begin_unverifiable_restore_is_unknown_and_keeps_prep(e):
    r = e.begin(FAKE_DOCKER_BREAK_ON_UPDATE="1")
    assert r.returncode == 12 and "unknown" in r.stdout
    assert e.record() is None and (e.state / "transition.prep.json").exists()


def test_begin_absent_container_is_recorded_not_failed(e):
    e.containers({"pixlate-api": {"policy": "unless-stopped", "status": "running", "image": f"x@{DG_A}"}})
    assert e.begin().returncode == 0
    assert e.record()["prev"]["restart_policies"] == {"pixlate-api": "unless-stopped", "pixlate-worker-cpu": "absent",
                                                      "pixlate-recovery": "absent"}


# ---- step · consume -------------------------------------------------------------------------------------------
def test_step_sequence_sandbox_end_to_end(e):
    assert e.begin().returncode == 0
    assert e.run("step", "--run-id", "r1", "--to", "validate").returncode == 10  # maintenance 전
    assert e.run("step", "--run-id", "r1", "--to", "maintenance").returncode == 10  # mark-stop 전
    e.to_maintenance()
    e.release(SHA_B, DG_B)
    assert e.run("step", "--run-id", "r1", "--to", "live").returncode == 10  # validate·isolated 건너뜀
    for s in ("validate", "isolated", "live"):
        r = e.run("step", "--run-id", "r1", "--to", s)
        assert r.returncode == 0, (s, r.stdout)
        assert e.record()["step"] == s and e.record()["request"]["consumed"] is True
    assert e.run("confirm-live", "--run-id", "r1").returncode == 0
    assert e.record()["state"] == "live-confirmed" and e.record()["request"] is None
    assert e.run("complete", "--run-id", "r1").returncode == 0
    assert e.record()["state"] == "complete"


def test_production_sequence_has_no_isolated(e):
    assert e.begin(sequence="production").returncode == 0
    e.to_maintenance()
    e.release(SHA_B, DG_B)
    assert e.run("step", "--run-id", "r1", "--to", "validate").returncode == 0
    assert e.run("step", "--run-id", "r1", "--to", "isolated").returncode == 10
    assert e.run("step", "--run-id", "r1", "--to", "live").returncode == 0


def test_step_does_not_hold_record_lock_while_applying(e):
    assert e.begin().returncode == 0
    e.to_maintenance()
    e.release(SHA_B, DG_B)
    assert e.run("step", "--run-id", "r1", "--to", "validate").returncode == 0
    assert set(e.probe.read_text().split()) == {"free"}  # pixlate-mode 가 불릴 때 L2 는 비어 있다(consume 이 잡을 수 있다)


def test_step_refuses_when_release_differs_from_target(e):
    assert e.begin().returncode == 0
    e.to_maintenance()  # release.conf 는 아직 이전(A)
    r = e.run("step", "--run-id", "r1", "--to", "validate")
    assert r.returncode == 10 and e.record()["request"] is None


def test_step_failure_keeps_step_and_records_failure(e):
    assert e.begin().returncode == 0
    e.to_maintenance()
    e.release(SHA_B, DG_B)
    r = e.run("step", "--run-id", "r1", "--to", "validate", FAKE_MODE_FAIL="validate")
    assert r.returncode == 13
    rec = e.record()
    assert rec["step"] == "maintenance" and rec["history"][-1]["event"] == "step-failed"


def test_step_other_run_id_rejected(e):
    assert e.begin().returncode == 0
    assert e.run("mark-stop", "--run-id", "other").returncode == 10


def _ready_request(e, step="validate", **req):
    assert e.begin().returncode == 0
    e.to_maintenance()
    e.release(SHA_B, DG_B)
    base = {"step": step, "sha": SHA_B, "digest": DG_B, "boot_id": "boot-1", "expires_at": int(time.time()) + 600, "consumed": False}
    e.patch(request={**base, **req})


def test_consume_table(e):
    assert e.consume("live").stdout.startswith("allow normal")  # 기록 없음 → 지금과 같은 동작
    _ready_request(e)
    assert e.consume("maintenance").returncode == 0  # 항상 허용
    assert e.consume("live").stdout.startswith("deny")  # 요청 단계와 모드 다름
    assert "release.conf" in e.consume("validate", sha=SHA_A, digest=DG_A).stdout
    e.boot.write_text("boot-2\n")
    assert "부팅 ID" in e.consume("validate").stdout
    e.boot.write_text("boot-1\n")
    r = e.consume("validate")
    assert r.returncode == 0 and r.stdout.startswith("allow request")
    r = e.consume("validate")  # 재사용
    assert r.returncode == 1 and "이미 사용한 요청" in r.stdout


def test_consume_expired_and_out_of_order(e):
    _ready_request(e, expires_at=int(time.time()) - 1)
    assert "만료" in e.consume("validate").stdout
    e.patch(request={"step": "isolated", "sha": SHA_B, "digest": DG_B, "boot_id": "boot-1",
                     "expires_at": int(time.time()) + 600, "consumed": False})
    assert "순서 위반" in e.consume("isolated").stdout  # 현재 maintenance — 다음은 validate


def test_consume_target_mismatch_and_states(e):
    _ready_request(e)
    e.patch(target={"sha": SHA_A, "digest": DG_A, "source": "previous"})
    assert e.consume("validate").returncode == 1  # 요청은 B 인데 대상은 A
    e.patch(state="abort-unknown")
    assert "abort-unknown" in e.consume("validate").stdout
    e.patch(state="live-confirmed", target={"sha": SHA_B, "digest": DG_B, "source": "candidate"})
    assert e.consume("live").stdout.startswith("allow live-confirmed")
    assert e.consume("validate").returncode == 1
    e.patch(recover_entry={"by": "x", "to": "previous", "started_at": "t"})
    assert e.consume("live").returncode == 1  # 복구 진입 표시 — 공개하지 않는다(방어 규칙)
    e.patch(state="complete", recover_entry=None)
    assert e.consume("isolated").stdout.startswith("allow normal")


def test_consume_corrupt_record_denies(e):
    (e.state / "transition.json").write_text("{not json")
    r = e.consume("live")
    assert r.returncode == 1 and r.stdout.startswith("deny")


def test_record_write_failure_keeps_previous_record(e, monkeypatch):
    assert e.begin().returncode == 0
    before = (e.state / "transition.json").read_text()
    mod = _load_tool(e, monkeypatch)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "replace", boom)
    assert mod.main(["mark-stop", "--run-id", "r1"]) == 12
    monkeypatch.undo()
    assert (e.state / "transition.json").read_text() == before
    assert not [p for p in e.state.iterdir() if p.name.startswith(".tmp-")]


def test_record_lock_timeout_is_rejection(e):
    import fcntl

    assert e.begin().returncode == 0
    fd = os.open(e.lock / "pixlate-transition.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = e.run("mark-stop", "--run-id", "r1", PIXLATE_LOCK_WAIT_S="0.3")
        assert r.returncode == 10 and "잠금" in r.stdout
        assert e.consume("validate").stdout.startswith("deny")
    finally:
        os.close(fd)
    assert e.record()["stop_started"] is False


# ---- abort ----------------------------------------------------------------------------------------------------
def test_abort_restores_and_confirms(e):
    assert e.begin().returncode == 0
    r = e.run("abort", "--run-id", "r1")
    assert r.returncode == 0, r.stdout
    assert e.record()["state"] == "aborted" and e.policies() == {c: "unless-stopped" for c in MANAGED}


def test_abort_refused_after_stop_started_or_reboot(e):
    assert e.begin().returncode == 0
    e.boot.write_text("boot-2\n")
    assert e.run("abort", "--run-id", "r1").returncode == 10  # 재부팅 뒤
    e.boot.write_text("boot-1\n")
    assert e.run("mark-stop", "--run-id", "r1").returncode == 0
    r = e.run("abort", "--run-id", "r1")
    assert r.returncode == 10 and "recover" in r.stdout
    assert e.record()["state"] == "incomplete"


def test_abort_unconfirmed_is_abort_unknown(e):
    assert e.begin().returncode == 0
    r = e.run("abort", "--run-id", "r1", PIXLATE_HEALTH_CMD="false")
    assert r.returncode == 12 and e.record()["state"] == "abort-unknown"


@pytest.mark.parametrize("code,curl_exit,ok", [("200", 0, True), ("302", 0, False), ("204", 0, False),
                                               ("200", 28, False), ("200", 18, False)])
def test_abort_default_health_needs_curl_ok_and_exactly_200(e, tmp_path, code, curl_exit, ok):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _script(bindir / "curl", f"#!/bin/sh\nprintf '%s' {code}\nexit {curl_exit}\n")
    assert e.begin().returncode == 0
    env = {k: v for k, v in e.env.items() if k != "PIXLATE_HEALTH_CMD"}
    env["PATH"] = f"{bindir}:{env['PATH']}"
    r = subprocess.run([sys.executable, str(TOOL), "abort", "--run-id", "r1"], env=env, capture_output=True, text=True)
    assert (r.returncode, e.record()["state"]) == ((0, "aborted") if ok else (12, "abort-unknown"))


def test_abort_with_coord_prev_needs_restore_command(e):
    assert e.begin(**{}).returncode == 0
    e.patch(coord_prev="applied:" + SHA_A)
    assert e.run("abort", "--run-id", "r1").returncode == 12
    assert e.record()["state"] == "abort-unknown"


def test_abort_runs_coord_restore(e, tmp_path):
    assert e.begin().returncode == 0
    e.patch(coord_prev="applied:" + SHA_A)
    out = tmp_path / "coord-args"
    restore = _script(tmp_path / "coord-restore", "#!/bin/sh\necho \"$@\" > " + str(out) + "\n")
    assert e.run("abort", "--run-id", "r1", PIXLATE_COORD_RESTORE=restore).returncode == 0
    assert out.read_text().split() == ["r1", "applied:" + SHA_A]


# ---- recover --------------------------------------------------------------------------------------------------
def test_recover_previous_after_failed_validate(e):
    assert e.begin().returncode == 0
    e.to_maintenance()
    e.release(SHA_B, DG_B)
    assert e.run("step", "--run-id", "r1", "--to", "validate", FAKE_MODE_FAIL="validate").returncode == 13
    assert e.run("recover-start", "--run-id", "r1", "--to", "previous", "--by", "ops").returncode == 0
    assert e.run("step", "--run-id", "r1", "--to", "maintenance").returncode == 0
    assert e.run("recover-target", "--run-id", "r1", "--to", "previous").returncode == 10  # release.conf 아직 후보
    e.release(SHA_A, DG_A)
    assert e.run("recover-target", "--run-id", "r1", "--to", "previous").returncode == 0
    assert e.record()["target"] == {"sha": SHA_A, "digest": DG_A, "source": "previous"}
    for s in ("validate", "isolated", "live"):
        assert e.run("step", "--run-id", "r1", "--to", s).returncode == 0, s
    assert e.run("confirm-live", "--run-id", "r1").returncode == 0
    assert e.record()["state"] == "live-confirmed"


def test_recover_start_after_reboot_before_stop_uses_kept_backup(e):
    assert e.begin().returncode == 0  # D3 직후
    e.boot.write_text("boot-2\n")     # 재부팅 — abort 불가
    assert e.run("abort", "--run-id", "r1").returncode == 10
    assert e.run("recover-start", "--run-id", "r1", "--to", "previous", "--by", "ops").returncode == 0
    rec = e.record()
    assert rec["stop_started"] is True and Path(rec["release_backup"]).exists()


def test_recover_start_from_abort_unknown_becomes_incomplete(e):
    assert e.begin().returncode == 0
    assert e.run("abort", "--run-id", "r1", PIXLATE_HEALTH_CMD="false").returncode == 12
    assert e.run("recover-start", "--run-id", "r1", "--to", "candidate", "--by", "ops").returncode == 0
    rec = e.record()
    assert rec["state"] == "incomplete" and rec["stop_started"] is True and rec["request"] is None
    assert rec["history"][-1]["prev_state"] == "abort-unknown"


def test_recover_start_without_backup_rejected(e):
    assert e.begin().returncode == 0
    Path(e.record()["release_backup"]).unlink()
    assert e.run("recover-start", "--run-id", "r1", "--to", "previous", "--by", "ops").returncode == 10
    assert e.record()["stop_started"] is False


def test_recover_target_only_after_maintenance(e):
    assert e.begin().returncode == 0
    assert e.run("recover-start", "--run-id", "r1", "--to", "previous", "--by", "ops").returncode == 0
    assert e.run("recover-target", "--run-id", "r1", "--to", "previous").returncode == 10  # step=precheck


def test_note_migration(e):
    assert e.begin().returncode == 0
    assert e.run("note-migration", "--run-id", "r1", "--from-rev", "0008", "--to-rev", "0009").returncode == 0
    assert e.record()["migrated"] == {"from_rev": "0008", "to_rev": "0009"}


# ---- --from-live -----------------------------------------------------------------------------------------------
def _live_confirmed(e):
    assert e.begin().returncode == 0
    e.to_maintenance()
    e.release(SHA_B, DG_B)
    for s in ("validate", "isolated", "live"):
        assert e.run("step", "--run-id", "r1", "--to", s).returncode == 0
    assert e.run("confirm-live", "--run-id", "r1").returncode == 0
    e.containers({c: {"policy": "unless-stopped", "status": "running", "image": f"reg/pixlate-api@{DG_B}"} for c in MANAGED})


def test_from_live_requires_save_first(e):
    _live_confirmed(e)
    assert e.run("from-live-enter", "--run-id", "r1", "--to", "previous", "--by", "ops").returncode == 10
    assert e.record()["state"] == "live-confirmed"


def test_from_live_save_then_enter_is_atomic(e):
    _live_confirmed(e)
    assert e.run("from-live-save", "--run-id", "r1", "--by", "ops").returncode == 0
    rec = e.record()
    assert rec["state"] == "live-confirmed" and rec["recover_prev_policies"] == {c: "unless-stopped" for c in MANAGED}
    assert e.run("from-live-enter", "--run-id", "r1", "--to", "previous", "--by", "ops").returncode == 0
    rec = e.record()
    assert e.policies() == {c: "no" for c in MANAGED}
    assert rec["state"] == "incomplete" and rec["stop_started"] is True and rec["recover_entry"]["to"] == "previous"
    assert e.consume("live").returncode == 1  # 부팅 공개 차단


def test_from_live_enter_policy_failure_is_unknown_and_not_entered(e):
    _live_confirmed(e)
    assert e.run("from-live-save", "--run-id", "r1", "--by", "ops").returncode == 0
    r = e.run("from-live-enter", "--run-id", "r1", "--to", "previous", "--by", "ops", FAKE_DOCKER_FAIL_UPDATE="pixlate-api")
    assert r.returncode == 12 and "확보되지 않았다" in r.stdout
    rec = e.record()
    assert rec["state"] == "live-confirmed" and rec["recover_entry"] is None


def test_confirm_live_clears_recover_entry(e):
    _live_confirmed(e)
    assert e.run("from-live-save", "--run-id", "r1", "--by", "ops").returncode == 0
    assert e.run("from-live-enter", "--run-id", "r1", "--to", "candidate", "--by", "ops").returncode == 0
    assert e.run("step", "--run-id", "r1", "--to", "maintenance").returncode == 0
    assert e.run("recover-target", "--run-id", "r1", "--to", "candidate").returncode == 0
    for s in ("validate", "isolated", "live"):
        assert e.run("step", "--run-id", "r1", "--to", s).returncode == 0, s
    assert e.run("confirm-live", "--run-id", "r1").returncode == 0
    rec = e.record()
    assert rec["state"] == "live-confirmed" and rec["recover_entry"] is None
    assert rec["history"][-1]["recover_entry_cleared"]["to"] == "candidate"
    assert e.consume("live").stdout.startswith("allow live-confirmed")  # 정상 복구 뒤 재부팅은 live


# ---- resolve · complete ---------------------------------------------------------------------------------------
def _resolve(e):
    return e.run("resolve", "--run-id", "r1", "--by", "ops", "--reason", "x")


def _maintenance_stopped(e, status="exited"):
    (e.conf / "deploy.mode").write_text("maintenance\n")
    (e.conf / "deploy.mode.applied").write_text("maintenance\n")
    e.containers({c: {"policy": "no", "status": status, "image": f"reg/pixlate-api@{DG_A}"} for c in MANAGED})


def test_resolve_only_when_maintenance_and_stopped(e):
    assert e.begin().returncode == 0
    assert _resolve(e).returncode == 10  # 모드 live
    (e.conf / "deploy.mode").write_text("maintenance\n")
    (e.conf / "deploy.mode.applied").write_text("maintenance\n")
    r = _resolve(e)  # 모드는 maintenance 지만 컨테이너가 아직 running
    assert r.returncode == 10 and "정지 상태가 아니다" in r.stdout
    _maintenance_stopped(e)
    assert _resolve(e).returncode == 0
    assert e.record()["state"] == "resolved"
    assert e.begin(run_id="r2").returncode == 0  # 닫힌 뒤 새 실행


def test_resolve_refuses_abort_unknown_while_service_runs(e):
    assert e.begin().returncode == 0
    assert e.run("abort", "--run-id", "r1", PIXLATE_HEALTH_CMD="false").returncode == 12  # 서비스는 계속 live 로 실행 중
    assert e.record()["state"] == "abort-unknown"
    r = _resolve(e)
    assert r.returncode == 10 and e.record()["state"] == "abort-unknown"


def test_resolve_refuses_live_incomplete(e):
    assert e.begin().returncode == 0
    _maintenance_stopped(e)
    (e.conf / "deploy.mode").write_text("live\n")
    (e.conf / "deploy.mode.applied").write_text("live-incomplete\n")
    assert _resolve(e).returncode == 10


def test_resolve_refuses_when_stop_unverifiable(e):
    assert e.begin().returncode == 0
    _maintenance_stopped(e)
    Path(str(e.docker_state) + ".broken").touch()
    r = _resolve(e)
    assert r.returncode == 10 and "조회 실패" in r.stdout


def test_resolve_live_confirmed_requires_stop_too(e):
    _live_confirmed(e)
    assert _resolve(e).returncode == 10
    _maintenance_stopped(e)
    assert _resolve(e).returncode == 0


def _load_tool(e, monkeypatch):
    for k, v in e.env.items():
        monkeypatch.setenv(k, v)
    loader = importlib.machinery.SourceFileLoader(f"pixlate_transition_{id(e)}", str(TOOL))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


def test_begin_record_replaced_but_not_durable_is_unknown_and_keeps_no(e, monkeypatch):
    """rename 뒤 디렉터리 fsync 실패 — 미완료 기록이 이미 보이므로 정책을 되돌리지 않는다(되돌리면 미완료 + unless-stopped)."""
    mod = _load_tool(e, monkeypatch)
    calls = []

    def flaky(d):
        calls.append(d)
        if len(calls) == 2:  # 1번째 = 준비 파일, 2번째 = 전환 기록
            raise OSError("fsync failed")

    monkeypatch.setattr(mod, "fsync_dir", flaky)
    rc = mod.main(["begin", "--run-id", "r1", "--kind", "deploy", "--sequence", "sandbox", "--sha", SHA_B, "--digest", DG_B])
    monkeypatch.undo()
    assert rc == 12
    assert e.record()["state"] == "incomplete"
    assert e.policies() == {c: "no" for c in MANAGED}
    assert (e.state / "transition.prep.json").exists()


def test_begin_record_write_before_replace_restores(e, monkeypatch):
    mod = _load_tool(e, monkeypatch)
    real = mod.os.replace

    def fail_record(src, dst):
        if str(dst).endswith("transition.json"):
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(mod.os, "replace", fail_record)
    rc = mod.main(["begin", "--run-id", "r1", "--kind", "deploy", "--sequence", "sandbox", "--sha", SHA_B, "--digest", DG_B])
    monkeypatch.undo()
    assert rc == 11  # 기록이 바뀌지 않았으므로 정책을 되돌리고 확인 → 원상태
    assert e.record() is None and e.policies() == {c: "unless-stopped" for c in MANAGED}


def test_complete_only_from_live_confirmed(e):
    assert e.begin().returncode == 0
    assert e.run("complete", "--run-id", "r1").returncode == 10
    assert e.run("confirm-live", "--run-id", "r1").returncode == 10  # step≠live


def test_show_prints_no_secret_fields(e):
    assert e.begin().returncode == 0
    out = json.loads(e.run("show").stdout)
    assert out["run_id"] == "r1" and out["state"] == "incomplete"
    assert e.run("show", "--field", "step").stdout.strip() == "precheck"
