# 로컬 브라우저 시험 전용 BE + 가짜 OAuth 제공자

실제 카카오·Google·S3를 부르지 않고, 로컬 FE(`next dev`)와 BE를 실제 브라우저로 붙여 보는 도구다. **운영 이미지에는 들어가지 않는다**(`.dockerignore`의 `tools/local_oauth`). `app/`은 이 패키지를 import하지 않는다.

## 실행
1. `local.env.example`을 복사해 DB 비밀번호만 채우고 셸에 export한다(모두 시험 값 — 실제 키를 넣지 않는다).
2. `python -m tools.local_oauth.serve --local-fake-providers --init-db` (첫 실행만 `--init-db` — `pixlate_local_…` DB 생성·마이그레이션)
3. 도구 점검(브라우저 아님): `python -m tools.local_oauth.smoke`

## 시작 조건(하나라도 아니면 시작하지 않음)
- 실행 인자 `--local-fake-providers`
- 켠 제공자의 클라이언트 ID·시크릿, `JWT_SECRET`이 `local-fake-` 접두사(시험 값)
- `DATABASE_URL`은 루프백 + DB 이름 `pixlate_local_…`. `PIXLATE_AUTH_REDIS_URL`·`CELERY_BROKER_URL`·`CELERY_RESULT_BACKEND`는 루프백으로 명시
- `PIXLATE_OAUTH_REDIRECT_BASE`·`PIXLATE_AUTH_ORIGINS`는 `http://localhost:<포트>`(또는 127.0.0.1)만
- 포트가 비어 있어야 한다(다른 프로세스를 멈추지 않는다)

## 하는 일
- BE `127.0.0.1:<port>`(기본 8000), 가짜 로그인 화면 `127.0.0.1:<fake-port>`(기본 8900). 대기 주소는 127.0.0.1 고정이다.
- 서버의 제공자 HTTP(토큰·사용자 정보·Google v1 인증서)는 프로세스 안 대역이 받는다. 대역에 없는 주소는 실패한다.
- S3 클라이언트를 차단 대역으로 바꾸고 AWS 자격 환경 변수를 지운다.
- 조작: `POST http://127.0.0.1:8900/_control`
  - `{"delays": {"kakao_token_info": 5}}` — 제공자 응답 지연(경합)
  - `{"fail": {"google_certs": 503}}` — 제공자 실패
  - `{"store_down": true}` — BE 세션 저장소 장애
  - `{"reset": true}` — 모두 해제
  - `GET /_control` — 현재 조작과 호출 기록(값 없음)

## 시험 계정
카카오: 시험 A·B(이메일 확인), C(이메일 동의 안 함). Google: 시험 A·B(이메일 확인), C(미확인). 화면의 「취소」는 `error=access_denied`로 돌려보낸다.

## 한계
제공자 동작은 문서와 시험 대역 기준의 흉내다. 실제 제공자 동작(PKCE 강제 여부, 코드 유효 시간, 오류 코드 실물, 실제 Google 토큰)의 근거가 아니다.

## 실제 제공자 로컬 시험 — `real`
`python -m tools.local_oauth.real --real-providers-local [--port 8000]` — 가짜 제공자 없이 BE를 127.0.0.1에 띄운다(실제 콘솔 클라이언트 값은 셸 환경 변수로만).

막는 범위는 두 가지뿐이다. **프로세스의 모든 외부 통신을 막지는 않는다.**
- OAuth 제공자 HTTP 전송: https이고 허용 호스트 4개(`kauth.kakao.com`·`kapi.kakao.com`·`oauth2.googleapis.com`·`www.googleapis.com`)일 때만 보내고, 나머지는 보내지 않고 실패
- S3: 클라이언트 차단(`app.s3._s3` 대역), 자격·엔드포인트 격리(AWS 자격 환경 변수 삭제, 자격·설정 파일 비움, 메타데이터 조회 끔, `AWS_ENDPOINT_URL_S3=http://127.0.0.1:9`)
- DB·Redis는 시작 조건(루프백)으로만 제한한다.

시작 조건: 실행 인자, 실제 클라이언트 ID(`local-fake-`면 거절), AWS 자격 환경 변수 없음, 루프백 DB(`pixlate_local_…`)·Redis, `http://localhost` 오리진.

### 값이 남지 않게 실행하기
- BE: 접근 로그를 끈다(진입점 기본). app 로그에는 코드·토큰·state가 없다.
- **FE `next dev` 출력에는 콜백 주소(`code`·`state`)가 찍힌다.** 실제 제공자 시험에서는 출력을 저장하지 않는다: `next dev … > NUL 2>&1`(Git Bash는 `> /dev/null 2>&1`). Next 자체 로그 파일(`.next/dev/logs/next-development.log`)에는 요청 주소가 남지 않는 것을 11-11에 확인했지만, 시험 뒤 그 폴더를 지운다.
- 오류를 보려고 출력을 꼭 받아야 하면 저장소 밖 임시 폴더(작업 세션 scratchpad)에만 받고, 그 시험 단계가 끝나는 즉시 지운다. 값은 보고서·커밋에 넣지 않는다.
- 브라우저 네트워크 기록은 도구가 `code`·`state`를 가린다. 결과에는 필드 존재 여부·검증 결과·상태 코드만 적는다.
