"""OCR 전처리 — glossary_ingredient.xlsx「표기차이_주의목록」의 데이터팀 제안 치환 규칙.

받침·유사 자모 오독(듐→듬/륨/둠 · 올→을 · 롤→를 · 헥→헬/핵 · 넨→넷)을 되돌린다.
데이터팀 주의: 일반 문장에 쓰면 안 된다(예: "을"은 조사). 전성분 문단에만 적용할 것.
"""
OCR_FIX_RULES = [
    ("듬", "듐"), ("륨", "듐"), ("둠", "듐"),
    ("을", "올"), ("를", "롤"),
    ("헬", "헥"), ("핵", "헥"),
    ("넷", "넨"),
]


def ocr_fix_ingredient(text: str) -> str:
    for wrong, right in OCR_FIX_RULES:
        text = text.replace(wrong, right)
    return text
