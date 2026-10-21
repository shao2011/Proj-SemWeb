---
title: 'Tech stack: multimodal agentic RAG'
type: 'reference'
created: '2026-07-29'
status: 'living'
context:
  - 'document/rag.md'
---

# Tech Stack: Multimodal Agentic RAG

Giai đoạn 1: ưu tiên cloud API, chưa self-host.
Cập nhật: 2026-07-29.

---

## Giai đoạn 1 (cloud-first)

| Lớp | Lựa chọn | Ghi chú |
|---|---|---|
| Object storage | **Cloudflare R2** | Zero egress fee |
| Vector DB | **Qdrant** | Bật sparse vector cho hybrid, quantization int8, Matryoshka |
| OCR / parse tài liệu | **Datalab API** (Marker) | Self-host Surya/Marker để giai đoạn 2 |
| STT | **Soniox** | Async ~$0.10/h. Bật **request-time context** để nạp lexicon domain |
| Embedding | **Gemini Embedding 2** | $0.20/MTok, MRL (dims 128-3072), 8k context, multimodal (text/image/audio/video). Bản `gemini-embedding-001` rẻ hơn ($0.15/MTok) nếu chỉ cần text |
| Reranker | **Cohere Rerank 4 Pro** | ELO 1629 (#2 leaderboard), 100+ ngôn ngữ, $0.05/1M token ≈ $50/tháng ở 100k query. Dùng **Rerank 4 Fast** nếu cần latency thấp (447ms vs 614ms, cùng giá) |
| LLM | **OpenRouter** | Tier rẻ: DeepSeek V4 Flash / Gemini Flash-Lite. Tier mạnh: Claude / Gemini Pro. **Pin provider** để giữ prompt cache |
| Semantic cache | **Qdrant** (collection riêng) | query → answer, ngưỡng cosine ~0.95 |
| Framework | **LangChain + LangGraph** | LangGraph cho router + cascade |
| Observability | **Langfuse** | Log model, tier, cached_tokens, cost mỗi request |
| Web crawl (URL) | **Crawl4AI** | Local-first, có MCP server |
| Vietnamese NLP | **underthesea** / pyvi / VnCoreNLP | Word segmentation cho BM25 sparse |

### Lưu ý phân vai

- **DeepSeek không có API embedding.** Chỉ có chat (`deepseek-v4-flash`, `deepseek-v4-pro`). Dùng DeepSeek cho generation, Gemini cho embedding.
- **Reranker gần như không tốn tiền** ($20-50/tháng ở 100k query, chênh lệch giữa đắt nhất và rẻ nhất chỉ ~$30). Chọn theo chất lượng, không theo giá. Rerank tốt hơn giảm tỉ lệ escalate tier 3, tức tiết kiệm chi phí LLM.
- **Đo lại nDCG@10 trên data tiếng Việt** trước khi chốt. Leaderboard nặng tiếng Anh. Đối thủ A/B: **Voyage Rerank 2.5 Lite** (ELO 1520 ở $0.020, tỉ lệ chất lượng/giá tốt nhất), Voyage Rerank 2.5.
- **Không dùng ZeroEntropy API**: Zerank-2 tuy đứng #1 (ELO 1638, 265ms, $0.025) nhưng ZeroEntropy đã bị Notion mua, API hosted chỉ support tới **4/9/2026**. Model đã open-source nên để giai đoạn 2.
- `jina-reranker-v3` **không ở top 10** leaderboard head-to-head. Mạnh trên BEIR (61.94 nDCG@10) nhưng BEIR là benchmark tiếng Anh. Giữ làm ứng viên self-host giai đoạn 2.

### Free tier reranker (dùng để benchmark tiếng Việt, chi phí $0)

| Provider | Free tier | Đủ ~query |
|---|---|---|
| **Voyage AI** | **200M token**, một lần/account | **~19.000** |
| Cohere | 1.000 call/tháng, 10 RPM, pool dùng chung mọi endpoint | 1.000 |
| Mixedbread | 1.000 rerank request/tháng (lặp lại) | 1.000/tháng |
| Jina | 10M token/API key mới | ~900 |
| bge-reranker-v2-m3 | chạy local, free vĩnh viễn | không giới hạn |

Kế hoạch: build 1 bộ eval tiếng Việt (~300-500 query + gold label, nhiều domain), chạy qua tất cả, so nDCG@10 + latency. Bắt đầu bằng **Voyage** (đủ cho cả benchmark và beta), giữ Cohere trial riêng cho vòng head-to-head. `bge-reranker-v2-m3` làm **baseline sàn**: API trả tiền nào không thắng được nó thì không đáng trả.

---

## Giai đoạn 2 (self-host khi có volume)

| Lớp | Chuyển sang | Lý do |
|---|---|---|
| OCR | Self-host **Marker + Surya** | Rẻ hơn API ~50x. Datalab free cho startup dưới $2M funding/revenue |
| Reranker | Self-host **zerank-2** (ELO cao nhất, 265ms, đã open-source) hoặc **jina-reranker-v3** | Bỏ chi phí API, giữ chất lượng top |
| Embedding | **Model embedding tiếng Việt tự train** | Noise-robust với lỗi OCR/ASR, xem [rag.md](rag.md) §9b |
| Visual rerank | **jina-reranker-m0** | Lấy lại chất lượng visual document mà không cần ColPali |

---

## Các nguyên tắc kiến trúc đã chốt

1. **Text-first, media-on-demand.** Index chỉ text. Ảnh trang và audio gốc giữ trên R2, expose qua tool `view_page_image()` / `play_audio_segment()`.
2. **Cascade 4 tier** (cache → model rẻ → model trung → model mạnh). Router quyết định bằng **signal có sẵn** (điểm rerank, số entity, OCR confidence), không bằng LLM call.
3. **Prompt caching**: prefix ổn định từng byte, pin provider trên OpenRouter, verify bằng `cached_tokens`.
4. **Lazy parsing**: chỉ re-parse bằng VLM những trang vừa hot vừa có confidence thấp.
5. **Video**: mặc định ASR + OCR keyframe. Không frame captioning trừ khi query cần hình.
6. **Đa domain**: config là data (lexicon, chunking, rerank_top_k, escalation_threshold, citation_required). Một embedding base dùng chung, LoRA adapter nếu cần chuyên biệt.
7. **Qdrant multitenancy**: 1 collection + payload filter, không collection-per-tenant.

Chi tiết nghiên cứu và nguồn: [rag.md](rag.md).
