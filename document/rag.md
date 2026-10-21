---
title: 'Multimodal RAG chatbot: field survey and chosen architecture'
type: 'orientation'
created: '2026-07-29'
status: 'living'
context:
  - 'document/techstack.md'
  - 'document/rag-workflow/1.ingestion.md'
---

# Multimodal RAG Chatbot (file / audio / video / URL)

Tổng quan lĩnh vực, ưu tiên phương pháp từ 2025 trở đi. Cập nhật: 2026-07-29.

---

## 0. TL;DR (đọc 2 phút)

Nếu bạn xây chatbot RAG nhận file + audio + video + URL vào giữa 2026, thiết kế "đúng thời điểm" gồm 5 quyết định:

| Quyết định | Lựa chọn 2023-2024 (cũ) | Lựa chọn 2025-2026 (mới) |
|---|---|---|
| Đọc tài liệu | OCR pipeline (Tesseract/PaddleOCR) + layout parser | VLM parser 1 lượt (MinerU 2.5, Granite-Docling-258M, dots.ocr, DeepSeek-OCR 2) **hoặc** bỏ hẳn parsing, retrieve trực tiếp trên ảnh trang |
| Biểu diễn | 1 vector / chunk text | Multi-vector late interaction trên ảnh trang (ColPali → ColQwen3, ColNomic, Nemotron ColEmbed) + omni-modal embedding |
| Audio | ASR → text → RAG text | Native audio retrieval (WavRAG, SpeechRAG) bỏ qua ASR, hoặc ASR mới (Voxtral Transcribe 2, MOSS-Transcribe-Diarize) |
| Video | Caption frame → text | Frame selection + multimodal graph memory (VideoRAG HKUDS, AdaVideoRAG, MegaRAG) |
| Điều phối | Pipeline retrieve-then-answer 1 lượt, prompt-engineered agent loop | Agentic RAG **được huấn luyện bằng RL** (Kimi-Researcher, AutoSearch, SAAS), hybrid retrieval + long-context |

Câu chốt của 2026: **không còn "RAG vs long context"**, mà là retrieve 50K-200K token đúng rồi để long-context reasoning trên đó. Và **không còn viết tay agent loop**, mà train policy quyết định khi nào search / search gì / khi nào dừng.

---

## 1. Kiến trúc tham chiếu

```
┌─────────── INGESTION (offline / on-upload) ───────────┐
│  file (pdf/docx/pptx/xlsx)  → VLM parser | page-image │
│  audio (mp3/wav)            → ASR+diarize | audio emb │
│  video (mp4/youtube)        → keyframe + ASR + OCR     │
│  URL                        → headless crawl → md      │
└────────────────────┬──────────────────────────────────┘
                     ▼
        ┌──── UNIFIED INDEX (3 lớp song song) ────┐
        │ A. text/BM25 (sparse, hybrid)           │
        │ B. dense/omni-modal embedding           │
        │ C. multi-vector visual (ColPali-style)  │
        │ D. graph/KG + hierarchical summary      │
        └────────────────┬────────────────────────┘
                         ▼
┌───────── QUERY TIME (agentic, multi-turn) ─────────┐
│ router → (modality chọn?) → hybrid retrieve        │
│  → multimodal rerank → context assembly            │
│  → MLLM generate + citation/grounding              │
│  → self-check (đủ chưa?) → loop hoặc trả lời       │
└────────────────────────────────────────────────────┘
```

Điểm mới quan trọng nhất về kiến trúc: lớp D (graph + summary) và vòng lặp agentic được **học** thay vì **viết tay**.

---

## 2. Ingestion theo từng modality

### 2.1 File / tài liệu (phần khó nhất, và cũng quan trọng nhất)

**Vấn đề đã được chứng minh:** OCR làm hỏng RAG. Paper *OCR Hinders RAG* (arXiv 2412.02592, bộ benchmark OHRBench) đo được lỗi OCR lan truyền theo tầng (cascading) xuống retrieval rồi xuống generation. Bản mở rộng 2026 (*When Good OCR Is Not Enough*, arXiv 2605.00911) benchmark riêng độ bền của OCR trong ngữ cảnh RAG. Kết luận: đừng coi parsing là bước "đã giải quyết".

**Ba hướng tiếp cận, xếp theo độ mới:**

1. **VLM parser end-to-end (2025-2026, đang thắng cho self-host).**
   - **MinerU 2.5** (OpenDataLab): PDF/Office → markdown/JSON sẵn cho LLM, tối ưu workflow agentic.
   - **Granite-Docling-258M** (IBM, Apache-2.0): parse 1 trang trong 1 lượt forward, model chỉ 258M. Docling đã vào Linux Foundation AI & Data, hiện là lựa chọn "VLM-grade mà self-host được" mạnh nhất cho pipeline lớn (batching, retry, scale).
   - **dots.ocr**, **DeepSeek-OCR 2**, **NuExtract 3** (schema-filling: điền trực tiếp vào JSON schema, bỏ qua bước markdown trung gian).
   - Model đóng: đầu 2026 các frontier model đã **vượt** OCR chuyên dụng trên tài liệu thật (OCR Arena: Gemini 3 Flash > Gemini 3 Pro > Claude Opus 4.6 > GPT-5.2). Open-source vẫn kém: Gemini-3-Pro hơn dots.mocr ~7.9% ở ảnh chụp tay.
   - Pattern production hay dùng: **confidence routing**. Parser rẻ chạy trước, nếu 0.70 < confidence < 0.90 thì route sang dots.ocr / Qwen3-VL-8B, dưới 0.70 route sang frontier VLM.

2. **Vision-native retrieval: bỏ hẳn parsing.** Xem mục 3.2. Đây là hướng có nhiều paper mới nhất và ViDoRe V3 xác nhận visual retriever > textual retriever.

3. **Hybrid (thực tế nhất):** giữ cả text đã parse và ảnh trang, retrieve trên cả hai. ViDoRe V3 kết luận context hybrid hoặc thuần visual đều cải thiện chất lượng sinh câu trả lời.

**Benchmark để đo:** OmniDocBench (CVPR 2025), MDPBench (đa ngữ, tình huống thực, arXiv 2603.28130 -- đáng chú ý cho tiếng Việt), ParseBench (arXiv 2604.08538, parsing cho agent).

### 2.2 Audio

Hai trường phái:

**(a) ASR-then-RAG (cascade).** Vẫn là mặc định production. Cập nhật model 2026:
- **Voxtral Transcribe 2** (Mistral, 04/02/2026): WER trung bình 5.9% vs Whisper 7.4% trên FLEURS, native streaming realtime, xử lý audio tới 3 giờ trong 1 lần (không cần chunk 30s như Whisper). **Nhưng chỉ 13 ngôn ngữ và KHÔNG có tiếng Việt.**
- **Whisper** (large-v3 / turbo): vẫn là lựa chọn cho tiếng Việt và các ngôn ngữ ngoài top-13. Nhược điểm: cửa sổ 30s, cần tooling ngoài cho long-form.
- **NVIDIA Parakeet TDT 0.6B v3**: FastConformer + TDT decoder, cân bằng tốt accuracy/throughput, phù hợp batch lớn.
- **MOSS-Transcribe-Diarize 0.9B**: sinh transcript + speaker label + word timestamp trong **một** lượt, thay cho chuỗi ASR → diarization riêng. Context 128k ≈ 90 phút audio. Rất hữu ích cho họp/podcast (biết ai nói câu nào là metadata quan trọng cho RAG).
- Tiếng Việt: *Vietnamese ASR: A Revisit* (Findings of EACL 2026) là điểm khởi đầu để chọn baseline.
- Vấn đề chưa giải: hậu xử lý lỗi ASR cho hội thoại dài (*Ontology Memory-Augmented ASR Correction*, arXiv 2606.13464), hiệu quả inference long-form (MURMUR, arXiv 2606.01483).

