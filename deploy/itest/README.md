# 운영 통합 검증 스크립트 (2026-10-13 기록용)

2026-10-13 운영 환경(EC2 API·cpu 워커·복구 + 학교 GPU ocr·gpu 워커)에서 합성 입력 1건으로 N2→N6 처리 경로를 검증할 때 쓴
감시·판정 스크립트다. 결과는 [`../integration-verification-2026-10-13.md`](../integration-verification-2026-10-13.md).

> **기록용이다.** 이번 검증의 값이 코드에 그대로 들어 있다(작업 9, 테스트 판매자 이메일, 섹션 10·11, 분석 run 22, 하류 run 37 등).
> 다른 작업에 다시 쓰려면 이 값들을 인자로 바꾸고 모의시험을 다시 돌려야 한다. 비밀 값·주소는 들어 있지 않다.

## 파일

| 파일 | 역할 |
|---|---|
| `driver_n2.py` | N2(analyze·judge) → N3 감시. 작업 9 에 analyze 1회 |
| `driver_n4.py` | N4(label·logo·inpaint·style·translate·preview) → N5 감시. `POST /v1/jobs/9/sections/proceed` 1회 |
| `driver_n6.py` | N6 감시. `POST /v1/jobs/9/confirm` 1회 → 최종 렌더 |
| `n5_verify.py` | N5 단계별 판정(성공·부분 검증·실패, 산출물 무결성, LaMa 실제 추론, 스타일 계약·렌더 가능) |
| `n6_verify.py` | N6 처리 판정(처리 성공·렌더 실패 보존·무결성 실패·미확정)과 품질 근거(블록 overflow) 분리 |
| `watch.sh` | 로컬 감시기 — EC2 컨테이너·학교 워커(PID·메모리) 감시, 비상 절차, driver 종료 뒤 DB 정리 확인 |
| `itest.sh` | EC2 실행기 — 배포 digest API 이미지 컨테이너에서 driver 를 `precheck`·`run`·`check`·`abort` 로 실행 |
| `test_*.py`, `test_watch.sh` | 모의시험(운영 접속 없음) |

## 실행 방법(기록)

EC2 에 driver 를 `driver.py` 이름으로 두고 실행한다(`itest.sh` 가 `driver.py` 를 마운트한다).

```
sudo bash /opt/pixlate/itest-<id>/itest.sh <증거 폴더> precheck <컨테이너 이름>   # 읽기 전용 사전 확인
sudo bash /opt/pixlate/itest-<id>/itest.sh <증거 폴더> run <컨테이너 이름>        # 사전 확인 → 감시기 준비 대기 → 요청 1회 → 감시
bash watch.sh <ssh_config> <증거 폴더> <로컬 로그> <컨테이너 이름> /opt/pixlate/itest-<id>   # driver 를 띄운 뒤 붙인다
```

## 안전장치

- **요청 1회**: 실행 요청(analyze·proceed·confirm)과 `/abort` 는 표시 파일을 `O_CREAT|O_EXCL` 로 만들어 한 번만 보낸다.
  표시 파일은 '요청 시도권을 사용함'이며 성공을 뜻하지 않는다. 결과는 별도 파일에 남긴다. `confirm` 응답이 유실돼도 다시 보내지 않고
  DB 로 반영 여부를 확인한다.
- **감시 준비 후 발행**: 감시기가 기준 컨테이너·학교 워커 PID 를 확인해 `WATCHER_READY` 를 남긴 뒤에만 요청을 보낸다.
- **판정**: 시도·인계·DB 시각은 REPEATABLE READ 읽기 전용 한 트랜잭션에서 읽는다. 실행 권한 판정(`lease_state`)은 복구 코드와 같다 —
  수신·검증 인계(`received`·`verifying`·`verified`)가 있으면 권한 유효 여부와 상관없이 채택 단계로 보고(계산 제한 미적용, 채택 지연은
  인계 수신 시각부터), 인계가 없을 때만 계산 권한 만료로 본다. 중단 직전에 한 번 더 조회해 다시 판정한다.
- **조회 실패는 0건이 아니다**: 판정하지 않고, 연속 실패면 감시 불능으로 처리한다.
- **N5·N6**: N5·N6 에는 `/abort` 가 없다. N5 도달·감시 종료와 검증 판정은 분리하고, N6 은 이상이 생기면 추가 요청 없이 관찰·보존한다.
- **비상 절차(watch.sh)**: 메모리 20GiB 초과·통제 불가 시 학교 워커 TERM(확인된 PID 에만)과 EC2 `pixlate-mode maintenance` 를 먼저
  모두 요청하고 대기는 분리한다. SIGKILL·자동 재시도·삭제는 하지 않는다.
- **출력**: 토큰·서명 URL·키·프롬프트·응답 본문은 기록하지 않는다. 증거 폴더는 EC2 `/var/lib/pixlate-itest/*`(root 700)이며 git 에 넣지 않는다.

## 모의시험

```
python -I test_driver_n2.py   # 24
python -I test_n4.py          # 46
python -I test_n6.py          # 28
bash test_watch.sh            # 21
```

실행기는 출력을 UTF-8 로 고정하고, 어떤 예외든 FAIL 로 세며, `RESULT ran=… pass=… fail=…` 로 실행 수를 함께 보고한다
(`python -I` 는 `PYTHONIOENCODING` 을 무시하므로 파일로 출력할 때 인코딩 오류로 집계가 빠지는 문제가 있었다).
판정 로직을 일부러 망가뜨린 변형(N2 4종·N4 12종·N6 7종)은 모두 이 시험에서 검출됐다.
