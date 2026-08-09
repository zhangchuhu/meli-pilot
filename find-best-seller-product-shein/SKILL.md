---
name: find-best-seller-product-shein
description: Find up to 15 high-selling SHEIN US fashion products similar to a supplied Mercado Libre product URL. Analyze the source garment, search SHEIN with English product-specific keywords, distinguish sponsored placements from organic results, verify exact garment category and image clarity, and report only products with at least 500 sold, 50 reviews, and a 4.1 rating. Use when asked to find SHEIN similar products, same styles, competitors, best sellers, or viral fashion products from a Mercado Libre link.
---

# Find Best-Seller Product on SHEIN

Use Chrome to turn one Mercado Libre fashion listing into a verified SHEIN US competitor shortlist.

## Input

Require one Mercado Libre product URL. Treat the URL target as authoritative when its visible label and href disagree.

## Output

Return up to 15 qualifying products with these fields in this order:

1. 标题
2. 链接
3. 是否 Ad
4. 价格
5. vendidos 数
6. 评价数
7. 评分
8. 场景

Also state the source-garment analysis, English search queries used, and how many candidates were inspected. Read [field-rules.md](references/field-rules.md) before collecting results.

## Workflow

### 1. Inspect the Mercado Libre source

Open the supplied URL and inspect the selected main SKU's title and gallery images. Derive a visual profile from the product itself, not merely the title:

- exact garment category and silhouette;
- color and color blocking;
- neckline, sleeve, length, fit, fabric appearance, closures, trim, lace, cutouts, back design, and other defining construction;
- likely use scene, such as casual, office, party, beach, vacation, or evening.

Do not confuse styling accessories with the garment being matched.

### 2. Build SHEIN search queries

Translate the visual profile into short English retail queries. Start with:

`[category] + [color] + [two or three defining features]`

Use one primary query and up to three controlled variants. Change only one attribute at a time, such as a synonym for the category or fabric. Avoid vague queries such as `women fashion`.

### 3. Search SHEIN US

Search at `https://us.shein.com/`. Preserve the visible search order and gather a broad pool, normally 40–80 unique candidates across the queries.

For every search card considered, record:

- query and visible position;
- whether the card is explicitly marked `Sponsored`, `Ad`, or equivalent;
- title, product URL, price, thumbnail, and any visible sold/rating text.

Never infer Ad status from rank alone. Deduplicate by canonical SHEIN product URL or product/goods ID.

### 4. Enforce visual compatibility

Keep only the same garment type and subtype as the source. Examples:

- dress → dress of a comparable silhouette;
- vest set → vest set;
- skirt set → skirt set;
- lace top → lace top, not a lace dress or bodysuit.

Open sufficiently large product imagery to confirm the category and defining details. Reject blurry, tiny, heavily obstructed, collage-only, or category-ambiguous images.

### 5. Verify each candidate

Open the SHEIN product page and verify the candidate using the evidence hierarchy in `references/field-rules.md`. A result qualifies only when all are independently supported:

- `vendidos >= 500`;
- `评价数 >= 50`;
- `评分 >= 4.1`.

Missing or ambiguous evidence fails the filter. Do not substitute review count for sold count and do not estimate numbers from badges, rank, or popularity language.

### 6. Prefer organic ranking

Select qualifying organic results first in their earliest visible search order. Sponsored results may fill remaining slots only when they meet every threshold and are clearly labeled. Never let a later ad displace an earlier qualifying organic result.

### 7. Report transparently

Return a Markdown table with the eight requested fields. Use `是`, `否`, or `未标注` for Ad status. Preserve displayed currency and compact count notation where useful, while including normalized numeric values when the threshold is not obvious.

If fewer than 15 products qualify, return only the verified set and explain the shortfall. Never weaken a threshold or invent a value to reach 15.

## Browser and failure handling

Use the user's Chrome session so locale, consent, and legitimate session state are preserved. Do not bypass CAPTCHAs or anti-bot checks. If blocked by a CAPTCHA, sign-in wall, unavailable region, or missing sales evidence, report the precise blocker and retain already verified results.

