# Pi.ai theme spec — tài liệu reimplement

Nguồn: CSS production của pi.ai (Wayback 2026-08-02, `pi.ai/_next/static/css/522bf01f21c02700.css`) + HTML archived của pi.ai/talk (2026-08-01). Toàn bộ giá trị bên dưới trích nguyên văn, không phỏng đoán.

## 1. Tư tưởng thiết kế

- "Warm paper": nền kem, không trắng tinh, không đen tuyền. Mọi màu xám/đen đều ngả ấm.
- Lời AI in bằng **serif** cỡ lớn, đặt thẳng trên nền, **không có bubble**. Chỉ tin nhắn user mới có bubble màu be.
- Accent duy nhất: xanh rêu đậm (nút) + xanh lá tươi (link/trạng thái).
- Bo tròn lớn (12-20px, nút pill 100px), shadow nâu ấm chứ không đen.
- Dark mode bật bằng class `.dark` trên root (không dùng `prefers-color-scheme`).

## 2. Design tokens

### Light (`:root`)

```css
:root {
  /* nền */
  --color-background: #FAF3EA;        /* nền chính */
  --color-background-alt: #F7EFE4;    /* sidebar, nền phụ */
  --color-contrast: #FCFAF7;          /* surface: card, input, modal, menu */
  --color-message-background: #F3EADE;/* bubble tin nhắn USER */
  --color-message-text: #131212;

  /* chữ */
  --color-text-primary: #1A4631;      /* chữ chính — xanh rêu */
  --color-text-primary-strong: #0D3C26; /* heading */
  --color-text-accent: #131212;       /* đen ấm */
  --color-text-secondary: #655E55;    /* nâu xám */
  --color-text-tertiary: #A69986;
  --color-placeholder: #BEB09D;

  /* accent */
  --color-primary-default: #1A4631;   /* nút chính; hover #1E3C2E, active #21322A */
  --color-tertiary-default: #038247;  /* xanh lá link/active; hover #077843, active #0A6E40 */

  /* fill & stroke */
  --color-fill-default: #ECE1D2;      /* hover #E9DDCD, active #E4D7C6 */
  --color-secondary-default: #F5EADC; /* nút phụ; hover #E9DDCD */
  --color-divider-stroke: #E1D3C0;
  --color-menu-stroke: #E9DDCD;
  --color-input-surface: #FCFAF7;
  --color-input-border: #D9C9B4;
  --color-card-default: #FCFAF7;      /* hover #FAF3EA, active #F7EFE3 */
  --color-panel-stroke: #E3D4BF;

  /* trạng thái */
  --color-error-default: #D42600;     /* hover #C72400, active #BB2100 */
  --color-disabled-fill: #C2B39F;
  --color-disable-stroke: #D9C9B4;
  --color-progress-highlight: #7FC099;
  --color-progress-base: #038247;

  /* misc */
  --color-sidebar-logo: #6B6255;
  --color-modal-backlay: rgba(107,98,85,.36); /* overlay NÂU mờ, không đen */
  --color-misc-scrollbar: #C2B39F;
  --color-skeleton-base: rgba(247,239,227,1);
  --color-skeleton-shimmer: rgba(159,147,120,.15);

  /* shadow — nâu ấm */
  --shadow-1: 0px 6px 24px -2px rgba(94,68,15,.12);
  --shadow-3: 0px 2px 16px -6px rgba(0,0,0,.1), 0px 12px 32px -6px #e4dbc8;
  --shadow-modal: 0px 2px 16px -6px rgba(0,0,0,.1), 0px 12px 20px -6px rgba(94,68,15,.12);
}
```

### Dark (`.dark`)

