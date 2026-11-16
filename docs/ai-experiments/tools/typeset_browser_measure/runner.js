// 브라우저 측 실행기 — typeset_browser_measure.py 가 Playwright 로 연다. 개발용, 운영 경로 아님.
//
// 측정 · 판정은 하지 않는다. FE 공통 모듈(타입만 지운 원본, ./fe/)의 함수를 그대로 부른다:
//   표시 CSS      typesetBlockStyle            (lib/n5/typeset-rules.ts — FE TypesetBlock 과 같은 호출)
//   측정 · 넘침   openTypesetMeasurer().measure (lib/n5/typeset-measurement.ts — DOM 줄바꿈 · DOM baseline · Canvas TextMetrics ink)
//   이미지 경계   buildFinalSectionOffsets · evaluateTypesetBlock (lib/n5/typeset-final-output.ts — Q-07 감지, 블록 넘침과 별개)
// 이 파일이 하는 일: 입력 블록 → FE 측정 입력 변환, 측정한 블록을 픽셀 대조용으로 옮기기, 섹션 합성 화면 만들기(확인 자료).
import { checkTypesetFontReady, openTypesetMeasurer } from './fe/typeset-measurement.js';
import { typesetBlockStyle } from './fe/typeset-rules.js';
import { buildFinalSectionOffsets, evaluateTypesetBlock } from './fe/typeset-final-output.js';

const host = document.getElementById('probe-host');
const stage = document.getElementById('stage');
let measurer = null;

// 입력 블록(renderInput 모양: box x·y·w·h, style 8키) → FE 측정 입력(bbox x·y·width·height, serverStyle).
// serverStyle 이 있으면 FE 는 역할 기본값으로 다시 계산하지 않는다(typesetBlockStyle). role 은 FE 입력 검사에만 쓰인다.
function toInput(sectionWidth, b) {
  return {
    text: b.text,
    role: b.role,
    sourceImageWidth: sectionWidth,
    bbox: { x: b.box.x, y: b.box.y, width: b.box.w, height: b.box.h },
    serverStyle: b.style,
  };
}

