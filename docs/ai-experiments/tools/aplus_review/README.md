# aplus_review — A+ 슬롯 내용 육안 검수 도구

A+ 템플릿 모듈(`pipeline/aplus/`, 개발용 v1)의 결과를 눈으로 보기 위한 **검수 도구**다. 운영 렌더가 아니며 파이프라인 의존성도 아니다(`docs/ai/open-questions.md` 2.9절 · #97).

## 구성

| 경로 | 내용 | 출처 |
|---|---|---|
| `gallery.py` | `AplusPlan` · `AplusModule` → 렌더러 입력 변환(이미지 칸은 원본 섹션에서 잘라 저장) · 970×300 PNG · 검수 화면(`index.html` — 원본 섹션 · 원문 · 한도 · 판단 기록) · `report.json` | 이 레포에서 작성 |
| `renderer/` | 입력 검사 · 배치 계산 · HTML 채우기 · 브라우저 캡처 · 글자 맞춤(`fit.js`) · 아이콘 | `ai-pixlate/seed-builder` PR #3 `aplus_renderer/renderer/`(커밋 `a889a0c`, 미병합) 복사. **`core.py`의 글꼴 경로만 수정**(아래) |
| `templates/` | 템플릿 14개 HTML · 공통 CSS · 매크로 | 같은 PR의 `aplus_renderer/templates/` 복사. **`_base.html`의 `@font-face`와 `base.css`의 `font-family`만 수정**(아래) |

글꼴 수정(2026-11-24, 사용자 결정): 사본에 있던 Noto Sans(`fonts/NotoSans-VF.ttf`)를 빼고 레포의 Pretendard(`pipeline/data/fonts/pretendard/` Regular · SemiBold)를 쓴다 — 최종 렌더는 FE가 하고 글꼴은 Pretendard다. 굵기 100~500은 Regular, 600~900은 SemiBold로 그린다. 그 밖의 렌더러 · 템플릿 코드는 사본 그대로다.

가져오지 않은 것: `fonts/`(위) · `spec/templates_spec.json`(치수는 `pipeline/data/aplus/spec.json`의 `regions`를 실행 때 렌더러 형식으로 내보내 쓴다 — 치수의 정본은 하나다) · `rhythm.py`(좌우 번갈아 규칙, 변환기 쪽 규칙이 대체) · `render.py` · `batch.py` · `make_gallery.py` · `make_samples.py` · `samples/`.

## 읽을 때 주의

- 최종 렌더는 FE가 한다. 이 도구는 글꼴만 조판 계약과 같은 Pretendard이고, 글이 칸을 넘으면 글자를 최소 80%까지 자동으로 줄이는 것은 조판 계약(`docs/ai/contract.md` 6.4 — 자동 축약/축소 없음)과 다르다. 템플릿 HTML · 줄바꿈도 FE 구현이 아니다. **이 이미지의 글자 크기 · 넘침은 최종 결과의 근거가 아니다.**
- 보는 것은 매핑(어느 섹션이 어느 칸 · 템플릿에 갔는지)과 슬롯 채우기(어느 블록이 어느 칸에 들어갔는지)다. 검수 화면의 `[검수 렌더]` 경고는 이 렌더러의 경고다.
- 글자 수 한도 초과는 규격 데이터 기준으로 따로 표시한다(빨간 숫자). 자르지 않는다.

## 실행

`jinja2` · `playwright`와 Chromium이 필요하다(`pip install jinja2 playwright` · `playwright install chromium`). 이 PC에서는 프로젝트 가상환경이 아닌 시스템 Python에 설치돼 있다.

```
python docs/ai-experiments/tools/aplus_review/gallery.py --plan <aplus_plan.json> --split <split.json> --out <새 폴더> [--blocks <원문.json>] [--cutout <aplus_cutout.json>] [--title 제목] [--no-inline]
```

- `--plan`: `pipeline.aplus.plan.run`의 결과(`AplusPlan` JSON, `status=ok`).
- `--blocks`: `{block_key: 원문}` JSON. 주면 문구 아래에 한국어 원문을 붙인다.
- `--cutout`: `pipeline.aplus.cutout.run`의 결과(`AplusCutoutResult` JSON). 주면 누끼에 성공한 이미지 칸은 배경을 지운 그림으로 그리고(전체가 보이게), 누끼 원천 · 확인 표시 · 실패를 칸 메모로 붙인다. 누끼를 하지 않았거나 실패한 칸은 원본 크롭으로 그린다.
- 기본은 `index.html`에 그림을 넣어 파일 하나로 열 수 있게 한다(`--no-inline`이면 파일 경로로 연결).
- 코드에서 직접 부를 때는 `render_gallery([Entry(...)], section_images, out_dir)`.

검수 화면: 칸마다 왼쪽에 원본 섹션(색 상자 = 사진을 자른 자리), 오른쪽에 렌더 · 고른 이유 · 코드가 바로잡은 점 · 문구별 원문 / 글자 수 / 한도 / 출처 블록. 칸마다 템플릿 · 문구 · 사진 판단과 메모를 적고 "판단 결과 CSV 내려받기"로 저장한다(판단은 그 브라우저에만 남는다).

출력: `index.html`(검수 화면) · `renders/*.png` · `content/*.json`(렌더러 입력) · `images/`(자른 사진) · `sources/`(원본 섹션 축소 그림) · `report.json` · `_renderer_spec/`(내보낸 치수 파일).

## 섹션 내용 라벨 확인 화면

섹션마다 그림 · 읽은 글 · 모델이 붙인 라벨(주 · 보조 · 확신도 · 이유 · 가는 칸)을 나란히 놓아 눈으로 보는 화면이다. 판정을 기록하지 않는다. Pillow만 쓴다.

```
python docs/ai-experiments/tools/aplus_review/label_review.py --out <새 폴더> [--title 제목] --product <이름>=<aplus_label.json>=<sections.json>[=<aplus_label_debug.json>] [--product ...]
```

- `aplus_label.json`: `pipeline.aplus.label.run`의 결과(`status=ok`). `sections.json`: 그 실행에 쓴 섹션 목록(`Section` JSON 배열).
- `aplus_label_debug.json`을 주면 모델에 보낸 섹션의 글을 함께 보여 준다.
- 화면: 상품 탭, 걸러 보기(보조 라벨 있음 · 확신 낮음 · 2,000px 넘음 · 7칸 밖 · 라벨별), 그림을 누르면 원본 크기, `j` · `k`로 다음 · 이전 섹션. 라벨 정의는 `pipeline/prompts/aplus_label.md`의 표에서 읽는다.

출력: `index.html` · `images/`(섹션 그림 JPEG).