```css
.dark {
  --color-background: #131212;
  --color-background-alt: #1A1918;
  --color-contrast: #1A1918;
  --color-message-background: #2D2B29;
  --color-message-text: #D8D4CE;
  --color-text-primary: #D8D4CE;          /* chữ chuyển kem, KHÔNG còn xanh */
  --color-text-primary-strong: #F7EFE3;
  --color-text-secondary: #B4AFA7;
  --color-text-tertiary: #AAA6A1;
  --color-placeholder: #AAA6A1;
  --color-primary-default: #1A4631;
  --color-button-accent-default: #289A62; /* accent sáng hơn ở dark */
  --color-tertiary-default: #43A774;
  --color-text-primary-alt: #33C782;
  --color-divider-stroke: #504A42;
  --color-menu-stroke: #3E3A35;
  --color-input-surface: #2C2A28;
  --color-input-border: #4B4844;
  --color-secondary-default: #33312E;
  --color-fill-default: /* dùng #33312E vùng này */;
  --color-error-default: #D42600;
}
```

## 3. Font

### Pi dùng gì (bản live 8/2026)

Root div có class `brand-font-scope` — remap font về bộ "legacy":

| Vai trò | Font thật | Loại | Bản quyền |
|---|---|---|---|
| Chat text, heading | **GT Alpina** (`LEGACY_ALPINA`) | serif | Grilli Type, trả phí |
| UI, nút, input | **ABC Oracle** (`LEGACY_ORACLE`) | sans | Dinamo, trả phí |
| Bộ mới (chưa bật) | Grenette (serif) + **DM Sans** (sans) | | Grenette trả phí, DM Sans FREE |
| Code | mono riêng (`MONO`) | mono | |

Fallback stack họ khai: `ui-serif, Georgia, Cambria, "Times New Roman", Times, serif`.

### Thay thế miễn phí (khuyến nghị)

- Serif chat: **Lora** hoặc **Source Serif 4** (Google Fonts). Muốn nhiều cá tính giống GT Alpina hơn: **Fraunces**.
- Sans UI: **DM Sans** — chính Pi ship font này, free trên Google Fonts. Dùng weight 500 làm mặc định UI (Pi để 500 gần như mọi chỗ).
- Mono: JetBrains Mono / IBM Plex Mono.

### Chống layout-shift (Pi làm vậy)

```css
@font-face {
  font-family: "Serif Fallback";
  src: local("Arial");
  ascent-override: 98.06%; descent-override: 31.38%;
  line-gap-override: 0%; size-adjust: 101.98%;
}
```
(số của GRENETTE; DM_SANS: ascent 92.63%, descent 28.95%, size-adjust 107.09%)

## 4. Typography scale (trích nguyên văn)

| Class | Size / line-height / tracking | Weight | Font |
|---|---|---|---|
| `t-heading-xl` | 3rem/2.85rem, -0.96px (desktop 4rem/3.8rem) | 400 | serif condensed |
| `t-heading-l` | 2.25rem/2.25rem, -0.54px (desktop 3rem) | 400 | serif condensed |
| `t-heading-m` | 2rem/2.2rem, -0.64px | 400 | serif condensed |
| `t-body-chat` | mobile 1.1875rem (19px), ≥1024px 1.375rem/1.99rem (22px), ≥1536px 1.5rem/2.175rem (24px), tracking -0.19→-0.24px | 400 | **serif** |
| `.chat-message` | 22px / 145% | 400 | serif |
| `t-body-m` | 1.125rem/1.575rem, -0.36px (desktop 1.25rem/1.75rem) | 500 | sans |
| `t-body-s` | 0.875rem/1.225rem, -0.14px (desktop 1.0625rem) | 500 | sans |
| `t-action-l` (nút to) | 1.375rem/1.65rem, -0.22px | 500 | sans |
| `t-action-m` | 1.125rem/1.35rem, -0.18px | 500 | sans |
| `t-action-s` | 1rem/1.2rem, -0.16px | 500 | sans |
| `t-label` | 0.875rem/1.225rem, -0.24px | 500 | sans |
| `t-code` | 0.875rem/1.356rem, tracking 0 | 400 | mono |

Quy luật: serif luôn weight 400, sans luôn 500, letter-spacing luôn âm nhẹ, line-height ~140-145%.

## 5. Layout & component

