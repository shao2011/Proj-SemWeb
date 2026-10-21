# Báo cáo đánh giá toàn bộ backend

**Ngày đánh giá:** 2026-08-08  
**Phạm vi:** `backend/` hiện tại, gồm application, ingestion, retrieval, storage, migrations, benchmark scripts và tests.  
**Ưu tiên:** tính đúng đắn logic. Security, style và formatting không nằm trong phạm vi.

## Kết luận nhanh

Backend có nền test khá tốt: **458 tests passed**. Các luồng chính như parse/chunk, global retrieval fallback, memory routing, summary watermark và API CRUD đều có coverage đáng kể.

Tuy nhiên, hệ thống **chưa sẵn sàng cho fresh deployment** vì migration chain bị hỏng. Ngoài ra còn các lỗi logic đáng chú ý ở history dài, memory promotion/concurrency, reranker validation và vector validation.

| Mức độ | Số lượng |
|---|---:|
| Critical | 1 |
| High | 6 |
| Medium | 6 |
| Low | 1 |

## Findings logic

### CR-01: Fresh migration bị hỏng vì migration tạo memory tables đang rỗng

**Mức độ:** Critical  
**Vị trí:** [82a8b357280a_memory_proposals_audit_user_settings.py](backend/migrations/versions/82a8b357280a_memory_proposals_audit_user_settings.py#L21), [cb5a1dde9f49_memory_audit_sequence.py](backend/migrations/versions/cb5a1dde9f49_memory_audit_sequence.py#L33)

Migration `82a8...` có `upgrade()` và `downgrade()` chỉ chứa `pass`, dù tên revision nói rằng nó tạo memory, proposals, audit và user settings. Revision kế tiếp gọi `ALTER TABLE memory_audit`, nhưng bảng này chưa tồn tại.

**Đã tái hiện:** chạy `alembic upgrade heads` trên PostgreSQL database tạm mới hoàn toàn thất bại với `psycopg2.errors.UndefinedTable: relation "memory_audit" does not exist`. Database tạm đã được xóa sau kiểm tra.

**Tác động:** deploy mới, CI dùng database sạch và disaster recovery từ migrations đều thất bại.

**Khuyến nghị:** bổ sung đầy đủ `op.create_table()`/constraints/indexes vào revision `82a8...`, sau đó thêm integration test chạy `alembic upgrade heads` từ database rỗng và kiểm tra downgrade/upgrade.

### HI-01: Thread trên 200 messages dùng history cũ nhất thay vì mới nhất

**Mức độ:** High  
**Vị trí:** [pg_threads.py](backend/rag_ingestion/storage/pg_threads.py#L174), [routes.py](backend/rag_ingestion/api/routes.py#L1172)

`list_messages()` sắp theo `position` tăng dần rồi áp `LIMIT 200`. Khi thread vượt 200 messages, hàm trả 200 messages đầu tiên. Route chat dùng kết quả này cho `UnderstandMessage`, history prompt và summary.

**Tác động:** câu hỏi gần nhất không còn context gần nhất; pronoun resolution, summary và câu trả lời dần dựa vào phần đầu của cuộc hội thoại. Summary watermark cũng có thể ngừng tiến sau mốc 200.

**Khuyến nghị:** lấy 200 rows mới nhất theo thứ tự giảm dần trong subquery, sau đó đảo lại về conversation order. Thêm test với ít nhất 201 messages.

### HI-02: Promote memory sang user scope bỏ qua cap và quy tắc supersede

**Mức độ:** High  
**Vị trí:** [pg_memory.py](backend/rag_ingestion/storage/pg_memory.py#L185)

`update_memory(..., promote_to_user=True)` chỉ đổi `scope`, xóa `thread_id/project_id` rồi commit. Hàm không kiểm tra user-scope cap và không supersede user memory đang có cùng key.

**Tác động:** có thể tạo hai memory user-scope còn live cùng key hoặc vượt cap. Khi recall, giá trị thắng phụ thuộc thứ tự row/memory id thay vì quy tắc nghiệp vụ.

**Khuyến nghị:** promotion phải dùng cùng transaction và invariant như `add_memory`: khóa scope đích, kiểm cap, supersede key trùng, audit toàn bộ thay đổi.

### HI-03: Confirm proposal không atomic và không idempotent

**Mức độ:** High  
**Vị trí:** [pg_memory.py](backend/rag_ingestion/storage/pg_memory.py#L383)

`confirm_proposal()` đọc proposal trong một session, gọi `add_memory()` trong transaction khác, rồi gọi `dismiss_proposal()` trong transaction thứ ba.

**Tác động:** crash sau `add_memory()` để lại proposal chưa dismiss. Hai request confirm đồng thời có thể cùng đọc proposal và ghi hai memory. Với cùng key, kết quả supersede/cap phụ thuộc race.

**Khuyến nghị:** lock proposal bằng `SELECT ... FOR UPDATE`, ghi memory và xóa/đánh dấu proposal trong một transaction. Confirm lại phải trả kết quả cũ hoặc trạng thái rõ ràng.

### HI-04: Memory cap và supersede không an toàn khi ghi đồng thời

**Mức độ:** High  
**Vị trí:** [pg_memory.py](backend/rag_ingestion/storage/pg_memory.py#L84)

`add_memory()` đọc toàn bộ live rows, kiểm cap và supersede rồi mới insert, nhưng không lock scope và database không có unique constraint cho một live key trong scope.

**Tác động:** hai transaction có thể cùng vượt qua cap check, hoặc cùng tạo hai live rows cùng key. Đây là lỗi invariant, không chỉ là vấn đề thứ tự hiển thị.

**Khuyến nghị:** serialize write theo owner/scope/target bằng row/advisory lock; thêm partial unique index cho live key nếu schema cho phép; map conflict thành retry hoặc response xác định.

### HI-05: Reranker index âm chọn nhầm chunk cuối

**Mức độ:** High  
**Vị trí:** [chain.py](backend/rag_ingestion/retrieval/chain.py#L228)

Validation chỉ chặn duplicate và `index >= len(pool)`. `index=-1` đi qua rồi `pool[-1]` hợp lệ trong Python.

**Tác động:** response provider lỗi có thể âm thầm chọn chunk cuối, tạo answer/citation sai thay vì fail rõ.

**Khuyến nghị:** validate `0 <= result.index < len(pool)` trước khi đọc pool. Thêm test cho index âm, duplicate và out-of-range.

### HI-06: Dense vector NaN/Infinity không bị chặn

**Mức độ:** High  
**Vị trí:** [qdrant_index.py](backend/rag_ingestion/indexing/qdrant_index.py#L169), [qdrant_index.py](backend/rag_ingestion/indexing/qdrant_index.py#L292), [embedding_cache.py](backend/rag_ingestion/providers/embedding_cache.py#L72)

Index và query chỉ kiểm tra dimension. Cache nhận ra vector delegate không hợp lệ nhưng vẫn trả vector đó cho caller.

**Tác động:** Qdrant có thể reject request giữa ingestion/query hoặc nhận dữ liệu làm similarity không xác định. Batch ingestion thất bại sau một provider response malformed.

**Khuyến nghị:** kiểm `math.isfinite()` cho mọi phần tử ở cả document và query path, raise provider-contract error trước khi gọi Qdrant.

### ME-01: OCR có thể trả page ngoài phạm vi và làm parse PDF crash

**Mức độ:** Medium  
**Vị trí:** [datalab.py](backend/rag_ingestion/ingestion/parsers/datalab.py#L162), [pdf.py](backend/rag_ingestion/ingestion/parsers/pdf.py#L199)

Datalab page id không nằm trong `page_map` được chuyển thành `int(raw_page) + 1`. `parse_pdf()` sau đó truy cập `probes[item.page - 1]` mà không kiểm bounds.

**Tác động:** response OCR lệch page numbering hoặc malformed làm toàn bộ file fail bằng `IndexError`.

**Khuyến nghị:** chỉ chấp nhận page thuộc tập đã request; reject/skip block ngoài phạm vi với lỗi provider rõ ràng.

### ME-02: Parse rỗng được báo confidence 1.0

**Mức độ:** Medium  
**Vị trí:** [pdf.py](backend/rag_ingestion/ingestion/parsers/pdf.py#L203), [image.py](backend/rag_ingestion/ingestion/parsers/image.py#L74)

`min(..., default=1.0)` làm tài liệu không có block mang confidence tuyệt đối. Empty PDF hoặc OCR trả rỗng có thể đi tiếp với `blocks=[]` và `overall_confidence=1.0`.

**Tác động:** trạng thái “không extract được gì” bị biểu diễn thành “hoàn toàn tin cậy”, làm sai metrics và UI.

**Khuyến nghị:** coi zero-block là parse failure hoặc confidence `0.0`; API phải trả lý do cụ thể.

### ME-03: Datalab payload chưa được validate trước khi ép kiểu

**Mức độ:** Medium  
**Vị trí:** [datalab.py](backend/rag_ingestion/ingestion/parsers/datalab.py#L89), [datalab.py](backend/rag_ingestion/ingestion/parsers/datalab.py#L162)

Initial/poll payload được dùng `.get()` mà không chắc là object. `raw_page`, `bbox` và `confidence` được ép `int/float` trực tiếp.

**Tác động:** response provider hợp lệ về JSON nhưng sai shape tạo `AttributeError`, `TypeError` hoặc `ValueError`, khó phân biệt với bug nội bộ.

**Khuyến nghị:** validate response bằng model/schema riêng và chuyển mọi mismatch thành `ProviderResponseError` có context.

### ME-04: Locator cho phép trạng thái hình học/thời gian vô nghĩa

**Mức độ:** Medium  
**Vị trí:** [models.py](backend/rag_ingestion/models.py#L47), [models.py](backend/rag_ingestion/models.py#L64)

`PageBBoxLocator` không bắt `x0 <= x1`, `y0 <= y1`; `TimeSpanLocator` không bắt `t0 <= t1`.

**Tác động:** provenance vẫn pass schema nhưng crop/playback có thể sai hoặc rỗng.

**Khuyến nghị:** thêm model validators và negative tests cho reversed/negative geometry.

### ME-05: Thread recency sequence dùng `max + 1` không có khóa toàn cục

**Mức độ:** Medium  
**Vị trí:** [pg_threads.py](backend/rag_ingestion/storage/pg_threads.py#L209)

Hai thread khác nhau có thể cùng đọc một `max(sequence)` và nhận cùng sequence. Lock trên một thread chỉ bảo vệ message position trong chính thread đó, không bảo vệ global recency counter.

**Tác động:** thứ tự recent threads bị hòa và phụ thuộc execution plan.

**Khuyến nghị:** dùng PostgreSQL sequence/identity hoặc counter row được lock atomically.

### ME-06: API chấp nhận PIN memory nhưng PIN không có tác dụng

**Mức độ:** Medium  
**Vị trí:** [memory.py](backend/rag_ingestion/storage/memory.py#L31), [routes.py](backend/rag_ingestion/api/routes.py#L201), [answering.py](backend/rag_ingestion/retrieval/answering.py#L63)

`MemoryType.PIN` được public request schema và storage chấp nhận, nhưng answering loại PIN khỏi prompt và retrieval không chuyển PIN thành `doc_ids` filter.

**Tác động:** API trả thành công cho một thiết lập không thay đổi hành vi hệ thống.

**Khuyến nghị:** hoặc triển khai PIN filter ở retrieval, hoặc loại PIN khỏi public API cho tới khi feature hoàn chỉnh.

### LO-01: App không đóng tài nguyên DB/Qdrant do chính nó tạo

**Mức độ:** Low  
**Vị trí:** [app.py](backend/rag_ingestion/api/app.py#L167), [app.py](backend/rag_ingestion/api/app.py#L182), [app.py](backend/rag_ingestion/api/app.py#L284)

App đăng ký shutdown cho executor và Redis do nó sở hữu, nhưng không dispose engine hoặc close Qdrant client do nó tự tạo.

**Tác động:** restart/test app lặp lại có thể giữ connection/socket lâu hơn cần thiết.

**Khuyến nghị:** track ownership cho cả engine và Qdrant, chỉ close những instance app tự tạo.

## Sai lệch giữa code và tài liệu

### DOC-01: `suggested_scope` đã bỏ khỏi code nhưng còn xuất hiện xuyên suốt thiết kế

[2.multi-turn-memory.md](document/rag-workflow/2.multi-turn-memory.md#L199) vẫn yêu cầu `suggested_scope`, mô tả nhánh write/proposal theo field này và dùng nó trong diagram/metrics. Code hiện quyết định scope từ key trong `memory_routing.py`; schema chỉ bỏ qua field cũ để tương thích.

**Cần làm:** sửa toàn bộ schema, ví dụ, diagram, metrics và trust-boundary trong tài liệu. Ghi rõ scope do deterministic router quyết định.

### DOC-02: Tài liệu vừa nói UnderstandMessage chạy song song, vừa ghi code đang tuần tự

[2.multi-turn-memory.md](document/rag-workflow/2.multi-turn-memory.md#L294) mô tả U chạy song song với retrieval/generation, nhưng status cuối tài liệu ghi node vẫn chạy tuần tự. Code thực tế gọi `understand()` đồng bộ trước retrieval và stream generation.

**Cần làm:** đánh dấu phần song song là target architecture, hoặc cập nhật flow hiện tại và ảnh hưởng latency.

### DOC-03: Audio, video và URL là thiết kế mục tiêu, chưa có implementation

[1.ingestion.md](document/rag-workflow/1.ingestion.md#L13) và [rag.md](document/rag.md#L11) mô tả pipeline cho audio/video/URL. Router/model hiện chỉ hỗ trợ PDF, DOCX, XLSX, PPTX và image.

**Phân loại:** roadmap gap, không phải regression. Tài liệu nên có bảng trạng thái “implemented / planned” ngay đầu để tránh hiểu đây là contract production hiện tại.

### DOC-04: Tech stack chọn Gemini embedding nhưng runtime không hỗ trợ Gemini

[techstack.md](document/techstack.md#L25) chọn Gemini Embedding 2. Code hiện hỗ trợ Cohere, Voyage và Jina.

**Phân loại:** quyết định kiến trúc/tài liệu chưa đồng bộ. Cần hoặc cập nhật tech stack theo runtime, hoặc ghi Gemini là planned provider.

### DOC-05: `Document + Block[]` được gọi là nguồn sự thật nhưng không được persist

Tài liệu ingestion coi `Block[]` là source of truth có provenance. Backend chỉ giữ chúng trong memory trong lúc parse/chunk; database không có `documents`/`blocks` tables, còn Qdrant payload không đủ tái tạo toàn bộ block graph.

**Phân loại:** roadmap/data-retention gap. Cần ghi rõ retention model hiện tại hoặc bổ sung persistence nếu audit/reparse là yêu cầu thật.

### DOC-06: Failure mode của Understand/Summary chưa được mô tả rõ

Code cố ý degrade về rewrite/memory rỗng hoặc summary cũ khi provider lỗi. Đây là hành vi quan trọng vì API vẫn trả answer thành công nhưng memory/summary có thể âm thầm không cập nhật.

**Cần làm:** thêm fault-tolerance contract, logs/metrics cần theo dõi và kỳ vọng retry.

### DOC-07: PIN được mô tả như retrieval filter nhưng backend hiện là no-op

Tài liệu có Q0 “filter pin”; code chấp nhận PIN nhưng không áp filter. Cần đánh dấu chưa triển khai hoặc hoàn thiện đường PIN-to-doc_ids.

## Khoảng trống test quan trọng

1. Không có test chạy toàn bộ Alembic chain trên database sạch. Đây là lý do `CR-01` tồn tại dù 458 tests xanh.
2. Không có test cho thread hơn 200 messages.
3. Không có test concurrency/idempotency cho confirm proposal, memory cap/supersede và global thread sequence.
4. Không có test reranker trả negative index hoặc duplicate index.
5. Không có test vector `NaN/Infinity` từ delegate embedder ở Qdrant path.
6. Không có test empty PDF/OCR output rỗng, OCR page ngoài phạm vi hoặc locator đảo chiều.

Không nên thêm test chỉ để tăng coverage %. Mỗi test trên gắn trực tiếp với một invariant đang bị vi phạm.

## Xác minh đã chạy

### Pytest

```text
458 passed, 3 warnings in 62.17s
```

Warnings:

- Starlette `TestClient` cảnh báo `httpx` integration cũ đã deprecated.
- Một test không lấy được Qdrant server version.
- Local Qdrant cảnh báo payload indexes không có tác dụng.

### Fresh migration

Chạy `alembic upgrade heads` trên PostgreSQL database tạm mới tạo:

```text
Running upgrade 6b64a4f82cd9 -> 82a8b357280a
Running upgrade 82a8b357280a -> cb5a1dde9f49
psycopg2.errors.UndefinedTable: relation "memory_audit" does not exist
```

Database tạm đã được xóa sau kiểm tra.

## Điểm tốt đã xác nhận

- Config dùng Pydantic bounds và cross-field validation khá chặt; các cảnh báo slice `-0` từ review tự động là false positive.
- Retrieval giữ global fallback bằng round-robin candidate allocation và có test regression.
- Summary watermark có test incremental, không fold lại cùng messages.
- Memory extraction lỗi không làm hỏng answer path, đúng tinh thần graceful degradation.
- Upload state machine, parser contracts, chunk hierarchy và owner-scoped CRUD có coverage thực tế, không chỉ test constructor.

## Thứ tự xử lý đề xuất

1. Sửa migration chain và thêm fresh-database test.
2. Sửa history trên 200 messages.
3. Gom toàn bộ memory write/promotion/confirm invariants vào transaction có lock/constraint rõ.
4. Chặn reranker index âm và vector không finite.
5. Siết parser/provider boundaries và locator invariants.
6. Đồng bộ tài liệu, đặc biệt `suggested_scope`, trạng thái modality và PIN.