**"ASR hinders RAG" cũng có bằng chứng, và tệ hơn OCR.** Đây là câu hỏi đúng phải đặt ra:

- **On the Impact of Speech Recognition Errors in Passage Retrieval for Spoken QA** (arXiv 2209.12944): bài tương đương OHRBench nhưng cho speech. Lỗi ASR lan truyền xuống retrieval, và tập trung đúng vào **named entity**, thứ mà retrieval phụ thuộc nhiều nhất.
- **ViSQA** (benchmark Spoken QA **tiếng Việt**, có bản audio sạch và bản nhiễu): ViT5 Exact Match tụt từ **62.04% xuống 36.30%** khi có lỗi ASR. Mất 26 điểm. Đây là con số quan trọng nhất trong cả file này nếu bạn làm tiếng Việt.
- **Analyzing Error Propagation in Korean Spoken QA with ASR-LLM Cascades** (arXiv 2605.17443): mức suy giảm tương đối **nhất quán giữa các LLM** khác nhau về năng lực tuyệt đối. Nghĩa là: đổi LLM mạnh hơn **không cứu được**, vì thiệt hại bám theo lượng thông tin đã mất ở tầng ASR. Một lỗi 1 ký tự có thể làm gold answer biến mất hoàn toàn.
- **SpeechDPR** (arXiv 2401.13463): vượt cascade rõ rệt khi **WER > 40%**. Đây là ngưỡng để quyết định có cần bỏ ASR hay không.
- **WER là thước đo sai** (AssemblyAI, 2026): WER đếm "My Cadillac" → "My cataracts" là **1 lỗi**, nhưng ngữ cảnh downstream bị phá hủy hoàn toàn. Phải đo **entity WER / entity recall**, không phải WER tổng.

**Điểm khác biệt cần nắm giữa hai bài toán:**

| | OCR làm mất gì | ASR làm mất gì |
|---|---|---|
| Nội dung | Ký tự (nhất là ảnh chụp, chữ ký, con dấu) | Named entity, số, tên riêng, thuật ngữ luật |
| Cấu trúc | Bảng, thứ tự đọc, layout | **Toàn bộ.** Không có dấu câu, không đoạn, không tiêu đề |
| Cứu được không | Có, giữ ảnh trang rồi retrieve trực tiếp trên ảnh | Không có "ảnh" tương đương. Chỉ có audio gốc |
| Hậu quả riêng | Bảng bị flatten thành vô nghĩa | Chunking không có ranh giới tự nhiên |

Điểm thứ hai là chỗ nhiều người bỏ qua: **transcript ASU thô không có cấu trúc để chunk**. Fixed-window 512 token trên transcript liên tục sẽ cắt giữa câu, giữa ý. Phải dùng **ranh giới diarization (lượt nói) + semantic chunking**, và giữ timestamp làm metadata.

**(b) Native audio RAG (hướng nghiên cứu mới, 2025+).** Ý tưởng: ASR làm mất prosody, cảm xúc, tiếng ồn, danh từ riêng, và tự nó mắc lỗi.
- **WavRAG** (ACL 2025 Long, arXiv 2502.14727): RAG framework đầu tiên hỗ trợ audio **native end-to-end**, embed và retrieve trực tiếp raw audio, biểu diễn audio + text trong một không gian kiến thức chung. Retrieval ngang ngửa pipeline ASR-Text SOTA nhưng **nhanh gấp 10 lần**.
- **SpeechRAG** (ICASSP 2025, arXiv 2412.16500): align không gian embedding text và speech, cho phép truy vấn **bằng text** để retrieve trực tiếp **đoạn audio**, không cần ASR.
- **SRAG** (ICASSP 2024): retrieval-based prompting cho spoken language understanding, vượt cả end-to-end và cascaded SLU ở intent prediction.
- **Hearing More with Less** (arXiv 2508.01166): multi-modal retrieval-and-selection để cải thiện chính ASR trong hội thoại.

Khuyến nghị: production dùng (a) + lưu audio gốc; nếu muốn đóng góp nghiên cứu thì (b) là chỗ còn trống, đặc biệt cho tiếng Việt (chưa ai làm native audio retrieval tiếng Việt).

### 2.3 Video (nhiều paper mới nhất nằm ở đây)

Video khó vì: dài (context nổ), đa kênh (hình + tiếng + chữ trên hình), và có trục thời gian.

**Các hướng chính 2025-2026:**

- **VideoRAG (Jeong et al., Findings of ACL 2025, arXiv 2501.05874):** RAG **trên một corpus video** (không phải 1 video). Retrieve video liên quan theo query rồi đưa vào LVLM. Đây là bài định nghĩa bài toán.
- **VideoRAG (HKUDS, KDD 2026, "Chat with Your Videos"):** cho video **cực dài**. Hai đóng góp: (1) cơ chế **frame selection** chọn tập frame giàu thông tin nhất, (2) chiến lược trích thông tin text từ video **khi không có phụ đề**. Repo: github.com/HKUDS/VideoRAG.
- **Video-RAG (OpenReview QaZxGWlbgO):** "visually-aligned" RAG. Dùng tool mã nguồn mở trích **auxiliary text** đã align với hình: audio (ASR), OCR chữ trên frame, object detection. Rẻ, không cần train, cắm được vào LVLM có sẵn. Đây là baseline mạnh nhất về tỉ lệ hiệu quả/chi phí.
- **AdaVideoRAG (arXiv 2506.13589):** retrieval **thích ứng** theo độ khó query (omni-contextual adaptive). Query dễ thì retrieve ít, query khó mới đào sâu. Cùng họ tư tưởng với AutoSearch/SAAS ở mục 5.
- **Multi-RAG (arXiv 2505.23990):** hệ thống adaptive video understanding, hợp nhất video + audio + document bằng cách chuyển **tất cả về biểu diễn text** (visual/audio → descriptive text + metadata) rồi index vector. Đơn giản, dễ triển khai, mất thông tin hình.
- **Structured memory cho ultra-long agentic video reasoning (arXiv 2605.08271):** "Bridging Modalities, Spanning Time". Xây memory có cấu trúc cho video siêu dài, agent reasoning trên đó.
- **Rethinking RAG in Long Videos (arXiv 2606.13141):** bài mổ xẻ "retrieve **cái gì** và **dùng thế nào**" trong video dài. Nên đọc để biết cái gì thực sự có tác dụng.
- **CRAFT (arXiv 2605.19075):** critic-refined adaptive keyframe targeting cho video QA. Có critic phản biện việc chọn keyframe.
- **MARQUIS (arXiv 2605.17640):** pipeline 3 tầng cho video RAG.

