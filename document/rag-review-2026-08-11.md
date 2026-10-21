# Báo cáo: Khảo sát chatbot + đánh giá luồng RAG eslothai-chat

Ngày 11/08/2026. Chỉ khảo sát và đánh giá, chưa sửa code.
Nguồn: phân tích trực tiếp codebase (`backend/rag_ingestion`) + khảo sát 9 sản phẩm với 25 nguồn công khai.

## 1. Tóm tắt cho người ra quyết định

Hệ RAG hiện tại **tốt hơn mặt bằng open-source** ở phần lõi retrieval: đã có hybrid search + cross-encoder rerank (chuẩn ngành), contextual chunking dạng breadcrumb, auto-merge phân cấp, citation lưu kèm message và click-through về đúng vị trí trong tài liệu. Kiến trúc project = partition trong 1 collection Qdrant, embedding profile pin theo project — đúng thiết kế.

Nhưng có 4 nhóm vấn đề chặn việc đưa ra user thật:

1. **Bảo mật:** danh tính chỉ là header `X-User-ID` client tự đặt. Ai cũng đọc/ghi được dữ liệu của bất kỳ ai.
2. **Toàn vẹn dữ liệu:** xóa project không xóa vector; không xóa/re-index được từng file; kết quả auto-merge (phần đắt nhất của pipeline) không bao giờ vào prompt.
3. **Trải nghiệm sản phẩm:** thiếu 3 thứ mà mọi đối thủ đều có — chế độ full-context cho project nhỏ (kiểu Claude Projects), custom instructions theo project, và pin tài liệu quan trọng.
4. **Hiệu năng:** 18–24 round-trip Qdrant thừa mỗi câu hỏi, embed query 3–4 lần, bước understand chạy tuần tự chặn streaming.

## 2. Khảo sát sản phẩm tương tự

Tất cả sản phẩm khảo sát đều hội tụ về cùng một mô hình dữ liệu: **Project/Workspace → {files, instructions, memory scope} → nhiều chat/thread**. Khác biệt nằm ở chiến lược retrieval và UX citation.

### ChatGPT Projects (OpenAI)

- Project gom chats + files + custom instructions riêng (đè instructions toàn cục khi ở trong project).
- Memory 2 chế độ, chọn lúc tạo và không đổi được: dùng memory toàn tài khoản, hoặc "project-only memory" cô lập hoàn toàn.
- Retrieval: **RAG-first**, cùng máy với API `file_search`: rewrite query, tách query phức thành nhiều search song song, keyword + semantic kết hợp, rồi rerank. Mặc định API: chunk 800 token, overlap 400, embedding text-embedding-3-large 256 chiều, tối đa 20 chunk (~16k token) mỗi lượt.
- Giới hạn file mỗi project: Free 5, Plus 25, Pro 40; 512MB/file, trích xuất ~2M token/file. (Con số theo gói là từ báo cáo bên thứ ba.)
- Citation mức tên file — yếu hơn NotebookLM.

### Claude Projects (Anthropic)

- Project knowledge + instructions riêng theo project.
- **Thiết kế đáng học nhất: dual-mode tự động.** Knowledge base nhỏ → nhét toàn bộ vào context (~200k), không mất mát do retrieval. Vượt ngưỡng → tự bật chế độ RAG (UI hiện "Indexing"), sức chứa tăng ~10 lần. Nhỏ lại → tự quay về full-context. User không cấu hình gì.
- Khuyến nghị kỹ thuật chính thức của Anthropic: KB dưới ~200k token (~500 trang) thì *bỏ RAG*, đưa hết vào prompt kèm prompt caching.

### Google NotebookLM

- Notebook → sources → chat chỉ trả lời từ sources; check/uncheck từng source theo từng câu hỏi.
- **Citation là chuẩn vàng:** số inline, click nhảy đến đúng đoạn được highlight trong source viewer.
- Giới hạn: 50 sources (free) / 300 (Pro) / 600 (Ultra); 500.000 từ hoặc 200MB mỗi source.
- Nhờ context 1–2M token của Gemini, notebook nhỏ gần như không cần chunk; retrieval chỉ kích hoạt khi source lớn. (Suy đoán từ phân tích bên thứ ba, Google không công bố.)

### Notion AI & Microsoft 365 Copilot

