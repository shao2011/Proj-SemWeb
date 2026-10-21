---
title: 'Thiết kế database'
type: 'reference'
created: '2026-08-18'
status: 'living'
context:
  - 'document/techstack.md'
  - 'document/rag-workflow/1.ingestion.md'
---

# Thiết kế database

Hai store, mỗi cái một loại dữ liệu:

| Store | Giữ gì | Nguồn sự thật cho |
|---|---|---|
| **Postgres** (Supabase) | project, file, thread, message, memory, user, usage | metadata và trạng thái |
| **Qdrant** (Qdrant Cloud) | vector dense + sparse của từng chunk, payload trích dẫn | nội dung đã index |

Hai cái phụ, không phải database: Redis (cache embedding và retrieval, mất là embed lại), disk dưới `MEDIA_ROOT` (markdown đã render và ảnh trang, không tái tạo được).

---

## 1. Kết nối

### Biến môi trường

| Biến | Giá trị | Default |
|---|---|---|
| `DATABASE_URL` | chuỗi kết nối Postgres | `postgresql://postgres:password@localhost:5432/rag` |
| `QDRANT_URL` | endpoint Qdrant | `http://localhost:6333` |
| `QDRANT_API_KEY` | key của cluster managed, để trống khi self-host | rỗng |
| `QDRANT_COLLECTION_PREFIX` | tiền tố tên collection | `rag` |
| `EMBEDDING_PROFILE_VERSION` | hậu tố tên collection | `v2` |
| `REDIS_URL` | cache, tắt khi không set | rỗng |
| `MEDIA_ROOT` | thư mục media | `media` |
| `TEST_DATABASE_URL` | Postgres cho pytest, tách khỏi `DATABASE_URL` | như `DATABASE_URL` default |

`compose.yaml` đặt default là service local cho `DATABASE_URL` và `QDRANT_URL`. Ghi hai biến đó trong `.env` là trỏ sang cloud, để trống là quay về local.

### Chuỗi kết nối Supabase