**Bias cần biết:** *Who is in the Spotlight* (arXiv 2506.11063) chỉ ra bias tiềm ẩn phá hoại multimodal RAG (mô hình bám vào modality/vị trí nào đó, bỏ evidence còn lại). Cần đo, không nên bỏ qua.

### 2.4 URL / web

Ít paper, nhiều engineering. Nhưng đừng coi nhẹ: URL là nguồn dữ liệu bẩn nhất.

- **Crawl4AI** (~60K stars, local-first, Playwright → markdown, có chunking helper, có **MCP server** built-in để agent gọi trực tiếp). Điểm mạnh 2026: "Adaptive Intelligence" tự học selector đáng tin theo thời gian. Miễn phí, self-host.
- **Firecrawl** (managed API): markdown/JSON sạch, giữ cấu trúc tài liệu và tiết kiệm token, recursive crawl để dựng knowledge base, tích hợp sẵn LangChain/LlamaIndex. Có **FIRE-1**, navigation agent tự điều hướng site phức tạp.
- Lựa chọn: cần kiểm soát + on-prem (dữ liệu luật, dữ liệu nội bộ) → Crawl4AI. Cần nhanh, ít vận hành → Firecrawl.
- Lưu ý thiết kế: URL cần **refresh policy** (nội dung thay đổi), **canonical/dedup** (cùng nội dung nhiều URL), và tách "URL người dùng dán vào ngay lúc chat" (ingest nóng, latency-sensitive) khỏi "URL trong knowledge base" (ingest nguội, batch).

---

## 3. Lớp biểu diễn (representation)

### 3.1 Omni-modal embedding: một model cho mọi modality

Đây là thay đổi lớn 2025-2026: thay vì 4 encoder rời rồi tìm cách ghép, dùng **một** không gian embedding.

- **Omni-Embed-Nemotron** (NVIDIA, arXiv 2510.03458): retrieval model hợp nhất text + image + audio + video. Hỗ trợ cả **cross-modal** (text→video) và **joint-modal** (text→video+audio) trong một model. Đây là bài tham chiếu chính.
- **jina-embeddings-v5-omni**: ghép text backbone **đóng băng** với vision + audio encoder đóng băng, chỉ train phần connector. Rẻ, dễ tái lập, ý tưởng rất đáng học nếu bạn muốn làm bản tiếng Việt.
- **Fusion Embedding** (arXiv 2607.18666): mở rộng một vision-language embedding model sẵn có sang audio, **đóng băng cả base và audio tower**. Cùng triết lý "train ít nhất có thể".
- **VLM2Vec-V2** (arXiv 2507.04590): embedding cho video, image, và visual document.
- SOTA giữa 2026 trên **MMEB-V2** (78 task): 66.4 điểm tổng, build trên Qwen2.5-Omni-7B với cross-modal alignment tường minh.
- Kiến trúc chung: input processor riêng theo modality (audio tokenizer, vision patch embedder, text tokenizer) → **shared transformer backbone** → embedding. Chính backbone chung tạo ra không gian hợp nhất.
- Native video encoder (embed clip **có cấu trúc thời gian**) đã thay thế cách average frame embedding.

### 3.2 Multi-vector / late interaction cho tài liệu dạng ảnh

Hướng nóng nhất về retrieval tài liệu.

- **ColPali** (arXiv 2407.01449, gốc 2024 nhưng là nền của mọi thứ 2025-2026): SigLIP-So400m, 1024 patch embedding/trang (grid 32x32 trên input 448x448), mỗi patch 128-dim, tính relevance bằng **MaxSim** (kế thừa ColBERT). Giữ granularity mức patch thay vì nén cả trang vào 1 vector.
- Thế hệ sau: **ColQwen3-4B** (backbone Qwen3, SOTA ViDoRe), **ColNomic**, **Nemotron ColEmbed**, **ColModernVBERT** (250M params, kém ColPali chỉ 0.6 điểm NDCG@5 với **10x ít tham số** hơn -- rất đáng chú ý nếu bạn deploy hạn chế GPU).
- Vấn đề của multi-vector là **chi phí lưu trữ và search**. Các giải pháp 2025-2026:
  - **Hierarchical Patch Compression cho ColPali** (arXiv 2506.21601): dynamic pruning + quantization.
  - **Visual RAG Toolkit** (arXiv 2602.12510): training-free pooling + multi-stage search để scale.
  - **NanoVDR** (arXiv 2603.12824): distill retriever VLM 2B xuống encoder **text-only 70M**. Hướng rất thực dụng cho production.
- Tinh chỉnh về chất lượng:
  - **Argus-Retriever** (arXiv 2606.04300): late-interaction region-aware + query-conditioned MoE.
  - **LITTA** (arXiv 2603.26683): late interaction + test-time alignment.
  - **Spatially-Grounded Document Retrieval** (arXiv 2512.02660): patch-to-region relevance propagation, cho grounding theo vùng.
  - **Beyond the Grid** (arXiv 2603.01666): multi-vector **có thông tin layout** từ biểu diễn tài liệu đã parse. Tức là kết hợp lại parsing với visual retrieval.

### 3.3 Chunking (đừng over-engineer)

Số liệu 2026 khá tỉnh táo:
- **Recursive character splitting, 512 token** vẫn là default tốt nhất cho phần lớn hệ thống (accuracy end-to-end ~69% trong benchmark, nhanh, rẻ, chịu được tài liệu hỗn hợp).
- **Semantic chunking**: +recall tới 9%, đổi lại phải embed từng câu.
- **Late chunking**: embed **cả tài liệu** bằng long-context model trước, rồi mới cắt token embedding thành chunk. Mỗi chunk vì thế mang tín hiệu ngữ nghĩa tầm xa. Dùng khi **cross-reference bị đứt** (rất đúng với văn bản luật: "theo quy định tại Điều 5 Khoản 2...").
- **Contextual Retrieval** (Anthropic): thêm context do LLM sinh vào mỗi chunk. Giảm retrieval failure rõ khi kết hợp hybrid search + rerank, nhưng tốn 1 LLM call/chunk lúc ingest.
- Nghiên cứu mới: **Query-Adaptive Semantic Chunking** (arXiv 2605.22834, chunk động + mở rộng cửa sổ theo query), **SLIDE** (arXiv 2503.17952), **Semantic Entanglement in Vector-Based Retrieval** (arXiv 2604.17677, framework hình thức cho hiện tượng "rối" ngữ nghĩa trong agentic RAG).
- Nguyên tắc: bắt đầu recursive 512 token với token-accurate counting, chỉ nâng cấp khi **metric** chỉ ra bottleneck cụ thể.

---

## 4. Retrieval, rerank, graph

### 4.1 Rerank đa mô thức

ViDoRe V3 xác nhận: **textual reranking cải thiện đáng kể** kể cả khi retrieve bằng visual. Vậy rerank là bắt buộc, không phải tùy chọn.

- **MM-R5** (arXiv 2506.12364): reranker đa mô thức tăng cường reasoning, train bằng **RL**.
- **RagVL / MLLM Is a Strong Reranker** (arXiv 2407.21439): knowledge-enhanced reranking + noise-injected training. Bài kinh điển của hướng này.
- **Rank-Nexus** (2601.20623): VLM 2B nhẹ, mạnh cả text rerank (TREC, BEIR) lẫn image rerank (INQUIRE, MMDocIR). Với image thiếu data, họ distill từ caption của MLLM.
- **ZipRerank** (arXiv 2605.11864): listwise multimodal rerank **cực kỳ hiệu quả** cho tài liệu dài. Giảm input length bằng early interaction query-image nhẹ, và **bỏ autoregressive decoding**, score toàn bộ candidate trong 1 forward pass. Đây là bài nên đọc nếu bạn quan tâm latency.
- **miniReranker** (arXiv 2606.10759): visual cache reuse + interaction sparsity, precache phần visual tái sử dụng được.