function rect(parent, r, cls) {
  const el = document.createElement('div');
  el.className = cls;
  Object.assign(el.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.right - r.left}px`, height: `${r.bottom - r.top}px` });
  parent.appendChild(el);
}

window.__tbm = {
  async open() {
    const fonts = await checkTypesetFontReady();
    const opened = await openTypesetMeasurer({ host });
    if (!opened.ok) return opened;
    measurer = opened.measurer;
    return { ok: true, fonts, userAgent: navigator.userAgent, dpr: window.devicePixelRatio };
  },

  measure(sectionWidth, block) {
    host.style.left = '0px';
    host.style.top = '0px';
    return measurer.measure(toInput(sectionWidth, block));
  },

  // 방금 측정한 블록 요소를 그대로 둔 채 host 만 정수 CSS px 로 옮기고 글자색만 검정으로 바꾼다(줄바꿈 · 글자 위치는 그대로).
  // 픽셀 대조용 촬영 준비이며 측정 결과를 바꾸지 않는다.
  placeForPixel(dx, dy, w, h) {
    host.style.left = `${dx}px`;
    host.style.top = `${dy}px`;
    measurer.blockElement.style.color = '#000000';
    document.documentElement.style.minWidth = `${w}px`;
    document.documentElement.style.minHeight = `${h}px`;
  },

  // 섹션 합성 — 배경 위에 FE 표시 CSS 로 모든 블록을 그린다. mode='clean'이면 섹션 밖을 잘라(섹션 PNG crop 과 같은 경계) 표시만,
  // 'annotated'면 섹션 밖도 보이게 두고 배치 영역(빨강) · 측정 ink(파랑) · DOM 줄 조각(초록 점선) · 섹션 경계(회색 점선)를 덧그린다.
  async renderSection(section, measured, mode, margin) {
    const img = new Image();
    img.src = section.backgroundUrl;
    await img.decode();
    stage.replaceChildren();
    stage.hidden = false;
    const m = mode === 'annotated' ? margin : 0;
    Object.assign(stage.style, { left: '0px', top: '0px', width: `${section.width + 2 * m}px`, height: `${section.height + 2 * m}px` });
    const sec = document.createElement('div');
    sec.className = 'section';
    Object.assign(sec.style, {
      left: `${m}px`, top: `${m}px`, width: `${section.width}px`, height: `${section.height}px`,
      backgroundImage: `url("${section.backgroundUrl}")`, overflow: mode === 'clean' ? 'hidden' : 'visible',
    });
    stage.appendChild(sec);
    for (const b of section.blocks) {
      const style = typesetBlockStyle(toInput(section.width, b));
      if (!style) continue;
      const el = document.createElement('div');
      el.dataset.typesetRender = String(b.blockId);
      Object.assign(el.style, style);
      el.textContent = b.text;
      sec.appendChild(el);
    }
    if (mode === 'annotated') {
      const ov = document.createElement('div');
      ov.className = 'overlay';
      Object.assign(ov.style, { left: `${m}px`, top: `${m}px`, width: `${section.width}px`, height: `${section.height}px` });
      stage.appendChild(ov);
      rect(ov, { left: 0, top: 0, right: section.width, bottom: section.height }, 'o-section');
      for (const b of section.blocks) {
        const r = measured[String(b.blockId)];
        const box = { left: b.box.x, top: b.box.y, right: b.box.x + b.box.w, bottom: b.box.y + b.box.h };
        rect(ov, box, r && r.ok && r.overflow.any ? 'o-box o-over' : 'o-box');
        if (r && r.ok) {
          for (const l of r.lines) rect(ov, l.rect, 'o-line');
          if (r.ink) rect(ov, r.ink, 'o-ink');
        }
      }
    }
    document.documentElement.style.minWidth = `${section.width + 2 * m}px`;
    document.documentElement.style.minHeight = `${section.height + 2 * m}px`;
    return { width: section.width + 2 * m, height: section.height + 2 * m };
  },

  // 측정 환경 조건 기록 — 이 브라우저의 Canvas TextMetrics 글리프 경계가 정수 px로 떨어지는지(플랫폼 래스터라이저 특성).
  // 소수 크기 · 서로 다른 글자에서 ascent · descent · left가 모두 정수면 quantized=true. 판정에는 쓰지 않는다.
  async probeMetrics() {
    const c = document.createElement('canvas').getContext('2d');
    const rows = [];
    for (const weight of [400, 600]) {
      await document.fonts.load(`${weight} 16px "Pretendard"`);
      for (const [text, px] of [['P', 16.3], ['j', 17.7], ['g', 23.45], ['Å', 31.1], ['가', 19.9]]) {
        c.font = `${weight} ${px}px "Pretendard"`;
        const m = c.measureText(text);
        rows.push({ weight, text, px, left: m.actualBoundingBoxLeft, right: m.actualBoundingBoxRight,
          ascent: m.actualBoundingBoxAscent, descent: m.actualBoundingBoxDescent, width: m.width });
      }
    }
    const quantized = rows.every((r) => [r.left, r.ascent, r.descent].every(Number.isInteger));
    return { quantized, rows };
  },

  hideStage() {
    stage.replaceChildren();
    stage.hidden = true;
  },

  // 최종 출력 이미지 경계(Q-07) — 출력 하나(include 섹션을 sectionOrder 순서로 이은 이미지)의 블록별 평가. FE 함수 그대로.
  evaluateFinal(output, items) {
    const layout = buildFinalSectionOffsets(output.sections);
    const image = { width: output.width, height: layout.totalHeight };
    const out = {};
    for (const it of items) {
      out[it.key] = evaluateTypesetBlock(it.measured, { sectionId: it.sectionId, excluded: false }, layout, image);
    }
    return { image, offsets: Object.fromEntries(layout.offsets), blocks: out };
  },
};
document.getElementById('status').dataset.ready = 'true';