### Khung trang
- Sidebar: `width 280px`, `bg-background-alt`, `border-r 1px var(--color-divider-stroke)`, chiếm `100dvh`. Mobile: overlay absolute z-999.
- Main: flex-1 trên `bg-background`.
- Vùng composer đáy: `border-top 1px rgb(237,225,209)`, padding-top 18px, padding-bottom 30px.

### Message
- **AI: không bubble.** Text serif `t-body-chat` màu `--color-text-primary` đặt thẳng trên nền.
- **User: bubble** `bg #F3EADE`, chữ `#131212`, bo ~20px, căn phải.
- Typing indicator: 3 chấm `animate-bounce` (keyframe bounce 1s infinite, translateY(-25%)↔0).

### Composer
- Ô nhập: `bg var(--color-input-surface)`, `border 1px var(--color-input-border)`, bo tròn lớn (pill khi 1 dòng), placeholder màu `#BEB09D` weight 500 sans.
- Focus: `focus-within:border-text-primary` + `ring-1 ring-text-primary` (viền chuyển xanh rêu, không glow xanh dương).
- Nút gửi: tròn, `bg #1A4631`, icon trắng ngà `#FCFAF7`; hover `#1E3C2E`.

### Nút & interaction
- Hover: đổi màu theo cặp default→hover→tap, hoặc `opacity .8`, hoặc `scale(.97)`. Transition chỉ dùng `transition-colors`, nhanh, không animation màu mè.
- Nút pill: `border-radius 100px`. Card/menu: 12px (`rounded-xl`). Item nhỏ: 10px.
- Disabled: `opacity .5`, `cursor-not-allowed`.

### Chip gợi ý (suggestion)
- Label: sans 16px/145%, màu `#6B6255`, weight 400.
- Chip xanh: chữ 10px `#038247`, nền `#03824714` (8% opacity), pill 100px, padding 4px 12px.

### Scrollbar
```css
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--color-divider-stroke); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--color-disable-stroke); }
```
Vùng chat list dùng `scrollbar-hide` (ẩn hẳn).

### Skeleton loading
Gradient chạy ngang: base `rgba(247,239,227,1)` + shimmer `rgba(159,147,120,.15)`, `background-size 200%`, keyframe đổi `background-position-x -100% → 100%` trong 2s.

### Modal
- Overlay `rgba(107,98,85,.36)` (nâu mờ). Modal `bg #FCFAF7`, viền `#E9DDCD`, shadow modal ấm, bo 20px.

## 6. Checklist reimplement cho eslothai-chat

1. Khai CSS variables (mục 2) vào `frontend/src/styles/global.css`, dark mode qua class `.dark` trên `<html>`.
2. Load font Google: DM Sans (500, 400) + Lora hoặc Source Serif 4 (400 + italic). Khai `--font-serif`, `--font-sans`.
3. Chat text AI: serif 19→22px responsive, line-height 145%, màu `--color-text-primary`, không bubble.
4. User bubble: `#F3EADE`, bo 20px.
5. Composer: surface `#FCFAF7` + border `#D9C9B4`, focus ring xanh rêu.
6. Sidebar 280px `#F7EFE4`.
7. Scrollbar 6px + modal overlay nâu.
8. Mọi shadow đổi sang bộ shadow nâu ấm mục 2.

## 7. File tham khảo

- CSS gốc đầy đủ: đã tải về scratchpad phiên làm việc (`pi.css`, 222KB). Tải lại: `https://web.archive.org/web/20260802063146/https://pi.ai/_next/static/css/522bf01f21c02700.css`
- Font woff2 gốc (chỉ để tham khảo metrics — GT Alpina/Oracle/Grenette là font thương mại, không được bundle vào sản phẩm):
  - Grenette: `.../_next/static/media/88d4cdef7e99559e-s.p.woff2` (qua prefix wayback trên)
  - GT Alpina: `.../_next/static/media/dcfe9eb862defd62-s.p.woff2`
  - ABC Oracle: `.../_next/static/media/fafeb41f0dcde155-s.p.woff2`
  - DM Sans: `.../_next/static/media/210398b63ea2710a-s.p.woff2`
