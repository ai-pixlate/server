# pipeline/samples — 샘플 입력과 기대 결과

| 디렉터리 | 내용 | git |
|---|---|---|
| `synthetic_01/` | 합성 원본 1장(600×1000, 배경색 2구간) + 단계별 기대 JSON. `make_synthetic.py`로 재생성 | 포함 |
| `local/` | 실제 상세페이지 표본(한국어). 개인 PC·GPU 서버에만 둔다 | **제외** |

레이아웃은 아래 규칙을 따른다. `python -m pipeline.run` 출력 디렉터리도 같다.

```
<sample>/
  source.png                 원본
  expected/                  (또는 실행 출력 out/<name>/)
    split.json               ① SplitResult
    sections/<section_key>.png
    ocr/<section_key>.json   ② OcrResult
    merge/<section_key>.json ③ MergeResult
    analyze.json             analyze() 출력 (실행 시)
    run.json                 사용한 config·입력·프롬프트 해시 (실행 시)
```

확인 방법 — JSON은 그대로 읽고, 좌표는 오버레이로 본다. 레포 루트에서 실행한다.

```bash
S=pipeline/samples/synthetic_01
python -m pipeline.run inspect --split $S/expected/split.json --image $S/source.png --out pipeline/out/split.png
python -m pipeline.run inspect --ocr   $S/expected/ocr/sec_1_01.json   --image $S/expected/sections/sec_1_01.png --out pipeline/out/ocr.png
python -m pipeline.run inspect --merge $S/expected/merge/sec_1_01.json --image $S/expected/sections/sec_1_01.png --out pipeline/out/merge.png
```

`expected/`는 아직 **형식 예시**다. 단계가 구현되면 같은 입력의 실행 결과를 여기와 비교하는 회귀 테스트로 쓴다.