```
postgresql://postgres.<project-ref>:<db-password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

Bốn ràng buộc, thiếu cái nào cũng không connect được:

- **Host là pooler**, không phải `db.<ref>.supabase.co`. Host direct chỉ có bản ghi AAAA, container Docker không có route IPv6.
- **Port 5432**, session mode. Port 6543 là transaction mode, không giữ session state mà DDL của Alembic cần.
- **User là `postgres.<project-ref>`**. Pooler định tuyến tenant bằng phần sau dấu chấm.
- **`sslmode=require`**.

`region` lấy trong dashboard Supabase, mục Connect. Không suy ra từ tên project.

### Engine

`build_engine` ([db/session.py](../../backend/rag_ingestion/db/session.py)): `pool_size=5`, `max_overflow=5`, `pool_pre_ping=True`, `pool_recycle=1800`.

### Migration

Alembic là chủ schema. `migrations/env.py` đọc URL từ `RuntimeConfig`, tức cùng `DATABASE_URL` mà app dùng.

```bash
cd backend && ../.venv/bin/python -m alembic upgrade head
```

`create_runtime_app(create_tables=True)` có gọi `target_metadata.create_all`, nhưng nó không ghi `alembic_version`. Chỉ dùng cho dev local.

---

## 2. Schema Postgres

15 bảng. Mọi timestamp là `timestamptz`; store làm việc bằng UTC tz-aware và API serialize offset.

### `projects`

| Cột | Kiểu | |
|---|---|---|
| `project_id` | varchar(64) | PK |
| `owner_user_id` | varchar(255) | index |
| `name` | varchar(200) | |
| `visibility` | varchar(16) | `private` |
| `embedding_profile` | varchar | JSON, ghim provider và số chiều. DB coi là opaque, validate bằng `PinnedEmbeddingProfile` lúc đọc |
| `icon` | varchar(32) | NULL |
| `color` | varchar(16) | NULL |
| `created_at`, `updated_at` | timestamptz | |

Profile đã ghim thì không đổi được: vector của project nằm trong collection của provider đó.

### `project_members`

| Cột | Kiểu | |
|---|---|---|
| `member_id` | varchar(64) | PK |
| `project_id` | varchar(64) | FK `projects` CASCADE |
| `email` | varchar(320) | index, lưu đã normalize |
| `role` | varchar(16) | `viewer` đọc và hỏi, `editor` thêm upload, trash, rename |
| `created_at` | timestamptz | |

Unique `(project_id, email)`. Khoá theo địa chỉ chứ không theo uid, vì owner nhập địa chỉ và người được thêm có thể chưa đăng ký. Access đối chiếu với địa chỉ **đã verify** trên token.

Owner cố tình không có dòng ở đây: quyền sở hữu nằm ở `projects.owner_user_id`, hai nơi thì sẽ lệch nhau.

### `project_files`

| Cột | Kiểu | |
|---|---|---|
| `file_id` | varchar(64) | PK |
| `project_id` | varchar(64) | FK `projects` CASCADE, index |
| `filename` | varchar(512) | |
| `content_hash` | varchar(128) | dedupe theo nội dung |
| `doc_id` | varchar(128) | NULL cho tới khi parse xong |
| `status` | varchar(16) | |
| `error` | varchar | NULL |
| `trashed_at` | timestamptz | NULL là đang sống |
| `created_at`, `updated_at` | timestamptz | |

`trashed_at` có giá trị: ẩn khỏi retrieval, vector và media vẫn giữ, nên upload lại đúng nội dung đó trong TTL thì restore chứ không embed lại.

### `threads`

| Cột | Kiểu | |
|---|---|---|
| `thread_id` | varchar(64) | PK |
| `owner_user_id` | varchar(255) | index |
| `project_id` | varchar(64) | NULL cho chat thường |
| `title` | varchar(200) | |
| `sequence` | bigint | khoá sắp recency, lấy từ sequence `threads_recency_seq` |
| `summary` | varchar | NULL. L2: các turn đã trôi khỏi cửa sổ replay |
| `summarized_through_position` | int | watermark, mọi message ở dưới đã nằm trong `summary` |
| `last_prompt_tokens` | int | provider đếm, là tử số của context gauge |
| `last_prompt_chars` | int | để hiệu chỉnh chars/token cho lượt sau |
| `last_model_id` | varchar(128) | tokenizer nào ra con số trên |
| `created_at`, `updated_at` | timestamptz | |

Index `threads_owner_sequence_idx (owner_user_id, sequence)`.

`sequence` là số nguyên chứ không phải timestamp: độ phân giải clock trên một số máy cho nhiều lần ghi cùng một giá trị, thứ tự recents khi đó thành tuỳ ý.

### `thread_messages`

| Cột | Kiểu | |
|---|---|---|
| `message_id` | varchar(64) | PK |
| `thread_id` | varchar(64) | FK `threads` CASCADE, index |
| `role` | varchar(16) | |
| `content` | varchar | |
| `citations` | varchar | JSON |
| `position` | int | thứ tự hội thoại |
| `created_at` | timestamptz | |

Unique `(thread_id, position)`. Thứ tự lưu tường minh vì Postgres không có rowid ngầm như SQLite.

### `thread_shares`

| Cột | Kiểu | |
|---|---|---|
| `share_id` | varchar(64) | PK, đồng thời là token trong URL nên phải mint không đoán được |
| `thread_id` | varchar(64) | FK `threads` CASCADE |
| `owner_user_id` | varchar(255) | index |
| `title` | varchar(200) | copy lúc share |
| `shared_through_position` | int | watermark |
| `access` | varchar(16) | `link` ai có URL cũng đọc, `invite` chỉ địa chỉ trong `thread_share_recipients` |
| `created_at`, `updated_at` | timestamptz | |

Unique `(thread_id)`: mỗi thread một link sống. Revoke là xoá dòng, nên share lại sẽ mint token mới và link cũ chết.

`shared_through_position` và `title` copy sang đây để link là **snapshot**: position chỉ được append, và đổi tên chat sau đó không được đổi thứ mà link đã phát ra.

### `thread_share_recipients`

| Cột | Kiểu | |
|---|---|---|
| `recipient_id` | varchar(64) | PK |
| `share_id` | varchar(64) | FK `thread_shares` CASCADE |
| `email` | varchar(320) | index, lưu đã normalize |
| `created_at` | timestamptz | |

Unique `(share_id, email)`. Index trên `email` vì "cái gì được share cho tôi" là tra theo địa chỉ.

### `message_reports`

| Cột | Kiểu | |
|---|---|---|
| `report_id` | varchar(64) | PK |
| `owner_user_id` | varchar(255) | index |
| `thread_id` | varchar(64) | index, **không** phải FK |
| `message_position` | int | NULL khi report cả chat |
| `reason` | varchar(32) | |
| `details` | text | NULL |
| `conversation` | text | JSON list `{role, content}`, chụp phía server lúc report |
| `created_at` | timestamptz | |

Không FK và lưu hội thoại inline có chủ đích: report mà bằng chứng biến mất khi chat bị xoá thì không còn là bằng chứng.

### `memory`

| Cột | Kiểu | |
|---|---|---|
| `memory_id` | varchar(64) | PK |
| `owner_user_id` | varchar(255) | index |
| `scope` | varchar(16) | `thread`, `project`, `user` |
| `thread_id` | varchar(64) | NULL, FK `threads` CASCADE |
| `project_id` | varchar(64) | NULL, FK `projects` CASCADE |
| `type` | varchar(16) | |
| `key` | varchar(255) | |
| `value` | varchar | |
| `source` | varchar(32) | |
| `superseded_by` | varchar(64) | NULL |
| `last_used_at` | timestamptz | NULL |
| `deleted_at` | timestamptz | NULL. Đánh dấu, không xoá cứng, để ghi sai vẫn truy được |
| `created_at`, `updated_at` | timestamptz | |

Ba index:

| Index | Cột | Để làm gì |
|---|---|---|
| `memory_owner_scope_idx` | `owner_user_id, scope, deleted_at` | đường đọc: một query cho cả ba scope |
| `memory_owner_key_idx` | `owner_user_id, key` | đếm số thread phân biệt theo key, để promote memory local lên global |
| `memory_live_key_idx` | `owner_user_id, scope, coalesce(thread_id,''), coalesce(project_id,''), key`, unique, `WHERE deleted_at IS NULL` | **một dòng sống cho mỗi key mỗi scope** |

`memory_live_key_idx` là partial vì dòng superseded và deleted được giữ lại và lặp key một cách chính đáng. Cặp `coalesce` là bắt buộc: trong unique index NULL không bao giờ bằng NULL, nên dòng scope `user` sẽ không đụng nhau. DB ép invariant này, không chỉ dựa vào pass supersede trong store.

CASCADE trên `thread_id` là bắt buộc: memory scope thread mà sống lâu hơn thread của nó thành dòng không bao giờ load lại được.

### `memory_proposal`

| Cột | Kiểu | |
|---|---|---|
| `proposal_id` | varchar(64) | PK |
| `owner_user_id` | varchar(255) | index |
| `thread_id` | varchar(64) | FK `threads` CASCADE |
| `suggested_scope` | varchar(16) | |
| `type`, `key`, `value` | varchar(16) / varchar(255) / varchar | |
| `source_message_id` | varchar(64) | NULL |
| `created_at` | timestamptz | |

### `memory_audit`

| Cột | Kiểu | |
|---|---|---|
| `audit_id` | varchar(64) | PK |
| `memory_id` | varchar(64) | index, **không** phải FK |
| `op` | varchar(16) | ADD / UPDATE / DELETE |
| `source` | varchar(32) | |
| `source_message_id` | varchar(64) | NULL |
| `sequence` | int | thứ tự trail |
| `created_at` | timestamptz | |

Không FK vì trail phải sống lâu hơn thứ nó ghi. `sequence` vì `created_at` không đủ: hai lần ghi trùng giá trị thì thứ tự ADD/UPDATE thành tuỳ ý.

### `users`

| Cột | Kiểu | |
|---|---|---|
| `uid` | varchar(255) | PK |
| `email` | varchar(320) | NULL |
| `display_name` | varchar(200) | NULL |
| `first_login_at`, `last_login_at` | timestamptz | |

Mọi identity từng xuất trình token hợp lệ. Đây là thứ làm cho việc claim account an toàn phía server: uid có trong bảng này thuộc về một account thật, không thể bị claim thành history ẩn danh dù client khai gì.

### `user_settings`

| Cột | Kiểu | |
|---|---|---|
| `owner_user_id` | varchar(255) | PK |
| `memory_enabled` | bool | default true |
| `display_name` | varchar(200) | NULL với khách ẩn danh |
| `email` | varchar(320) | NULL với khách ẩn danh |
| `created_at`, `updated_at` | timestamptz | |

### `llm_usage`

| Cột | Kiểu | |
|---|---|---|
| `owner_user_id` | varchar(255) | PK |
| `prompt_tokens`, `completion_tokens`, `total_tokens` | bigint | |
| `requests` | int | |
| `updated_at` | timestamptz | |

Accumulator, không phải log từng call: màn hình settings chỉ cần tổng, và một dòng mỗi user thì không phình vô hạn.

### `alembic_version`

`version_num varchar(32)`.

---

## 3. Schema Qdrant

### Collection

Tên: `<QDRANT_COLLECTION_PREFIX>-<provider>-<EMBEDDING_PROFILE_VERSION>`, ví dụ `rag-jina-v1`.

Một collection cho nhiều project. Tách tenant bằng payload filter `owner_user_id` + `project_id`, không tách bằng collection. Mỗi provider một collection riêng vì số chiều và không gian vector khác nhau.

Vector:

| Tên | Loại | Cấu hình |
|---|---|---|
| `dense` | dense | size theo `profile.dimensions`, distance COSINE |
| `sparse` | sparse | modifier IDF |

`config.metadata.embedding_profile` giữ profile của collection. `ensure_collection` so với profile của project và ném lỗi nếu lệch, nên một collection không bao giờ chứa hai loại vector.

Point id là UUID suy ra từ `chunk_id` (deterministic), nên index lại cùng nội dung là ghi đè chính point đó chứ không sinh bản trùng.

### Payload

| Field | |
|---|---|
| `chunk_id`, `doc_id`, `owner_user_id`, `project_id` | định danh và tenant |
| `level` | leaf / section / document |
| `domain`, `language` | |
| `source_text` | text gốc của chunk |
| `source_block_ids`, `source_span`, `locators` | neo trích dẫn về block, offset, và bbox trang |
| `confidence`, `citable` | có được trích dẫn hay không |
| `parent_id`, `child_ids`, `section_path`, `breadcrumb` | cấu trúc cây |
| `refs`, `media_refs` | tham chiếu chéo, key media |
| `embedding_provider`, `embedding_model`, `embedding_version`, `embedding_mode`, `embedding_task` | profile đã dùng |

### Payload index: bắt buộc đủ

Bảy index, tạo trong `ensure_collection`:

| Field | Kiểu |
|---|---|
| `owner_user_id`, `project_id`, `level`, `chunk_id`, `doc_id`, `source_block_ids` | keyword |
| `citable` | bool |

**Luật: mọi field mà filter gọi tên đều phải có index ở đây.** Cluster managed bật strict mode (`unindexed_filtering_retrieve: false`, `unindexed_filtering_update: false`), filter trên field chưa index bị trả 400 chứ không fallback sang scan. Self-host không bật, nên thiếu index ở local chỉ chậm hơn và không lộ ra.

Thêm filter trên field mới thì thêm index trong cùng lần sửa. Test `test_every_field_a_filter_names_is_indexed` chốt danh sách này.

Đường search không gọi `ensure_collection` (read không trả round-trip tạo index), nên collection sinh ra bởi code cũ không tự có index mới. Vá tay:

```bash
curl -X PUT "$QDRANT_URL/collections/<name>/index" -H "api-key: $QDRANT_API_KEY" \
  -H 'Content-Type: application/json' -d '{"field_name":"citable","field_schema":"bool"}'
```