### 4.2 Graph / KG đa mô thức

- **MegaRAG** (ACL 2026 Long, arXiv 2512.20626): graph-based multimodal RAG. Hợp nhất tín hiệu **text + visual + spatial** thành concept có cấu trúc, phân cấp cho visual document. Điểm mới: đưa visual cue vào **cả 3 chỗ** (dựng KG, retrieval, và generation), không chỉ lúc index.
- **LightRAG** (EMNLP 2025) + **RAG-Anything** (HKUDS): RAG-Anything là hệ all-in-one xử lý tài liệu đan xen text, sơ đồ, bảng, công thức qua một interface. Từ 06/2026 LightRAG tích hợp native RAG-Anything, chạy được trên CPU/laptop.
- **KET-RAG**: multi-granular indexing, ghép "KG skeleton" nhẹ với graph text-based rẻ hơn. Giải bài toán chi phí dựng KG bằng LLM.
- **MKG-RAG-Bench** (arXiv 2606.26458): benchmark riêng cho retrieval trong multimodal KG-augmented generation.
- Liên quan tiếng Việt: *Applying Graph RAG: Enhancing Retrieval and Synthesis of Vietnamese Legal Text Information* (Springer 2025) báo cáo **+14.05% NDCG** và **+4.34% accuracy** so với RAG thường trên văn bản luật tiếng Việt. Đây là bằng chứng graph rất phù hợp cấu trúc trích dẫn chéo của luật.

### 4.3 Hybrid vẫn là nền

Dense + sparse (BM25) + rerank. Riêng với tiếng Việt, BM25 cần **word segmentation** (underthesea/pyvi/VnCoreNLP) mới có tác dụng thật. Có bài 2026 phản biện thú vị: *Rethinking Agentic Search with Pi-Serini* (arXiv 2605.10848) hỏi "lexical retrieval có đủ không?" cho agentic search, đáng đọc để không over-engineer.

---

## 5. Lớp reasoning: agentic RAG được huấn luyện

Đây là thay đổi lớn nhất về tư duy trong 2025-2026.

**Vấn đề của cách cũ:** 2 năm qua, chuẩn mực là RAG bọc trong một **agent loop viết tay**, control flow bằng prompt engineering. Model **không bao giờ học được policy**, và mỗi lần lại tự ứng biến sai.

**Cách mới:** ngừng script hóa vòng lặp, bắt đầu **train** nó. Agent được RL để tự quyết khi nào query, hỏi gì, và khi nào đã biết đủ để trả lời, reward dựa trên **kết quả cuối** chứ không theo script.

Bằng chứng: **Kimi-Researcher** (Moonshot) tăng từ 8.6% lên **26.9%** trên Humanity's Last Exam gần như hoàn toàn nhờ RL end-to-end. Không base model mới, không context lớn hơn, chỉ là **search policy được train tốt hơn**.

Các bài nên đọc:
- **RL Foundations for Deep Research Systems: A Survey** (arXiv 2509.06733). Vào đây trước.
- **AutoSearch** (arXiv 2604.17337): độ sâu search thích ứng cho agentic RAG bằng RL.
- **SAAS** (arXiv 2605.29796): self-aware RL để **chống over-search**. Vấn đề thật: agent search quá nhiều, đốt token, giảm chất lượng.
- **CuSearch** (arXiv 2605.11611): curriculum rollout sampling theo search depth.
- **Agentic-R** (arXiv 2601.11888), **AgentIR** (arXiv 2603.04384): reasoning-aware retrieval cho deep research agent. Điểm chung: retriever cũng phải biết agent đang reason gì.
- Đặc thù multimodal: **AutothinkRAG** (arXiv 2603.05551) điều khiển theo độ phức tạp cho image-text; **TechRAG** (arXiv 2606.01613) agentic RAG multimodal có **evidence gating** cho tài liệu kỹ thuật.
- Deep Research agent hiện chạy **hơn 20 lượt retrieval** cho một task mà người làm mất nhiều giờ.

**Long context vs RAG, chốt lại:**
- Long context thắng: knowledge base < 200K token, tương đối ổn định, query cần reason **xuyên toàn bộ** corpus (đọc cả quyển sách).
- RAG thắng: corpus lớn, thay đổi liên tục, query volume cao, và **chi phí**. Số đo 2026: RAG ~$0.00008/query vs long context ~$0.10/query, tức **rẻ hơn ~1250 lần**. Long context mất 30%+ accuracy khi nội dung liên quan nằm giữa cửa sổ ("context rot", lost-in-the-middle).
- Kết luận: **hybrid**. Retrieve 50K-200K token, rồi long-context reason trên đó. "Pure RAG misses single-document reasoning; pure long context rots." Xem thêm *When Retrieval Succeeds and Fails* (arXiv 2510.09106).

---

## 6. Benchmark và evaluation

**Retrieval / end-to-end đa mô thức:**
- **ViDoRe V3** (ACL 2026, arXiv 2601.08620) -- **quan trọng nhất hiện nay**. Human-annotated, 10 dataset, 26.000 trang tài liệu, 3.099 query đã verify, mỗi query có **6 ngôn ngữ**, 12.000 giờ công annotation (relevance, bounding box, reference answer). Đo cross-lingual retrieval, answer generation, và visual grounding. Kết luận chính: visual retriever > textual retriever; late-interaction + textual rerank cải thiện rõ; context hybrid hoặc thuần visual cho answer tốt hơn. Vẫn yếu ở: phần tử **non-text**, query **open-ended**, và visual grounding chi tiết.
- **UniDoc-Bench** (arXiv 2510.03663): benchmark hợp nhất cho document-centric multimodal RAG.
- **MMDocRAG / MMDocIR** (arXiv 2505.16470): 4.055 QA do expert annotate, evidence chain **đa trang, đa mô thức**, và **answer ở dạng multimodal**. Đo cả retrieval, rerank, generation.
- **M2RAG** (ACM MM 2025): 4 task -- image captioning, multimodal QA, multimodal fact verification, image reranking.
- **Dyn-VQA** (arXiv 2411.02937): dynamic VQA + self-adaptive planning agent.
- **MMEB-V2**: 78 task cho embedding đa mô thức.
- **Document Haystacks** (arXiv 2411.16740): reasoning trên 1000+ tài liệu.

**Parsing:** OmniDocBench, MDPBench, ParseBench, OHRBench.

