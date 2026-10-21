export type Provider = 'cohere' | 'voyage' | 'jina'

export type FileProcessingStatus = 'pending' | 'processing' | 'completed' | 'failed'

export interface PinnedEmbeddingProfile {
  provider: string
  model: string
  version: string
  dimensions: number
  task: string
  mode: string
}

/** What the caller may do in a project. Ordered weakest first. */
export type ProjectRole = 'viewer' | 'editor' | 'owner'

export interface Project {
  project_id: string
  owner_user_id: string
  name: string
  visibility: 'private'
  embedding_profile: PinnedEmbeddingProfile
  icon: string | null
  color: string | null
  created_at: string
  updated_at: string
  /** The caller's role. Shared projects arrive in the same list as your own. */
  role: ProjectRole
  /** People besides the owner. Non-zero means the project is shared. */
  member_count: number
}

export interface ProjectMember {
  email: string
  role: ProjectRole
}

export interface ProjectFile {
  file_id: string
  project_id: string
  owner_user_id: string
  filename: string
  content_hash: string
  doc_id: string | null
  status: FileProcessingStatus
  error: string | null
  created_at: string
  updated_at: string
}

export interface QueryHit {
  chunk_id: string
  doc_id: string
  score: number
  source_text: string
  source_block_ids: string[]
  source_span: number[] | null
  locators: Record<string, unknown>[]
  confidence: number
  citable: boolean
  parent_id: string | null
  section_path: string[]
  refs: string[]
  breadcrumb: string
}

export interface QueryResponse {
  query: string
  hits: QueryHit[]
  evidence_leaf_ids?: string[]
  routed_doc_ids?: string[]
  diagnostics?: Record<string, number>
}

export interface AnswerResponse extends QueryResponse {
  answer: string
  citations: string[]
}

/**
 * A file queued at project creation, tracked client-side from the moment its
 * upload starts until the request settles. Unlike `ProjectFile` it has no
 * backend record yet for files still waiting their turn in the batch.
 */
export interface PendingUpload {
  key: string
  projectId: string
  name: string
  status: 'uploading' | 'done' | 'failed'
}

export interface ProcessDocumentResponse {
  file_id: string
  doc_id: string
  filename: string
  source_type: string
  block_count: number
  chunk_count: number
  indexed_count: number
  status: FileProcessingStatus
}

export interface UploadFileResult {
  index: number
  filename: string
  status: 'completed' | 'failed'
  file_id: string | null
  doc_id: string | null
  source_type: string | null
  block_count: number | null
  chunk_count: number | null
  indexed_count: number | null
  detail: string | null
}

export interface UploadBatchSummary {
  total: number
  completed: number
  failed: number
}

export interface HealthResponse {
  status: string
  configured_providers: Provider[]
}

export interface Thread {
  thread_id: string
  owner_user_id: string
  project_id: string | null
  title: string
  created_at: string
  updated_at: string
  /** Server-computed context gauge (0-100). Null before the first measured turn. */
  context_used_percent?: number | null
  /** Model that produced the last measured turn; feeds the picker on reopen. */
  last_model_id?: string | null
}

export interface ThreadMessage {
  message_id: string
  thread_id: string
  role: 'user' | 'assistant'
  content: string
  citations: string[]
  created_at: string
  /** 1-based conversation order. Where a fork or a report points. */
  position: number
}

export interface ThreadDetail extends Thread {
  messages: ThreadMessage[]
}

/** `link` = anyone with the URL; `invite` = only the listed addresses. */
export type ShareAccess = 'link' | 'invite'

/** The owner's view of a share link. */
export interface ShareView {
  share_id: string
  /** How far into the thread the snapshot reaches. */
  shared_through_position: number
  /** The thread's length now: greater means the snapshot has fallen behind. */
  message_count: number
  access: ShareAccess
  /** Normalized addresses, empty for a link share. */
  recipients: string[]
  created_at: string
  updated_at: string
}

/** One row of "Shared with you". Carries nothing identifying about the owner. */
export interface SharedWithMeItem {
  share_id: string
  title: string
  updated_at: string
}

export interface SharedMessage {
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

/** What a public visitor gets: the transcript and nothing identifying. */
export interface SharedChat {
  title: string
  created_at: string
  messages: SharedMessage[]
  message_count: number
  truncated: boolean
  /** Invite shares only: this reader may take the chat into a copy of their own. */
  can_continue: boolean
}

export interface ModelOption {
  id: string
  label: string
}

export interface ModelCatalog {
  default: string
  models: ModelOption[]
}

export interface CompactThreadResponse {
  changed: boolean
  /** Backlog was left behind: a failed fold or the iteration ceiling. */
  stalled: boolean
  summarized_through_position: number | null
  context_used_percent: number | null
}

export type MemoryScope = 'thread' | 'project' | 'user'

export type MemoryType = 'preference' | 'fact' | 'pin'

export type MemorySource = 'user_explicit' | 'user_confirmed' | 'llm_extracted'

/** The backend's closed key list. Anything else must be `other:<slug>`. */
export const MEMORY_KEYS = [
  'response_language',
  'response_length',
  'response_format',
  'tone',
  'nickname',
  'occupation',
  'organization',
  'focus_area',
  'expertise_level',
] as const

export interface Memory {
  memory_id: string
  owner_user_id: string
  scope: MemoryScope
  thread_id: string | null
  project_id: string | null
  type: MemoryType
  key: string
  value: string
  source: MemorySource
  superseded_by: string | null
  last_used_at: string | null
  deleted_at: string | null
  created_at: string
  updated_at: string
}

export interface UserSettings {
  owner_user_id: string
  memory_enabled: boolean
  display_name: string | null
  email: string | null
  created_at: string
  updated_at: string
}

/** A rendered message: a stored one plus the live hits that are not persisted. */
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  hits?: QueryHit[]
  citations?: string[]
  error?: boolean
  /** Absent while streaming and on locally-minted error bubbles. */
  position?: number
}

/** The closed list the report dropdown offers; mirrors the backend's. */
export type ReportReason = 'harmful' | 'false_information' | 'not_helpful' | 'other'
