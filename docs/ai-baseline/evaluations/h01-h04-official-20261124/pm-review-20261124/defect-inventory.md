# PM 반영 결함 목록

동일 root의 양 체크포인트 관찰은 한 행으로 표시. 최초 이력·개정 이력은 별도 보존.

| root ID | 심각도 | 원인 단계 | 관찰 |
|---|---|---|---|
| GS-01-IMG-002-volume | critical | OCR | 원본 50ml가 50m로 바뀜. text_block 41의 source_lines와 source_ko에 이미 50m. 번역·렌더까지 전파됨. |
| GS-01-IMG-002-layout | major | undetermined | 기존 제목·본문·배지·라인업 겹침과 비건 설명 잘림 유지. 수치 배지 15.92%/6.78% 판독 실패는 별도 critical로 분리해 중복 가산하지 않음. PM 추가: K01 Protection Factor가 SPF/PA 값 칸에 겹침(값은 읽힘); K02 Sunscreen과 To support healthy marine ecosystems 겹침; K03 goodal/Good for All of Us, Continuous Commitment/설명, Free of 18 Suspicious Ingredients/설명·각주 겹침; 목록 8) o-Cymen-5-ol·9) Sorbic Acid가 02. Plasticizers에 가려 판독 불가; sulfate surfactants 각주와 04. Sunscreen Agents 겹침. |
| GS-01-IMG-002-claim | critical | 번역·현지화 | Those looking for a sunscreen with verified broad spectrum SPF: 원문 자외선차단지수 검증에 없는 broad spectrum 추가. |
| GS-01-IMG-002-protected | major | undetermined | FSC/SOYINK 마크 글자와 Good all goodal 타원 로고가 재작성·재배치됨. |
| GS-01-IMG-002-escape | minor | undetermined | 해시태그 인접 문구에 이스케이프 문자열 \n이 노출됨. |
| GS-01-IMG-002-badge-020 | critical | undetermined | 15.92%가 두 원으로 갈라진 배지에 가려 1 · 92만 남아 판독 불가. |
| GS-01-IMG-002-badge-025 | critical | undetermined | 6.78%가 같은 모양의 배지에 가려 6. · %만 남아 판독 불가. |
| GS-01-IMG-002-irritation | major | 번역·현지화 | 저자극 순한 선크림이 gentle, hypoallergenic sunscreen으로 번역됨(block 188). |
| GS-02-IMG-003-layout | major | undetermined | 큰 제목 #1 Amazon Best의 Seller가 이미지 밖으로 잘림. Performance/제품명 겹침의 판독 실패는 별도 name-readability critical로 분리. 기존 major 심각도 유지. |
| GS-02-IMG-003-name-readability | critical | undetermined | Performance가 Retinal 위에 겹쳐 원본 없이 제품명을 확정할 수 없음. |
| GS-02-IMG-004-layout | major | undetermined | 거대 제목이 해시태그·피부 사진에 침범, 후기 말풍선과 인용문 겹침. 흡수 깊이 제목 하단 일부 잘림. |
| GS-02-IMG-004-replacement | critical | 번역·현지화 | The fine lines seem to have almost completely disappeared...가 원본 첫 후기의 번역으로 남음. |
| GS-02-IMG-005-layout | major | undetermined | 0.00% 개선 배지의 제목·비교 대상·개선 문구가 겹치고 오른쪽 밖으로 잘림. 숫자는 판독 가능. |
| GS-02-IMG-005-graph | major | undetermined | 3D 그래프의 BEFORE 표제가 없어지고 눈금 일부가 다시 그려짐; AFTER는 남음. |
| GS-02-IMG-006-layout | major | undetermined | 큰 표제와 네 효능 타원이 겹침; 892% 아래 제목·설명 겹침. |
| GS-02-IMG-006-microscope | major | undetermined | 현미경 사진 위에 원본에 없는 큰 회색 C가 생김. |
| GS-02-IMG-007-layout | major | undetermined | 효능 제목·본문 및 얼굴의 각질층/눈가 라벨이 겹침. 800만 제목과 상품명도 겹침. 숫자·각주를 별도로 H04 대조함. |
| GS-02-IMG-008-layout | major | undetermined | 제목·수치 설명이 카드 밖으로 넘침. 원료적 특성 한정 각주 일부 잘림·제목과 겹침. |
| GS-02-IMG-008-papers | major | undetermined | 논문 사진 속 영문을 새로 그려 논문 표제·본문이 서로 겹치고 원래 문서의 가독성을 훼손함. |
| GS-02-IMG-009-layout | major | undetermined | 후기 작성자·표제·시험 배지 글자가 칸과 다른 문구를 침범. 얼굴 부위의 검은/회색 라벨 대비가 낮음. |
| GS-02-IMG-009-badges | major | undetermined | 시험 배지 여러 개의 한국어가 남은 가운데 큰 Skin lifting improvement Test completed만 겹쳐 표시됨. |
| GS-02-IMG-010-layout | major | undetermined | 사진 라벨과 큰 제목이 구획을 벗어나고 만족도 설명과 각주가 겹침. |
| GS-02-IMG-010-escape | minor | undetermined | 만족도 문구에 \n**가 그대로 노출됨. |
| GS-02-IMG-011-layout | major | undetermined | 큰 제목이 이마 주름 항목으로 침범하고 시험 각주가 제목에 겹침. 값과 전후 기간은 판독 가능. |
| GS-02-IMG-013-layout | major | undetermined | 시험 인증서 표제와 결론 글자가 표 경계를 벗어나고 도장·본문이 겹침. |
| GS-02-IMG-013-irritation | major | 번역·현지화 | 일반 피부 일차자극 시험 결론의 저자극성을 hypoallergenic으로 옮김. 아래 민감성 시험은 low (non-)irritating으로 보존. |
| GS-02-IMG-014-usage-layout | critical | 렌더·배치 | 초급자 첫 달 주 1–2회와 증가 주 3–4회가 겹침. 중급자 주 5–6회가 고급자 Daily와 겹쳐 용법 대응 판독 곤란. 원문·번역의 빈도 텍스트는 보존됨. |
| GS-02-IMG-014-bean | critical | 번역·현지화 | 콩알만한 크기가 size of Glycine Max (Soybean) Pulp로 변해 사용량 비교물이 달라짐. |
| GS-02-IMG-014-layout | major | undetermined | 병용 제품 제목에 여분 1이 붙고 일부 제목이 넘침. 개발 이야기 표제에 \n 문자열 노출. |
| GS-02-IMG-014-sketch | major | undetermined | 개발 스케치 사진 안 the vita A / retinal / tightening 손글씨가 크게 재작성되어 잘림. |
| GS-02-IMG-014-rice-name | minor | 번역·현지화 | 쌀알 크기를 size of an Oryza Sativa (Rice) Kernel로 표현. 의미는 맞으나 사용법에 불필요한 학명. |