**Generation / an toàn:**
- Ragas 2026 đã hỗ trợ agentic workflow, tool use, text-to-SQL và **multimodal input**, thêm metric **Multimodal Faithfulness** và **Noise Sensitivity**. Faithfulness hoạt động bằng cách tách câu trả lời thành **atomic claim** rồi đối chiếu từng claim với context.
- Ba tầng phải đo riêng: retrieval (có lấy đúng chunk?), generation (có dùng đúng chunk?), end-to-end (câu trả lời có đúng, có grounded, có hữu ích?).
- **Citation quality** (citation có đúng, có đủ, có attribute chính xác) là bắt buộc với domain **luật** và y tế.
- Failure mode riêng của multimodal: **misaligned visual grounding** và bất nhất giữa evidence retrieve được và evidence được sinh ra. RAGAS/ARES chỉ mới có measure sơ khai.
- Framework: Ragas, TruLens, DeepEval.

---

## 7. Framework để dựng nhanh

| Framework | Thế mạnh | Phù hợp khi |
|---|---|---|
| **RAG-Anything** (HKUDS) | All-in-one multimodal: text + sơ đồ + bảng + công thức đan xen, một interface | Muốn có ngay 1 hệ multimodal đầy đủ để thử nghiệm |
| **LightRAG** (EMNLP 2025) | Graph RAG nhẹ, chạy CPU/laptop, 4 chiến lược chunking (Fix/Recursive/Vector/Paragraph), native RAG-Anything từ 06/2026 | Resource hạn chế, muốn graph |
| **RAGFlow** (infiniflow) | Trích xuất tài liệu phức tạp (bảng, visual) rất tốt, RAG + Agent, nhiều chat channel (Telegram, Discord, Feishu, Line) | Cần sản phẩm chatbot thật, deploy được |
| **Morphik** | ColPali-based, search hiểu nội dung **hình**, một endpoint cho image/PDF/video | Muốn vision-native retrieval mà không tự build |
| **LlamaIndex / txtai / R2R** | Đều xử lý được text, image, audio, video | Cần linh hoạt, tự ghép |
| **Crawl4AI + MCP** | Crawl → markdown, agent gọi trực tiếp qua MCP | Nhánh URL |

Gợi ý thực dụng: **RAGFlow** hoặc **RAG-Anything** làm xương sống, cắm **ColQwen3/ColModernVBERT** cho nhánh visual, **Whisper + MOSS-Transcribe-Diarize** cho audio, **Crawl4AI** cho URL, **ZipRerank/Rank-Nexus** làm reranker.

---

## 8. Khoảng trống nghiên cứu (nơi có thể đóng góp)

Từ chính các survey và benchmark, những gì **chưa** giải quyết:

1. **Efficiency ở scale.** Multi-vector visual retrieval đẹp nhưng đắt. Hướng: distillation (NanoVDR), pooling training-free, quantization + dynamic pruning.
2. **Fine-grained representation và visual grounding.** ViDoRe V3 nói thẳng: model hiện tại vẫn yếu ở phần tử non-text (chart, sơ đồ, công thức) và grounding chi tiết.
3. **Robustness và bias.** *Who is in the Spotlight* (2506.11063) cho thấy có bias hệ thống trong multimodal RAG. Chưa có phương pháp giảm thiểu chuẩn.
4. **Cross-modal alignment và reasoning.** Đây là thách thức riêng của multimodal RAG, không có ở RAG unimodal.
5. **Native audio retrieval ngoài tiếng Anh.** WavRAG/SpeechRAG chủ yếu tiếng Anh/Trung. **Chưa có cho tiếng Việt.**
6. **Over-search / cost control cho agentic RAG.** SAAS mới bắt đầu, còn nhiều chỗ làm.
7. **Benchmark tiếng Việt.** Có *Benchmarking Vietnamese Legal Knowledge of LLMs* (arXiv 2512.14554) và bài Graph RAG luật Việt, nhưng chính các tác giả nhận định: **thiếu benchmark chuẩn hóa, hợp nhất** cho workflow trợ lý pháp lý thực tế. Chưa có ViDoRe/MMDocIR phiên bản tiếng Việt.

### Liên hệ với hướng embedding tiếng Việt của bạn

Ba đề tài giao thoa rõ ràng, đi từ dễ tới khó:

- **(A) ColVi: visual document retriever cho văn bản pháp lý tiếng Việt.** Fine-tune ColQwen3 hoặc ColModernVBERT trên ảnh trang văn bản luật/công báo/bản scan tiếng Việt. Ăn khớp trực tiếp với pipeline hard-negative mining bạn đã có. Rủi ro thấp, đóng góp rõ (chưa ai làm), và tận dụng đúng lợi thế "data + hard negative" của bạn.
- **(B) Benchmark multimodal RAG pháp lý tiếng Việt.** Kiểu ViDoRe V3 thu nhỏ: trang scan + bảng biểu + query có verified answer + bounding box. Công sức annotation lớn nhưng giá trị cộng đồng cao và dễ được nhận.
- **(C) Native audio retrieval tiếng Việt.** Theo công thức jina-v5-omni: đóng băng text backbone tiếng Việt của bạn, chỉ train connector cho audio encoder. Chi phí train thấp bất ngờ. Trắng địa hoàn toàn.

(A) và (B) bổ trợ nhau: (B) cho bạn thước đo để chứng minh (A).

---

## 9. Lộ trình đọc đề xuất

**Tuần 1, lấy bản đồ:**
1. *Ask in Any Modality* (arXiv 2502.08826, bản mở rộng 06/2025 có thêm agentic interaction và audio-centric retrieval) + repo llm-lab-org/Multimodal-RAG-Survey.
2. *Scaling Beyond Context* (arXiv 2510.15253, ACL 2026): survey multimodal RAG cho document understanding, taxonomy theo domain / retrieval modality / granularity.
3. *A Survey of Multimodal RAG* (arXiv 2504.08748).

**Tuần 2, đi vào retrieval:**
4. ColPali (2407.01449) → *Late Interaction Overview* của Weaviate → ViDoRe V3 (2601.08620).
5. *Roles of MLLMs in Visually Rich Document Retrieval for RAG: A Survey* (arXiv 2601.03262).
6. Omni-Embed-Nemotron (2510.03458).

**Tuần 3, audio + video:**
7. WavRAG (2502.14727), SpeechRAG (2412.16500).
8. Video-RAG (OpenReview) → VideoRAG HKUDS → *Rethinking RAG in Long Videos* (2606.13141).

**Tuần 4, agentic + eval:**
9. *RL Foundations for Deep Research Systems* (2509.06733) → SAAS (2605.29796).
10. MegaRAG (2512.20626), MKG-RAG-Bench (2606.26458).
11. OCR Hinders RAG (2412.02592) + When Good OCR Is Not Enough (2605.00911).

---

---

## 9b. Kiến trúc đã chọn: Agentic RAG text-centric qua Cloud API

Quyết định: **OCR + STT quy hết về text, agentic loop chạy qua cloud API, không self-host VLM.** Đây là lựa chọn đúng cho production. Nhưng phải bù lại đúng chỗ, vì §2.1 và §2.2 vừa chứng minh rằng "quy về text" chính là chỗ mất mát.

### Nguyên tắc trung tâm: text-first, media-on-demand

Sai lầm phổ biến là hiểu "quy về text" thành "**xóa** ảnh và audio sau khi parse". Đúng phải là:

