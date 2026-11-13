# 우선 경계 후보 82개 검토

원본 crop를 기준으로 조사. 포함·제외 판정은 문자열 편집이나 CER 계산이 아니다. 빈 출력은 GT 누락 면제 근거가 아니다.

| 섹션 | 출력 인덱스 | region_key | 판정 | 근거 |
|---|---:|---|---|---|
| GS-01_002__sec_1_01 | 11 | reg_0012 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-01_002__sec_1_06 | 0 | reg_0001 | undetermined | 넓은 오검출 영역이 얼굴/용기 경계를 덮음. o를 라벨로 자동 제외하면 삽입 오류 은폐 가능 |
| GS-01_002__sec_1_11 | 25 | reg_0026 | empty_no_text | 빈 출력; 범위 승인/GT 삭제 근거 아님 |
| GS-01_002__sec_1_11 | 26 | reg_0027 | empty_no_text | 빈 출력; 범위 승인/GT 삭제 근거 아님 |
| GS-01_002__sec_1_21 | 2 | reg_0003 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-01_002__sec_1_21 | 3 | reg_0004 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-01_002__sec_1_21 | 4 | reg_0005 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-01_002__sec_1_21 | 5 | reg_0006 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-01_002__sec_1_23 | 0 | reg_0001 | undetermined | Good all(일반 문구)+goodal(로고)이 하나의 OCR 반환 문자열. 임의 분할 금지 |
| GS-02_001__sec_1_02 | 2 | reg_0003 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_001__sec_1_02 | 3 | reg_0004 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_002__sec_1_01 | 5 | reg_0006 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_002__sec_1_01 | 6 | reg_0007 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_002__sec_1_01 | 7 | reg_0008 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_002__sec_1_01 | 8 | reg_0009 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_002__sec_1_01 | 10 | reg_0011 | undetermined | 판매랭킹 TOP.1 배지의 인증/일반 설명 구분 확인 필요 |
| GS-02_008__sec_1_01 | 12 | reg_0013 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_01 | 15 | reg_0016 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_01 | 18 | reg_0019 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 12 | reg_0013 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_008__sec_1_04 | 59 | reg_0060 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 77 | reg_0078 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 80 | reg_0081 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 86 | reg_0087 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 87 | reg_0088 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 90 | reg_0091 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 92 | reg_0093 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 93 | reg_0094 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 94 | reg_0095 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 96 | reg_0097 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 97 | reg_0098 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 101 | reg_0102 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 103 | reg_0104 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 105 | reg_0106 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 106 | reg_0107 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 118 | reg_0119 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 122 | reg_0123 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 125 | reg_0126 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 126 | reg_0127 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 128 | reg_0129 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 136 | reg_0137 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 137 | reg_0138 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 138 | reg_0139 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 140 | reg_0141 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 143 | reg_0144 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 145 | reg_0146 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 148 | reg_0149 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 149 | reg_0150 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 151 | reg_0152 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 154 | reg_0155 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 157 | reg_0158 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 159 | reg_0160 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_008__sec_1_04 | 173 | reg_0174 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_014__sec_1_05 | 7 | reg_0008 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_014__sec_1_05 | 8 | reg_0009 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_014__sec_1_05 | 9 | reg_0010 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_014__sec_1_09 | 5 | reg_0006 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_014__sec_1_09 | 6 | reg_0007 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_014__sec_1_09 | 11 | reg_0012 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_014__sec_1_09 | 12 | reg_0013 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_014__sec_1_09 | 13 | reg_0014 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_014__sec_1_11 | 0 | reg_0001 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-02_014__sec_1_11 | 1 | reg_0002 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-02_014__sec_1_11 | 2 | reg_0003 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-03_002__sec_1_01 | 0 | reg_0001 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-03_002__sec_1_04 | 1 | reg_0002 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-03_002__sec_1_04 | 9 | reg_0010 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-03_002__sec_1_04 | 12 | reg_0013 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-03_002__sec_1_04 | 26 | reg_0027 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-03_002__sec_1_04 | 27 | reg_0028 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-03_002__sec_1_04 | 30 | reg_0031 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-03_002__sec_1_05 | 19 | reg_0020 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-03_018__sec_1_08 | 12 | reg_0013 | exclude | 원본 crop의 인쇄/로고/인증/논문 부분 |
| GS-03_018__sec_1_08 | 13 | reg_0014 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-03_018__sec_1_08 | 15 | reg_0016 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-03_018__sec_1_08 | 16 | reg_0017 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-03_018__sec_1_08 | 17 | reg_0018 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-03_018__sec_1_08 | 25 | reg_0026 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-03_018__sec_1_08 | 26 | reg_0027 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-03_018__sec_1_08 | 66 | reg_0067 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-03_018__sec_1_08 | 67 | reg_0068 | include | 원본 crop의 별도 안내/효능/메모 문구 |
| GS-03_018__sec_1_08 | 68 | reg_0069 | include | 원본 crop의 별도 안내/효능/메모 문구 |