- Điểm chung đáng học: **retrieval nhận biết quyền truy cập** — ACL kiểm tra tại thời điểm query, index không bao giờ mở rộng quyền. Mọi câu trả lời đều cite kèm link về nguồn.

### Open-source

| Sản phẩm | Mô hình knowledge | Retrieval | Điểm đáng chú ý |
|---|---|---|---|
| Open WebUI | Workspace → Knowledge (bộ file), gắn vào chat hoặc bind vào Model | Hybrid BM25 + vector, cross-encoder rerank, chunk mặc định ~500, cấu hình admin đầy đủ | Có chế độ "Full Context" thủ công theo knowledge; cộng đồng đang xin auto-switch kiểu Claude (chưa có) |
| LibreChat | File theo file_id, RAG là microservice riêng (pgvector + LangChain) | Vector thuần, top-k 3, chunk khuyến nghị 1500/150; có cờ full-context | Đơn giản nhất: không hybrid, không rerank mặc định |
| AnythingLLM | Workspace = namespace vector + chat + settings; thread chia hội thoại trên cùng knowledge | Vector + tùy chọn theo từng tài liệu | **Pin document:** tài liệu ghim được nhét nguyên văn vào context mọi lượt — lối thoát khi RAG bỏ sót |
| Onyx (Danswer) | Connectors → indexing workers → Vespa (vector + text); "Document Sets" để scope | Hybrid dense + BM25 song song → cross-encoder rerank → citations về bản gốc | Tham chiếu enterprise chuẩn nhất về pipeline |

## 3. Pattern chung của ngành 2025–2026

1. **Dual-mode với ngưỡng tự động là pattern thắng cuộc.** Full-context khi knowledge còn vừa (ngưỡng tham khảo ~200k token), tự chuyển RAG khi vượt. Claude ship sẵn; Open WebUI/AnythingLLM bắt user tự chọn; ChatGPT thì RAG-first.
2. **Hybrid search + rerank là mức sàn** cho nhánh RAG. Số liệu Anthropic: contextual embeddings giảm 35% lỗi retrieval; thêm contextual BM25 → 49%; thêm rerank → 67% (5,7% → 1,9%).
3. **Contextual chunking:** gắn 50–100 token mô tả "chunk này nằm đâu trong tài liệu" trước khi embed và index BM25. Chunk size phổ biến: OpenAI 800/400, Open WebUI 500, LibreChat 1500/150.
4. **Rewrite + decomposition query** trước retrieval, chạy song song.
5. **Citation mức đoạn văn, click được, nhảy đến đoạn highlight.** Citation mức tài liệu bị coi là tụt hậu trong 2026.
6. **Instructions và memory scope theo project**, đè cấu hình toàn cục.
7. **Retrieval theo quyền truy cập** khi có nhiều user: ACL tại query-time.
8. **Lối thoát khi RAG hỏng:** pin/full-context theo từng tài liệu quan trọng.

## 4. Luồng RAG hiện tại của eslothai-chat

### Stack

| Lớp | Công nghệ | Vị trí |
|---|---|---|
| API | FastAPI + uvicorn, SSE streaming | `api/app.py:160`, `api/routes.py:502` |
| Metadata | Postgres 16, SQLModel, Alembic (5 revision) | `db/tables.py` |
| Vector | Qdrant, 1 collection/provider, named vectors dense (cosine) + sparse (IDF) | `indexing/qdrant_index.py:158-170` |
| Embedding/Rerank | Cohere embed-v4.0 / Voyage voyage-4 / Jina v4 (1024d), rerank tương ứng; pin theo project lúc tạo | `config.py:190-200`, `providers/adapters.py` |
| Generation | OpenRouter, 3 slot model (generate / understand / summary) | `providers/openrouter.py:23` |
| OCR | Datalab, submit + poll | `ingestion/parsers/datalab.py:90` |
| Cache | Redis: cache embedding query + cache nguyên outcome retrieval | `providers/embedding_cache.py`, `retrieval/cache.py` |

### Ingestion