```
INDEX TIME (rẻ, một lần)        →  chỉ index TEXT
  page → OCR → markdown + metadata{page_id, bbox, confidence}
  audio → STT → transcript + metadata{audio_id, t_start, t_end, speaker}
  BLOB gốc (ảnh trang, đoạn audio) lưu object storage, KHÔNG xóa

QUERY TIME (chỉ 3-5 chunk tới được LLM)
  retrieve trên text (hybrid BM25 + dense) → rerank
  → nếu chunk là bảng/biểu đồ/scan xấu (confidence thấp): ĐÍNH ẢNH TRANG vào prompt
  → nếu chunk là audio và câu hỏi cần số/tên riêng: đính đoạn audio hoặc n-best
  → LLM sinh câu trả lời + citation trỏ về page_id/timestamp
```

Vì sao đây là chìa khóa chi phí: bạn **không** trả tiền VLM cho 50.000 trang lúc index. Bạn trả tiền vision chỉ cho **3-5 trang thực sự tới LLM mỗi query**. Đó là chênh lệch 4 bậc độ lớn, và bạn vẫn lấy lại phần lớn chất lượng mà ViDoRe V3 nói visual retrieval mang lại.

Trong agentic framing, cái này thành một **tool**: `view_page_image(page_id)` và `play_audio_segment(audio_id, t_start, t_end)`. Agent tự quyết khi nào cần nhìn. Đúng tinh thần §5.

### Bù mất mát ở nhánh tài liệu (OCR)

1. **Confidence routing.** Parser rẻ chạy hết. Trang có confidence thấp (ảnh chụp, scan lệch, có dấu/chữ ký) route sang VLM. Thực tế 10-20% trang cần route, tức bạn trả 10-20% chi phí VLM full.
2. **Giữ cấu trúc bảng.** Bảng phải ra markdown hoặc HTML, tuyệt đối không flatten thành dòng text. Đây là nguồn lỗi số 1 trong OHRBench.
3. **Giữ bbox.** Cho phép citation chính xác và cho phép crop đúng vùng thay vì gửi cả trang (giảm token vision).
4. **Late chunking** (§3.3) cho văn bản luật, vì trích dẫn chéo "theo Điều 5 Khoản 2" bị đứt khi chunk độc lập.

### Bù mất mát ở nhánh audio (STT)

Đây là nhánh cần đầu tư nhiều hơn, vì mất 26 điểm EM như ViSQA đo.

1. **Lexicon biasing / hotword.** Hầu hết cloud STT API hỗ trợ phrase hints hoặc custom vocabulary. Nạp từ điển thuật ngữ pháp lý tiếng Việt + tên cơ quan + số hiệu văn bản vào đây. **Đây là can thiệp rẻ nhất và hiệu quả nhất**, vì nó đánh trực diện vào failure mode named-entity.
2. **N-best indexing.** Index top-3 đến top-5 hypothesis thay vì chỉ top-1. Kỹ thuật cũ nhưng vẫn hiệu quả, tăng recall đáng kể với chi phí storage gần như bằng không. Nếu ASR không chắc "Nghị định 15" hay "Nghị định 50", index cả hai.
3. **LLM post-correction có lexicon.** Chạy một lượt LLM rẻ sửa transcript, có đưa từ điển domain vào context (xem *Ontology Memory-Augmented ASR Correction*, arXiv 2606.13464). Với Claude Haiku 4.5 chi phí không đáng kể.
4. **Diarization làm cấu trúc.** MOSS-Transcribe-Diarize hoặc pipeline tương đương cho ranh giới lượt nói. Chunk theo lượt nói, không theo cửa sổ cố định.
5. **Đo entity WER, không đo WER.** Metric của bạn phải là "có bao nhiêu % số hiệu văn bản và tên cơ quan được transcribe đúng", không phải WER tổng.
6. **Ngưỡng quyết định:** nếu WER trên audio thật của bạn **> 40%**, cascade đã hết hiệu quả và cần xem native audio retrieval (SpeechDPR/WavRAG). Dưới 40%, cascade + 5 mitigation trên là đúng.

### Chi phí thật (tính với Claude API, giá tháng 7/2026)

Giả định 50.000 trang tài liệu, 500 giờ audio.

| Hạng mục | Cách làm | Chi phí |
|---|---|---|
| Parse 50k trang, VLM toàn bộ, Opus 5 | ~2000 token in + ~1200 out/trang, $5/$25 per MTok | **~$2.000** một lần |
| Cùng vậy nhưng Haiku 4.5 | $1/$5 per MTok | **~$400** một lần |
| Haiku 4.5 + Batch API (giảm 50%) | | **~$200** một lần |
| Confidence routing (15% trang qua VLM) | Haiku + batch | **~$30** một lần |
| Vision on-demand lúc query | 3 ảnh trang ≈ 6.000 token in/query | ~$0.03/query (Opus 5), ~$0.006 (Haiku) |
| LLM post-correct transcript | Haiku 4.5, 500h ≈ 4.5M từ | vài chục USD một lần |

Con số đáng chú ý: **parse toàn bộ 50k trang bằng VLM chỉ tốn $200-2.000 và chỉ tốn một lần.** Nếu parsing kém làm bạn mất 10 điểm accuracy trên **mọi query, mãi mãi**, thì đây là khoản rẻ nhất trong cả hệ thống. Chi phí VLM lớn là ở **retrieval multi-vector** (ColPali: 1024 vector/trang, cần GPU và storage lớn), không phải ở parsing. Bạn nên tránh cái thứ hai, không nên tránh cái thứ nhất.

Khuyến nghị: **confidence routing + Haiku 4.5 + Batch API**. Khoảng $30 cho 50k trang, gần như free, và bạn vẫn có VLM ở đúng những trang cần.

### Lớp agentic

Dùng tool use với vòng lặp agent. Tool tối thiểu:

| Tool | Mục đích |
|---|---|
| `search_documents(query, filters)` | Hybrid BM25 + dense trên text đã parse |
| `search_audio(query, filters)` | Trên transcript, trả kèm timestamp + speaker |
| `view_page_image(page_id, bbox?)` | Vision on-demand, đây là chỗ bù OCR loss |
| `play_audio_segment(audio_id, t0, t1)` | Audio on-demand, bù ASR loss |
| `fetch_url(url)` | Crawl4AI, ingest nóng URL người dùng dán |
| `get_document_structure(doc_id)` | Mục lục, giúp agent điều hướng văn bản dài |

Lưu ý triển khai với Claude API:
- Dùng **Tool Runner** (`client.beta.messages.tool_runner`) thay vì viết tay vòng lặp. Nó vẫn cho bạn hook để gate/log/sửa kết quả từng lượt.
- **Prompt caching** là đòn tiết kiệm lớn nhất: system prompt + tool definitions + lexicon pháp lý phải nằm ở prefix ổn định, có `cache_control`. Cache read rẻ ~10x. Tuyệt đối không nhét timestamp hay session ID vào system prompt (làm invalidate toàn bộ prefix).
- **Adaptive thinking** (`thinking: {type: "adaptive"}`) + `effort` để điều tiết độ sâu. Với RAG pháp lý, `high` là mức tối thiểu hợp lý.
- **Citation bắt buộc** với domain luật. Mỗi claim phải trỏ được về page_id/bbox hoặc audio timestamp. Đây cũng là cách bạn phát hiện lỗi OCR/ASR lúc chạy thật.
- Chống over-search: agentic RAG rất dễ đốt token. Xem SAAS (arXiv 2605.29796) và cân nhắc **task budget** (`output_config.task_budget`, beta `task-budgets-2026-03-13`, tối thiểu 20k token) để agent tự tiết chế.

