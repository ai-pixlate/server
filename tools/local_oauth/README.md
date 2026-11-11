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
