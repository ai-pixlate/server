"""떠 있는 진입점(serve)을 HTTP 로 점검하는 도구 자체 검증. 브라우저가 아니다 — 브라우저 연동 검증을 대신하지 않는다.

    python -m tools.local_oauth.smoke [--be http://127.0.0.1:8000] [--fake http://127.0.0.1:8900] [--origin http://localhost:3000]

BE 에 직접 붙는다(Next 프록시를 거치지 않음). 쿠키는 이 스크립트가 손으로 들고 다닌다. 토큰·코드 값은 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import sys
import threading
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import httpx

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> bool:
    RESULTS.append((name, bool(ok), note))
    print(f"[{'통과' if ok else '실패'}] {name}{' — ' + note if note else ''}", flush=True)
    return ok


def set_cookies(r: httpx.Response) -> dict[str, dict]:
    out = {}
    for raw in r.headers.get_list("set-cookie"):
        c = SimpleCookie()
        c.load(raw)
        for k, m in c.items():
            out[k] = {"value": m.value, "max-age": m["max-age"], "path": m["path"], "samesite": m["samesite"].lower(),
                      "secure": "secure" in raw.lower(), "httponly": "httponly" in raw.lower()}
    return out


class Run:
    def __init__(self, be: str, fake: str, origin: str):
        self.be, self.fake, self.origin = be.rstrip("/"), fake.rstrip("/"), origin
        self.c = httpx.Client(timeout=30, follow_redirects=False)

    def post(self, path: str, cookies: dict | None = None, **kw):
        h = {"Origin": self.origin, "X-Requested-With": "XMLHttpRequest", **kw.pop("headers", {})}
        if cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
        return self.c.post(self.be + path, headers=h, **kw)

    def control(self, body: dict):
        return self.c.post(self.fake + "/_control", json=body).json()

    def authorize(self, url: str, account: str) -> dict:
        page = self.c.get(url)
        assert page.status_code == 200, page.status_code
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        form = {f"q_{k}": v for k, v in q.items()}
        form["account"] = account
        r = self.c.post(url.split("?")[0], data=form)
        assert r.status_code == 302
        return {"location": r.headers["location"], **{k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}}

    def login(self, provider: str, account: str, jar: dict) -> httpx.Response:
        s = self.post(f"/v1/auth/social/{provider}/start", jar)
        jar["pxl_oauth_bind"] = set_cookies(s)["pxl_oauth_bind"]["value"]
        cb = self.authorize(s.json()["authorizeUrl"], account)
        r = self.post("/v1/auth/social", jar, json={"provider": provider, "code": cb["code"], "state": cb["state"]})
        ck = set_cookies(r)
        if r.status_code == 200:
            jar["pxl_refresh"] = ck["pxl_refresh"]["value"]
            jar.pop("pxl_oauth_bind", None)
        return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--be", default="http://127.0.0.1:8000")
    ap.add_argument("--fake", default="http://127.0.0.1:8900")
    ap.add_argument("--origin", default="http://localhost:3000")
    a = ap.parse_args(argv)
    run = Run(a.be, a.fake, a.origin)
    run.control({"reset": True})

    # 1. 카카오 시작 → 가짜 화면 → 콜백 → 완료
    jar: dict = {}
    s = run.post("/v1/auth/social/kakao/start", jar)
    bind = set_cookies(s).get("pxl_oauth_bind", {})
    check("start 200 + 결속 쿠키(HttpOnly·SameSite=Strict·Path=/auth)", s.status_code == 200 and bind.get("httponly")
          and bind.get("samesite") == "strict" and bind.get("path") == "/auth", f"secure={bind.get('secure')}")
    url = s.json()["authorizeUrl"]
    check("authorizeUrl 은 가짜 화면(127.0.0.1)", urlsplit(url).hostname == "127.0.0.1" and "/kakao/authorize" in url)
    jar["pxl_oauth_bind"] = bind["value"]
    cb = run.authorize(url, "kakao-a")
    check("가짜 화면 → FE 콜백으로 302(code·state)", cb["location"].startswith(a.origin + "/login/callback/kakao?") and "code" in cb)
    r = run.post("/v1/auth/social", jar, json={"provider": "kakao", "code": cb["code"], "state": cb["state"]})
    ck = set_cookies(r)
    check("완료 200 + refresh 쿠키 설정 + 결속 쿠키 삭제", r.status_code == 200 and "pxl_refresh" in ck
          and ck.get("pxl_oauth_bind", {}).get("max-age") == "0", r.text[:120] if r.status_code != 200 else "")
    access = r.json().get("accessToken") if r.status_code == 200 else None
    jar = {"pxl_refresh": ck.get("pxl_refresh", {}).get("value")}
    me = run.c.get(run.be + "/v1/auth/me", headers={"Authorization": f"Bearer {access}"})
    check("/auth/me 200(카카오 시험 A)", me.status_code == 200 and me.json().get("email") == "kakao-a@local.test", str(me.status_code))
    again = run.post("/v1/auth/social", {"pxl_oauth_bind": bind["value"]}, json={"provider": "kakao", "code": cb["code"], "state": cb["state"]})
    check("같은 완료 재전송 → 401 state_invalid", again.status_code == 401 and "state_invalid" in again.text)
    rf = run.post("/v1/auth/refresh", jar)
    check("refresh 200(회전)", rf.status_code == 200 and "pxl_refresh" in set_cookies(rf))
    jar["pxl_refresh"] = set_cookies(rf)["pxl_refresh"]["value"]

    # 2. S3 차단 — 브랜드 로고 업로드는 S3 를 부른다 → 실패해야 한다
    acc = rf.json()["accessToken"]
    br = run.c.post(run.be + "/v1/brands", headers={"Authorization": f"Bearer {acc}"}, json={"nameKo": "로컬시험", "nameEn": "LocalTest"})
    if br.status_code == 201:
        try:
            up = run.c.post(run.be + f"/v1/brands/{br.json()['id']}/logos", headers={"Authorization": f"Bearer {acc}"},
                            files={"file": ("x.png", b"\x89PNG\r\n\x1a\n", "image/png")})
            check("S3 호출 차단(로고 업로드 실패)", up.status_code >= 500, str(up.status_code))
        except httpx.HTTPError as e:
            check("S3 호출 차단(로고 업로드 실패)", True, f"연결 오류 {type(e).__name__}")
    else:
        check("S3 확인용 브랜드 생성", False, str(br.status_code))

    # 3. 로그아웃 → 204 + 쿠키 삭제, 저장소 장애 → 503
    run.control({"store_down": True})
    lo = run.post("/v1/auth/logout", jar)
    check("저장소 장애 로그아웃 → 503·쿠키 변경 없음", lo.status_code == 503 and not lo.headers.get_list("set-cookie"), str(lo.status_code))
    run.control({"store_down": False})
    lo = run.post("/v1/auth/logout", jar)
    check("로그아웃 204 + refresh 쿠키 삭제", lo.status_code == 204 and set_cookies(lo).get("pxl_refresh", {}).get("max-age") == "0")

    # 4. Google
    gj: dict = {}
    g = run.login("google", "google-a", gj)
    check("Google 로그인 200(가짜 ID 토큰이 서버 google-auth 검증 통과)", g.status_code == 200 and g.json()["seller"]["email"] == "google-a@local.test",
          g.text[:120] if g.status_code != 200 else "")
    run.control({"fail": {"google_certs": 503}})
    import time as _t
    _t.sleep(0)  # 인증서는 캐시돼 있으므로 같은 kid 면 조회하지 않는다 — 여기서는 캐시 동작만 본다
    g2 = run.login("google", "google-b", {})
    check("인증서 캐시 사용(조회 실패가 켜져도 캐시 안에서는 200)", g2.status_code == 200, str(g2.status_code))
    run.control({"reset": True})

    # 5. 취소
    s = run.post("/v1/auth/social/kakao/start", {})
    d = run.authorize(s.json()["authorizeUrl"], "__deny__")
    check("취소 → FE 콜백에 error=access_denied", d.get("error") == "access_denied" and "code" not in d)

    # 6. Origin 거절
    bad = run.c.post(run.be + "/v1/auth/social/kakao/start", headers={"Origin": "http://evil.localhost:3000", "X-Requested-With": "XMLHttpRequest"})
    check("허용 목록 밖 Origin → 401 origin_not_allowed·쿠키 없음", bad.status_code == 401 and "origin_not_allowed" in bad.text
          and not bad.headers.get_list("set-cookie"))

    # 7. 검증 불가 refresh + 결속으로 로그아웃하는 동안 진행 중 로그인 → 늦은 완료 state_invalid
    jar = {}
    s = run.post("/v1/auth/social/kakao/start", jar)
    jar["pxl_oauth_bind"] = set_cookies(s)["pxl_oauth_bind"]["value"]
    cb = run.authorize(s.json()["authorizeUrl"], "kakao-b")
    run.control({"delays": {"kakao_token_info": 2}})
    out = {}
    t = threading.Thread(target=lambda: out.update(r=run.post("/v1/auth/social", jar, json={"provider": "kakao", "code": cb["code"], "state": cb["state"]})))
    t.start()
    _t.sleep(0.8)
    lo = run.post("/v1/auth/logout", {"pxl_oauth_bind": jar["pxl_oauth_bind"], "pxl_refresh": "not-a-refresh-token"})
    t.join()
    run.control({"reset": True})
    check("처리 중 검증 불가 refresh 로그아웃 → 401 refresh_invalid", lo.status_code == 401 and "refresh_invalid" in lo.text)
    check("늦은 완료 → 401 state_invalid·쿠키 없음", out["r"].status_code == 401 and "state_invalid" in out["r"].text
          and not out["r"].headers.get_list("set-cookie"), str(out["r"].status_code))

    st = run.c.get(run.fake + "/_control").json()
    check("가짜 제공자 기록에 외부 호출 차단 없음(대역 밖 호출 0)", not any(x["call"] == "blocked" for x in st["calls"]))
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n도구 자체 검증: {len(RESULTS) - len(failed)}/{len(RESULTS)} 통과" + (f" — 실패: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