### Đề tài nghiên cứu mà lựa chọn này mở ra

Đi text-centric thì bỏ hướng ColVi (visual retriever). Nhưng nó mở ra một hướng **khớp hơn** với dự án embedding tiếng Việt của bạn:

> **Noise-robust Vietnamese legal embedding: huấn luyện retriever bền với lỗi OCR và ASR.**

Ý tưởng: trong contrastive training, augment positive bằng **biến thể đã bị làm nhiễu theo đúng phân bố lỗi thật** của OCR và ASR tiếng Việt (không phải nhiễu random). Cụ thể:
- Lỗi OCR tiếng Việt: nhầm dấu (ẩ/â/á), nhầm ký tự hình dạng giống, bảng bị flatten
- Lỗi ASR tiếng Việt: nhầm số hiệu văn bản, đồng âm, mất dấu, biên từ sai
- Cộng thêm augment no-diacritics mà bạn đã ghi nhận trước đó

Vì sao đây là đề tài tốt:
- Trùng khớp hoàn toàn với pipeline contrastive + hard negative mining bạn đang có. Chỉ thêm một bước augmentation.
- Lợi thế của bạn vẫn là **data**, không phải algorithm, đúng như bạn đã kết luận.
- Chưa ai làm cho tiếng Việt, và ViSQA đã cung cấp sẵn cặp audio sạch/nhiễu để đánh giá.
- Nó **giải trực tiếp** cái vấn đề bạn vừa đồng ý là có thật, thay vì né nó.
- Chi phí train thấp, không cần GPU lớn, phù hợp với việc bạn đã chọn không self-host VLM.

Baseline để so: retriever thường trên transcript nhiễu (dự kiến tụt như ViSQA đo) so với retriever noise-robust trên cùng transcript đó.

---

## 10. Nguồn

