---
name: general-title
description: Generate 10 high-conversion Spanish product titles for Mercado Libre México from a product URL or image. Use when users ask for Mercado Libre listing titles,爆款标题, Mexican Spanish fashion titles, or title optimization for dresses, women's skirt sets, pants sets, vest sets, and formal gowns.
---

# General Title

Generate exactly 10 truthful, search-oriented titles for Mercado Libre México.

## Workflow

1. Inspect the input.
   - For a URL, open the product page and extract only visible product facts.
   - For an image, inspect the garment visually. Mark uncertain attributes as unknown; never invent fabric, brand, size, color, or construction.
2. Identify one primary category: dress, women's skirt set, women's pants set, women's vest set, or women's formal gown. If none fits, use the closest Mercado Libre category and retain the same title logic.
3. Research Mercado Libre México before writing:
   - Search the matching category on `mercadolibre.com.mx`.
   - Use Mercado Libre's own listings to break down keywords in this order: **silhouette/cut + audience + design features + style/occasion + components**.
   - Prefer listings showing more than 300 sales. Mercado Libre commonly exposes buckets such as `+500`, `+1000`, `+5mil`, and `+10mil vendidos`; treat these as qualifying.
   - Separate reusable wording from facts unique to another product. Never copy a competitor's brand, model, or unsupported feature.
4. Read [references/title-rules.md](references/title-rules.md), select the relevant category formula, and create a factual keyword pool.
5. Generate 10 materially different titles. Vary keyword order and the leading search intent while keeping the product identity accurate.
6. Validate every title against all constraints below. Rewrite any failure before responding.

## Title constraints

- Write in natural Mexican Spanish.
- Limit each title to **60 words maximum**, counted by whitespace-separated tokens.
- Put the core category term within the first three words whenever natural.
- Include the actual components for sets, such as `Top Y Falda` or `Chaleco Y Pantalón`.
- Use only attributes supported by the input image/page.
- Use high-intent terms before generic adjectives.
- Avoid prices, discounts, shipping claims, emojis, all caps, excessive punctuation, and keyword spam.
- Do not use a competitor's brand or model. Include a brand/model only when it belongs to the user's product.
- Do not repeat the same meaningful keyword more than twice in one title.
- Do not make all 10 titles minor punctuation variants.

## Output

Return only:

`品类：<identified category>`

`关键词：<版型> | <人群> | <设计特征> | <风格/场景> | <组件>`

Then a numbered list of exactly 10 Spanish titles. Do not add explanations, scores, translations, or invented attributes.