- Định dạng: PDF (phân loại từng trang born-digital/scanned, chỉ OCR trang scan), DOCX/XLSX/PPTX (native, không OCR; XLSX giữ cả công thức lẫn giá trị; PPTX kèm speaker notes), ảnh (OCR toàn file). **Audio/video/URL mô tả kỹ trong thiết kế nhưng chưa tồn tại.**
- Reconciliation qua trang: bóc boilerplate lặp, nối caption↔figure, nối bảng/đoạn văn bị cắt trang (`ingestion/reconciliation.py:86`).
- Chunking structure-first 3 tầng: leaf (min 100 / target 256 / max 512 "token", overlap 0) → SECTION → DOCUMENT. Chunk đặc biệt (bảng, hình, code, slide...) giữ nguyên khối. `index_text` = breadcrumb + nguyên văn — dạng rẻ của Contextual Retrieval.
- ID deterministic (sha256) → re-ingest nội dung y hệt sẽ ghi đè tại chỗ.
- Upload: sync qua ThreadPoolExecutor 2 worker, kết quả stream SSE từng file. **Không có queue, không có job record bền** — request HTTP sống suốt cả batch.

### Retrieval (Q0–Q6)

1. **Q0** soft route: hybrid search trên SECTION/DOCUMENT lấy 5 `doc_id` — không hard filter.
2. **Q1**: 3 search leaf — scoped theo doc route, scoped theo query đã rewrite, và luôn có 1 search toàn cục (30 mỗi nguồn).
3. **Q2**: khử trùng lặp rồi chia quota round-robin về 60 ứng viên.
4. **Q3**: cross-encoder rerank của provider (bắt buộc).
5. **Q4**: auto-merge kiểu HiChunk (gộp leaf lên parent theo 3 điều kiện) + mở rộng tham chiếu 1 bước.
6. **Q6**: resolve về leaf citable, giữ thứ tự rerank. (Q5 definition pinning: chưa có.)

Hybrid = dense + sparse cục bộ, fuse bằng RRF, hard filter `owner_user_id` + `project_id`. Prompt: evidence đánh số `[n] breadcrumb` đặt ở user turn cuối để giữ prefix cache. Citation = danh sách chunk đã đưa vào prompt, lưu kèm message; click-through qua endpoint markdown có anchor block-level.

### Chat thường vs chat project

Một route duy nhất, khác nhau đúng 1 nhánh `if thread.project_id is not None` (`routes.py:1480`): có project thì chạy Q0–Q6 và emit event `sources`; không thì evidence rỗng. Cả hai chế độ đều chạy LLM `UnderstandMessage` mỗi message (rewrite query + trích memory), memory 3 scope (thread/project/user) với supersede theo key, summary hội thoại gấp dần sau mỗi lượt. `project_id` gán lúc tạo thread, không đổi được — đúng thiết kế.

## 5. Điểm mạnh

- Hybrid + rerank + contextual chunking — đủ 3 thứ Anthropic đo được là giảm 67% lỗi retrieval. Nhiều sản phẩm open-source chưa có đủ bộ này (LibreChat chẳng hạn).
- Citation lưu bền kèm message + click-through về đúng block trong markdown — gần chuẩn NotebookLM hơn phần lớn self-hosted.
- Ingestion Office giữ cấu trúc thật (bảng dạng HTML, công thức + giá trị Excel, speaker notes) thay vì flatten text.
- Memory 4 tầng (buffer / summary / thread / project / user scope) có audit trail, soft delete, supersede theo key — vượt mức trung bình.
- Có eval harness MTRAG với Recall@k / nDCG@k tự viết — đa số dự án không có gì.
- Embedding profile bất biến theo collection, ownership check ở Postgres trước Qdrant, payload filter là lớp phòng thủ thứ hai.
- Thiết kế trong `document/rag-workflow` trung thực: tự đánh dấu status từng phần, tự liệt kê gap. Phần lớn thiết kế cốt lõi đã build đúng như mô tả.

## 6. Điểm yếu, xếp theo mức độ

### Nghiêm trọng — chặn production