**Survey và tổng quan**
- [Ask in Any Modality: A Comprehensive Survey on Multimodal RAG (arXiv 2502.08826)](https://arxiv.org/abs/2502.08826) | [site](https://multimodalrag.github.io/) | [repo](https://github.com/llm-lab-org/Multimodal-RAG-Survey)
- [A Survey of Multimodal RAG (arXiv 2504.08748)](https://arxiv.org/abs/2504.08748)
- [Scaling Beyond Context: MRAG for Document Understanding (arXiv 2510.15253)](https://arxiv.org/pdf/2510.15253)
- [Roles of MLLMs in Visually Rich Document Retrieval for RAG (arXiv 2601.03262)](https://arxiv.org/pdf/2601.03262)
- [RL Foundations for Deep Research Systems: A Survey (arXiv 2509.06733)](https://arxiv.org/pdf/2509.06733)
- [awesome-ai-agent-papers 2026](https://github.com/VoltAgent/awesome-ai-agent-papers)

**Tài liệu / parsing**
- [OCR Hinders RAG (arXiv 2412.02592)](https://arxiv.org/pdf/2412.02592) · [When Good OCR Is Not Enough (arXiv 2605.00911)](https://arxiv.org/pdf/2605.00911)
- [OmniDocBench (CVPR 2025)](https://github.com/opendatalab/OmniDocBench) · [MinerU](https://github.com/opendatalab/mineru) · [MDPBench (arXiv 2603.28130)](https://arxiv.org/html/2603.28130v1) · [ParseBench (arXiv 2604.08538)](https://arxiv.org/pdf/2604.08538)
- [The Definitive Guide to OCR in 2026](https://slavadubrov.github.io/blog/2026/03/04/the-definitive-guide-to-ocr-in-2026-from-pipelines-to-vlms/) · [Best Document Parsing Tools 2026](https://mixpeek.com/curated-lists/best-document-parsing-tools)

**Visual retrieval / late interaction**
- [ColPali (arXiv 2407.01449)](https://arxiv.org/abs/2407.01449) · [Late Interaction Overview, Weaviate](https://weaviate.io/blog/late-interaction-overview) · [Late Interaction Retrieval, Mixpeek](https://mixpeek.com/guides/late-interaction-retrieval)
- [Hierarchical Patch Compression for ColPali (arXiv 2506.21601)](https://arxiv.org/pdf/2506.21601) · [Visual RAG Toolkit (arXiv 2602.12510)](https://arxiv.org/pdf/2602.12510) · [NanoVDR (arXiv 2603.12824)](https://arxiv.org/pdf/2603.12824)
- [Argus-Retriever (arXiv 2606.04300)](https://arxiv.org/pdf/2606.04300) · [LITTA (arXiv 2603.26683)](https://arxiv.org/pdf/2603.26683) · [Spatially-Grounded Document Retrieval (arXiv 2512.02660)](https://arxiv.org/pdf/2512.02660) · [Beyond the Grid (arXiv 2603.01666)](https://arxiv.org/pdf/2603.01666)

**Embedding hợp nhất**
- [Omni-Embed-Nemotron (arXiv 2510.03458)](https://arxiv.org/abs/2510.03458) · [Fusion Embedding (arXiv 2607.18666)](https://arxiv.org/html/2607.18666) · [VLM2Vec-V2 (arXiv 2507.04590)](https://arxiv.org/pdf/2507.04590)
- [Omnimodal Embeddings guide](https://mixpeek.com/guides/omnimodal-embeddings) · [Best Multimodal Embedding Models 2026](https://mixpeek.com/curated-lists/best-multimodal-embedding-models)

**Audio: lỗi ASR lan truyền xuống RAG (quan trọng)**
- [On the Impact of Speech Recognition Errors in Passage Retrieval for Spoken QA (arXiv 2209.12944)](https://arxiv.org/pdf/2209.12944) · [ViSQA: Vietnamese spoken QA benchmark](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12795348/) · [Error Propagation in Korean Spoken QA with ASR-LLM Cascades (arXiv 2605.17443)](https://arxiv.org/html/2605.17443)
- [SpeechDPR (arXiv 2401.13463)](https://arxiv.org/pdf/2401.13463) · [Word error rate is broken (AssemblyAI, 2026)](https://www.assemblyai.com/blog/word-error-rate-is-broken) · [Attention-guided Evidence Grounding for Spoken QA (arXiv 2603.16292)](https://arxiv.org/pdf/2603.16292) · [Speech-to-Speech RAG overview](https://www.emergentmind.com/topics/speech-to-speech-retrieval-augmented-generation-s2s-rag)

**Audio**
- [WavRAG (ACL 2025, arXiv 2502.14727)](https://arxiv.org/abs/2502.14727) · [SpeechRAG (ICASSP 2025, arXiv 2412.16500)](https://arxiv.org/html/2412.16500) · [SRAG (IEEE)](https://ieeexplore.ieee.org/document/10546001/)
- [Voxtral (arXiv 2507.13264)](https://arxiv.org/pdf/2507.13264) · [Voxtral vs Whisper 2026](https://weesperneonflow.ai/en/blog/2026-03-31-voxtral-whisper-open-source-speech-models-comparison-2026/) · [Best Open ASR Models 2026](https://www.marktechpost.com/2026/07/23/best-open-speech-recognition-asr-models-in-2026-wer-languages-latency-and-license-compared/)
- [Vietnamese ASR: A Revisit (EACL 2026)](https://aclanthology.org/2026.findings-eacl.345.pdf) · [Hearing More with Less (arXiv 2508.01166)](https://arxiv.org/pdf/2508.01166) · [MURMUR (arXiv 2606.01483)](https://arxiv.org/pdf/2606.01483) · [Ontology Memory-Augmented ASR Correction (arXiv 2606.13464)](https://arxiv.org/pdf/2606.13464)

**Video**
- [VideoRAG over Video Corpus (Findings ACL 2025)](https://aclanthology.org/2025.findings-acl.1096/) · [VideoRAG HKUDS (KDD 2026)](https://github.com/HKUDS/VideoRAG) · [Video-RAG (OpenReview)](https://openreview.net/forum?id=QaZxGWlbgO)
- [AdaVideoRAG (arXiv 2506.13589)](https://arxiv.org/pdf/2506.13589) · [Multi-RAG (arXiv 2505.23990)](https://arxiv.org/abs/2505.23990) · [Rethinking RAG in Long Videos (arXiv 2606.13141)](https://arxiv.org/pdf/2606.13141)
- [Structured Memory for Ultra-Long Agentic Video Reasoning (arXiv 2605.08271)](https://arxiv.org/pdf/2605.08271) · [CRAFT (arXiv 2605.19075)](https://arxiv.org/pdf/2605.19075) · [MARQUIS (arXiv 2605.17640)](https://arxiv.org/pdf/2605.17640)
- [Who is in the Spotlight: Hidden Bias in Multimodal RAG (arXiv 2506.11063)](https://arxiv.org/pdf/2506.11063)

**URL / web**
- [Crawl4AI guide, ScrapingBee](https://www.scrapingbee.com/blog/crawl4ai/) · [Crawl4AI vs Firecrawl 2026](https://www.capsolver.com/blog/AI/crawl4ai-vs-firecrawl) · [Firecrawl vs Crawl4AI for RAG](https://alterlab.io/blog/firecrawl-vs-crawl4ai-web-scraping-for-rag) · [Best Web Extraction Tools for AI 2026](https://www.firecrawl.dev/blog/best-web-extraction-tools)

**Rerank**
- [MM-R5 (arXiv 2506.12364)](https://arxiv.org/pdf/2506.12364) · [MLLM Is a Strong Reranker (arXiv 2407.21439)](https://arxiv.org/abs/2407.21439) · [Rank-Nexus (arXiv 2601.20623)](https://arxiv.org/html/2601.20623) · [ZipRerank (arXiv 2605.11864)](https://arxiv.org/pdf/2605.11864) · [miniReranker (arXiv 2606.10759)](https://arxiv.org/html/2606.10759)

**Graph / KG**
- [MegaRAG (ACL 2026, arXiv 2512.20626)](https://arxiv.org/abs/2512.20626) · [MKG-RAG-Bench (arXiv 2606.26458)](https://arxiv.org/html/2606.26458v1) · [Multimodal-GraphRAG repo](https://github.com/Sh-31/Multimodal-GraphRAG) · [GraphRAG 2026 guide](https://www.articsledge.com/post/graphrag-retrieval-augmented-generation)

**Agentic RAG + RL**
- [AutoSearch (arXiv 2604.17337)](https://arxiv.org/html/2604.17337v1) · [SAAS (arXiv 2605.29796)](https://arxiv.org/pdf/2605.29796) · [CuSearch (arXiv 2605.11611)](https://arxiv.org/pdf/2605.11611) · [Agentic-R (arXiv 2601.11888)](https://arxiv.org/pdf/2601.11888) · [AgentIR (arXiv 2603.04384)](https://arxiv.org/pdf/2603.04384)
- [Pi-Serini: Is Lexical Retrieval Sufficient? (arXiv 2605.10848)](https://arxiv.org/pdf/2605.10848) · [AutothinkRAG (arXiv 2603.05551)](https://arxiv.org/pdf/2603.05551) · [TechRAG (arXiv 2606.01613)](https://arxiv.org/pdf/2606.01613)
- [How AI Agents Learned to Think: the RL recipe behind Agentic RAG (07/2026)](https://buzzgrewal.medium.com/how-ai-agents-learned-to-think-the-reinforcement-learning-recipe-behind-agentic-rag-and-deep-0cd3663c9bf6)

**Benchmark / eval**
- [ViDoRe V3 (ACL 2026, arXiv 2601.08620)](https://arxiv.org/html/2601.08620v2) · [UniDoc-Bench (arXiv 2510.03663)](https://arxiv.org/pdf/2510.03663) · [MMDocRAG (arXiv 2505.16470)](https://arxiv.org/html/2505.16470v2) · [M2RAG (ACM MM 2025)](https://dl.acm.org/doi/abs/10.1145/3746027.3755625) · [Dyn-VQA (arXiv 2411.02937)](https://arxiv.org/abs/2411.02937) · [Document Haystacks (arXiv 2411.16740)](https://arxiv.org/pdf/2411.16740)
- [RAG Evaluation 2026: frameworks, metrics, gates](https://futureagi.com/blog/what-is-rag-evaluation-2026/) · [RAGAS/TruLens/DeepEval so sánh](https://atlan.com/know/llm-evaluation-frameworks-compared/) · [Ragas Faithfulness guide 2026](https://qaskills.sh/blog/ragas-faithfulness-answer-relevancy-guide)

**Chunking**
- [Best Chunking Strategies for RAG 2026, Firecrawl](https://www.firecrawl.dev/blog/best-chunking-strategies-rag) · [RAG Chunking Strategies 2026 playbook](https://www.digitalapplied.com/blog/rag-chunking-strategies-2026-retrieval-quality-playbook) · [8 methods compared](https://denser.ai/blog/rag-chunking-strategies/)
- [Query-Adaptive Semantic Chunking (arXiv 2605.22834)](https://arxiv.org/pdf/2605.22834) · [SLIDE (arXiv 2503.17952)](https://arxiv.org/pdf/2503.17952) · [Semantic Entanglement in Vector Retrieval (arXiv 2604.17677)](https://arxiv.org/pdf/2604.17677)

**Long context vs RAG**
- [When Retrieval Succeeds and Fails (arXiv 2510.09106)](https://arxiv.org/pdf/2510.09106) · [RAG vs long context: 2026 data](https://usewire.io/blog/long-context-vs-rag-what-the-data-shows/) · [Context Rot, RAG, and Long Context](https://glasp.co/articles/context-rot-rag-long-context-hybrid) · [Production decision framework](https://tianpan.co/blog/2026-04-09-long-context-vs-rag-production-decision-framework)

**Framework**
- [RAG-Anything](https://github.com/hkuds/rag-anything) · [LightRAG (EMNLP 2025)](https://github.com/hkuds/lightrag) · [RAGFlow](https://github.com/infiniflow/ragflow) · [Morphik](https://github.com/ZadeFrontier/morphik-core-multi-modal-rag) · [15 Best Open-Source RAG Frameworks 2026](https://www.firecrawl.dev/blog/best-open-source-rag-frameworks)

**Tiếng Việt**
- [Benchmarking Vietnamese Legal Knowledge of LLMs (arXiv 2512.14554)](https://arxiv.org/html/2512.14554) · [Applying Graph RAG to Vietnamese Legal Text (Springer)](https://link.springer.com/chapter/10.1007/978-3-031-98164-7_25)