1. **Danh tính là header client tự đặt.** `X-User-ID` chỉ được trim, không xác thực (`routes.py:540-544`); frontend sinh UUID vào localStorage (`frontend/src/lib/storage.ts:3-9`). Bất kỳ client nào cũng đọc/ghi project, thread, tài liệu, memory của người khác. Thiết kế thừa nhận đây là posture dev, nhưng chưa có gì thay thế.
2. **Xóa project không xóa vector.** `delete_project` chỉ xóa row Postgres (`pg_projects.py:110-134`); không có lệnh delete nào tới Qdrant trong toàn bộ backend. Chunk mồ côi nằm mãi trong collection chung.
3. **Không xóa / re-index được từng tài liệu.** Không tồn tại route delete document; `content_hash` tính ra, lưu vào, không bao giờ đọc lại → upload trùng tạo row thứ hai.
4. **Kết quả auto-merge không bao giờ vào prompt.** `context_chunks` — đầu ra của Q4, được docstring mô tả là "cái đem đi generate" — không được route nào dùng (`chain.py:24-26` vs `routes.py:641-651`, `:1494-1501`). Phần văn bản liền mạch của parent bị vứt; mất luôn lý do tồn tại của HiChunk.
5. **LOG_LEVEL mặc định DEBUG in nguyên văn evidence + memory cá nhân** ra stdout (`config.py:42`, `routes.py:1541-1556`, `:1638-1648`). Kèm theo: Postgres password mặc định `password` publish cổng 5432 ra mọi interface trong `compose.yaml`; không CORS, không rate-limit.

### Cao — sai lệch chất lượng retrieval

6. **Sparse không phải BM25.** Term count thô, hash 32-bit có collision, không length-normalization, không saturation (`qdrant_index.py:108-133`). Ngành coi BM25 thật + rerank là mức sàn; rerank có rồi, nửa BM25 thì chưa.
7. **"Token" = đếm từ theo whitespace** (`chunker.py:69`) và **tách câu chỉ theo `[.!?]`** (`chunker.py:66`). Với tiếng Thái/CJK — đúng thị trường của sản phẩm tên "eslothai" — budget sai lệch lớn và câu bị cắt giữa chừng. Language detection hoàn toàn không tồn tại: `Block.language` luôn `None`.
8. **Không MMR/diversity** — chunk gần trùng nhau có thể chiếm cả top-k. Q0 vứt bỏ danh tính SECTION, chỉ giữ `doc_id` (`chain.py:170`). Hierarchy SECTION phẳng theo tuple chính xác nên cây sâu thành anh em ngang hàng (`chunker.py:380-382`).
9. **Tài liệu bị thay thế (`supersedes`/`effective_date`) vẫn được cite như hiện hành** — thiết kế tự gọi đây là "lỗ hổng nghiêm trọng nhất còn lại".

### Trung bình — hiệu năng & vận hành

10. `ensure_collection()` chạy mỗi lần search, mỗi lần bắn 6 lệnh `create_payload_index(wait=True)` → **18–24 round-trip Qdrant đồng bộ cho mỗi câu hỏi** (`qdrant_index.py:171-184`, `:325`).
11. Query bị embed lại 3–4 lần mỗi request (mỗi `hybrid_search` tự embed); chỉ đỡ khi có Redis.
12. `UnderstandMessage` chạy tuần tự, chặn trước byte đầu tiên của câu trả lời (`routes.py:1438-1446`) — thiết kế yêu cầu chạy song song.
13. Embedding không batch: tài liệu sinh >96 chunk sẽ fail với Cohere (giới hạn /v2/embed) — đã ghi nhận trong IMPLEMENT.md, chưa sửa. Không retry 429.
14. Upload không có queue/job bền — client disconnect giữa batch là mất; Datalab poll bằng `time.sleep` chiếm 1 trong 2 worker tới 30s/tài liệu scan.
15. Không có metrics runtime (chỉ log line); ~30 counter trong thiết kế chưa tồn tại. Eval MTRAG mới đo Q1+Q3, mới chạy Cohere trên 2.000 passages — tham số chunking hiện là mặc định chưa kiểm chứng.

### Ghi chú thêm

- Prompt injection từ tài liệu chưa có phòng vệ (evidence nội suy thô vào user turn).
- `Document`/`Block[]` không được lưu bền → không thể lazy re-parse; sidecar markdown là workaround một phần.
- Hai chỗ code cố ý làm khác thiết kế và có ghi chú: memory scope user được *ghi thẳng* thay vì đề xuất chờ xác nhận (bỏ mất trust boundary của thiết kế, `memory/routing.py:43-57`); pattern clause theo keyword (Điều/Article/§) bị bỏ, chỉ còn số thứ tự.

## 7. So khớp với ngành: khoảng cách sản phẩm

| Pattern ngành | eslothai-chat | Nhận xét |
|---|---|---|
| Dual-mode: full-context khi nhỏ, RAG khi lớn | ❌ RAG cho mọi project, kể cả 1 file 2 trang | **Gap sản phẩm lớn nhất.** Project nhỏ đang chịu rủi ro retrieval-miss không cần thiết trong khi Claude/AnythingLLM/Open WebUI đều có lối full-context |
| Custom instructions theo project | ❌ Chưa có | ChatGPT, Claude, Open WebUI (qua Model) đều có; bảng `ProjectRow` hiện chưa có cột nào cho việc này |
| Pin tài liệu quan trọng vào context | ❌ `MemoryType.PIN` bị API từ chối vì chưa có gì đọc nó | Lối thoát chống RAG-miss của AnythingLLM/Open WebUI |
| Hybrid + rerank | 🟡 Có, nhưng sparse chưa phải BM25 | Nửa keyword đang yếu hơn mọi đối thủ dùng BM25 thật |
| Contextual chunking | ✅ Breadcrumb prefix | Bản rẻ (không LLM-generated) nhưng đúng hướng |
| Query rewrite trước retrieval | 🟡 Có, nhưng chạy tuần tự chặn stream | OpenAI chạy song song |
| Citation passage-level click được | 🟡 Có click-through markdown + anchor; nhưng citation = mọi chunk đã show, không phải chunk thực dùng | Trên mức ChatGPT, dưới NotebookLM |
| Memory scope theo project | ✅ 3 scope thread/project/user | Ngang ChatGPT project-only memory, hơn phần lớn open-source |
| Permission-aware retrieval | ❌ Không có xác thực nên chưa bàn được ACL | — |

## 8. Thứ tự ưu tiên đề xuất (chỉ đề xuất, chưa làm gì)

1. **Xác thực thật** thay `X-User-ID` — điều kiện tiên quyết cho mọi thứ hướng user.
2. **Vòng đời dữ liệu:** xóa vector khi xóa project, route xóa/re-index từng tài liệu, dùng `content_hash` để chặn upload trùng.
3. **Nối `context_chunks` vào prompt** — sửa 1 chỗ, lấy lại giá trị của toàn bộ tầng auto-merge.
4. **Dual-mode full-context/RAG theo ngưỡng token** + instructions theo project + pin tài liệu — 3 tính năng đưa sản phẩm ngang mặt bằng đối thủ.
5. **Sửa nền đa ngữ:** tokenizer thật thay đếm whitespace, tách câu có tiếng Thái/CJK, language detection — sản phẩm nhắm thị trường Thái thì đây không phải việc phụ.
6. **Hiệu năng:** bỏ `ensure_collection` khỏi đường search, embed query 1 lần, chạy understand song song, batch embedding + retry 429.

## 9. Nguồn

**Claude Projects:** support.claude.com/en/articles/11473015 · support.claude.com/en/articles/9517075 · anthropic.com/engineering/contextual-retrieval

**ChatGPT Projects:** help.openai.com/en/articles/10169521 · developers.openai.com/api/docs/guides/tools-file-search · folio3.ai/ai-pulse/openai-extends-chatgpt-projects-free-users · datastudios.org (giới hạn file theo gói) · techradar.com (project-only memory)

**NotebookLM:** support.google.com/notebooklm/answer/16269187 · elephas.app/blog/notebooklm-source-limits · jubinsoni.medium.com (phân tích kiến trúc)

**Notion/Copilot:** notion.com/help/notion-ai-connectors · notion.com/help/enterprise-search · learn.microsoft.com/en-us/microsoftsearch/semantic-index-for-copilot

**Open-source:** docs.openwebui.com/features/workspace/knowledge · github.com/open-webui/open-webui/discussions/19177 · librechat.ai/docs/features/rag_api · github.com/danny-avila/rag_api · docs.anythingllm.com/chatting-with-documents · deepwiki.com/Mintplex-Labs/anything-llm/8-workspace-system · onyx-dot-app-onyx.mintlify.app/architecture

**Codebase:** phân tích trực tiếp `backend/rag_ingestion`, `db`, `migrations`, `frontend/src`, `document/rag-workflow`, IMPLEMENT.md, CODE_REVIEW.md (đã đối chiếu — nhiều mục CODE_REVIEW.md đã được sửa và stale).

*Điểm chưa chắc chắn:* cơ chế nội bộ ChatGPT Projects suy ra từ API file_search; giới hạn file theo gói là báo cáo bên thứ ba; nội bộ NotebookLM từ phân tích ngoài; thông số LibreChat từ blog khách có cảnh báo trôi phiên bản.
