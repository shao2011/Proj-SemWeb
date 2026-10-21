from __future__ import annotations

from dataclasses import dataclass, field

from rag_ingestion.config import RetrievalConfig
from rag_ingestion.retrieval.automerge import auto_merge, descendant_leaf_ids
from rag_ingestion.chunking import Chunk, ChunkLevel
from rag_ingestion.indexing.qdrant_index import (
    ROUTING_LEVELS,
    Embedder,
    QdrantHybridIndex,
    chunk_from_payload,
)
from rag_ingestion.logger import get_logger
from rag_ingestion.retrieval.reranking import Reranker

_log = get_logger(__name__)


@dataclass(slots=True)
class RetrievalOutcome:
    """Result of the query chain.

    ``context_chunks`` is what gets assembled for generation and may contain
    promoted parents. ``chunks`` is the citable leaf evidence those parents
    resolve back to, so a citation never points vaguely at a parent.
    """

    chunks: list[Chunk] = field(default_factory=list)
    context_chunks: list[Chunk] = field(default_factory=list)
    evidence_leaf_ids: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    routed_doc_ids: list[str] = field(default_factory=list)
    diagnostics: dict[str, int] = field(default_factory=dict)


def _fill_pool(sources: list[list[Chunk]], budget: int) -> list[Chunk]:
    """Fill the candidate pool with an even share per source.

    Merging every source into one dict and slicing it would cut whichever source
    was inserted last, which is the global fallback. With the scoped branch
    returning a full page that silently empties the fallback, and
    ``global_k >= 1`` stops meaning anything. No existing test noticed, because
    today's two sources happen to sum to exactly ``max_candidates``.

    Allocation is round-robin rather than a fixed slice each, so a source that
    runs short hands its unused slots to the others instead of wasting them.
    Duplicates across sources are kept once, at their first position.
    """
    if budget < 1:
        return []

    # Each source is reduced to what the earlier ones did not already return.
    # Overlap is not a contribution: counted as one, the scoped branch spends the
    # fallback's share on rows it had itself provided, and the fallback's real
    # contribution -- which sits at its tail, past the shared head -- is never
    # reached.
    seen: set[str] = set()
    distinct: list[list[Chunk]] = []
    for source in sources:
        fresh: list[Chunk] = []
        for chunk in source:
            if chunk.chunk_id not in seen:
                seen.add(chunk.chunk_id)
                fresh.append(chunk)
        distinct.append(fresh)

    pool: dict[str, Chunk] = {}
    cursors = [0] * len(distinct)
    while len(pool) < budget:
        placed = False
        for position, source in enumerate(distinct):
            if len(pool) >= budget:
                break
            if cursors[position] < len(source):
                chunk = source[cursors[position]]
                cursors[position] += 1
                pool[chunk.chunk_id] = chunk
                placed = True
        if not placed:
            break
    return list(pool.values())


def _points_to_chunks(points: list[object]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for point in points:
        payload = getattr(point, "payload", None) or {}
        if payload.get("chunk_id"):
            chunks.append(chunk_from_payload(payload))
    return chunks


def _selected_score(
    chunk: Chunk, scores: dict[str, float], chunks_by_id: dict[str, Chunk]
) -> float:
    """The rerank score a selected chunk stands on.

    Auto-Merge replaces ranked leaves with an ancestor, and an ancestor was never
    in the pool the reranker scored, so it has no score of its own. The strongest
    leaf underneath it is the relevance it is standing on.
    """
    own = scores.get(chunk.chunk_id)
    if own is not None:
        return own
    return max(
        (
            scores.get(leaf_id, 0.0)
            for leaf_id in descendant_leaf_ids(chunk, chunks_by_id)
        ),
        default=0.0,
    )


def _ancestors(
    index: QdrantHybridIndex,
    chunks: list[Chunk],
    *,
    owner_user_id: str,
    project_id: str,
) -> dict[str, Chunk]:
    """Walk parent links upward so Auto-Merge can promote more than one level."""
    known = {chunk.chunk_id: chunk for chunk in chunks}
    frontier = {
        chunk.parent_id for chunk in chunks if chunk.parent_id and chunk.parent_id not in known
    }
    while frontier:
        loaded = index.fetch_chunks(
            list(frontier), owner_user_id=owner_user_id, project_id=project_id
        )
        if not loaded:
            break
        known.update(loaded)
        frontier = {
            parent.parent_id
            for parent in loaded.values()
            if parent.parent_id and parent.parent_id not in known
        }
    return known


def run_retrieval(
    index: QdrantHybridIndex,
    embedder: Embedder,
    reranker: Reranker,
    query: str,
    *,
    owner_user_id: str,
    project_id: str,
    language: str | None = None,
    limit: int = 10,
    config: RetrievalConfig | None = None,
    rewritten_query: str | None = None,
    exclude_doc_ids: list[str] | None = None,
    project_doc_ids: set[str] | None = None,
) -> RetrievalOutcome:
    active = config or RetrievalConfig()
    diagnostics: dict[str, int] = {}

    # Embed once here, not once per search: Q0, the scoped pass, and the
    # global fallback all run on the same query text.
    query_vector = embedder.embed_query(query, index.profile, intent="query")
    has_rewrite = bool(rewritten_query and rewritten_query != query)
    rewrite_vector = (
        embedder.embed_query(rewritten_query, index.profile, intent="query")
        if has_rewrite
        else None
    )

    # Q0: soft route over section and document vectors.
    routed_points = index.hybrid_search(
        query,
        embedder,
        owner_user_id=owner_user_id,
        project_id=project_id,
        language=language,
        limit=active.route_limit,
        levels=ROUTING_LEVELS,
        citable=False,
        exclude_doc_ids=exclude_doc_ids,
        dense_vector=query_vector,
    )
    routed = _points_to_chunks(routed_points)
    routed_doc_ids = list(dict.fromkeys(chunk.doc_id for chunk in routed))
    diagnostics["routed"] = len(routed)

    # When routing reaches every document the project has, the doc-scoped
    # pass on the original query is the global search with a filter that
    # excludes nothing -- identical results, one wasted round-trip. The
    # rewrite pass still runs: it differs by text, not by scope.
    route_covers_project = bool(
        routed_doc_ids
        and project_doc_ids is not None
        and set(routed_doc_ids) >= project_doc_ids
    )
    diagnostics["route_covers_project"] = int(route_covers_project)

    # Q1: scoped candidates plus a global fallback that always runs.
    sources: list[list[Chunk]] = []
    if routed_doc_ids:
        if not route_covers_project:
            sources.append(
                _points_to_chunks(
                    index.hybrid_search(
                        query,
                        embedder,
                        owner_user_id=owner_user_id,
                        project_id=project_id,
                        language=language,
                        limit=active.route_k,
                        levels=(ChunkLevel.LEAF,),
                        doc_ids=routed_doc_ids,
                        exclude_doc_ids=exclude_doc_ids,
                        dense_vector=query_vector,
                    )
                )
            )
        # A rewrite only helps the scoped search: Q0's routing already ran on
        # the original query, and the global branch below is the fallback of
        # last resort, which stays on the original on purpose (§3.9's safety
        # net: a bad rewrite must not be able to take the fallback down too).
        if has_rewrite:
            sources.append(
                _points_to_chunks(
                    index.hybrid_search(
                        rewritten_query,
                        embedder,
                        owner_user_id=owner_user_id,
                        project_id=project_id,
                        language=language,
                        limit=active.route_k,
                        levels=(ChunkLevel.LEAF,),
                        doc_ids=routed_doc_ids,
                        exclude_doc_ids=exclude_doc_ids,
                        dense_vector=rewrite_vector,
                    )
                )
            )
    diagnostics["scoped"] = sum(len(source) for source in sources)
    diagnostics["rewrite_used"] = int(has_rewrite)

    sources.append(
        _points_to_chunks(
            index.hybrid_search(
                query,
                embedder,
                owner_user_id=owner_user_id,
                project_id=project_id,
                language=language,
                limit=active.global_k,
                levels=(ChunkLevel.LEAF,),
                exclude_doc_ids=exclude_doc_ids,
                dense_vector=query_vector,
            )
        )
    )

    pool = _fill_pool(sources, active.max_candidates)
    diagnostics["candidates"] = len(pool)
    _log.debug(
        "Q1 pool=%d from %d source(s) sizes=%s routed_docs=%d",
        len(pool),
        len(sources),
        [len(source) for source in sources],
        len(routed_doc_ids),
    )
    if not pool:
        _log.debug("No candidates for %r; returning an empty outcome", query)
        return RetrievalOutcome(routed_doc_ids=routed_doc_ids, diagnostics=diagnostics)

    # Q3: rerank the fused pool, on the rewrite when there is one.
    ranked_results = reranker.rerank(
        rewritten_query or query,
        [chunk.source_text for chunk in pool],
        top_n=min(limit, len(pool)),
    )
    ranked: list[Chunk] = []
    scores: dict[str, float] = {}
    seen: set[int] = set()
    for result in ranked_results:
        # Both bounds, not just the upper one: `pool[-1]` is legal Python, so a
        # negative index used to pass this check and silently answer from the last
        # candidate in the pool instead of failing.
        if not 0 <= result.index < len(pool) or result.index in seen:
            raise ValueError(
                f"Reranker returned an invalid candidate index: {result.index!r} "
                f"for a pool of {len(pool)}"
            )
        seen.add(result.index)
        chunk = pool[result.index]
        ranked.append(chunk)
        scores[chunk.chunk_id] = result.relevance_score
    diagnostics["reranked"] = len(ranked)

    # Cut the tail at a fraction of the best hit rather than at a fixed score.
    # A reranker's relevance score is query-dependent, so an absolute floor
    # tuned on one query does not transfer to the next, and the three provider
    # families this backend supports do not share a distribution either. A
    # ratio has no units and so needs neither calibration.
    #
    # The argmax always survives -- `keep_ratio <= 1.0` makes that arithmetic,
    # not a special case -- so this can thin the evidence but never empty it.
    # Read off `max` instead of `ranked[0]` because nothing here enforces that
    # a reranker returns its results in descending order.
    #
    # `ranked` stays every scored leaf and `evidence` is what survives, because
    # the reference weighting below has to keep seeing a cut leaf's real score.
    # Take a cut leaf out of that weighting and its references come back anyway
    # through the ancestor fallback, at the ancestor's strength -- which is the
    # strongest leaf under it, so cutting a leaf would *promote* its references
    # past those of a leaf that survived.
    evidence = ranked
    if ranked:
        top = max(scores[chunk.chunk_id] for chunk in ranked)
        # A negative top score turns the ratio upside down -- the floor lands
        # *above* the best hit and cuts everything, argmax included. Cohere,
        # Voyage and Jina all document a normalized [0, 1] score, but nothing in
        # `RankedResult` enforces it, and a raw cross-encoder logit is signed. A
        # ratio means nothing on that scale, so leave the set alone instead.
        floor = top * active.keep_ratio if top > 0 else float("-inf")
        cut = [chunk for chunk in ranked if scores[chunk.chunk_id] < floor]
        if cut:
            _log.debug(
                "Q3 cut %d of %d below %.4f: %s",
                len(cut),
                len(ranked),
                floor,
                [(chunk.chunk_id, round(scores[chunk.chunk_id], 4)) for chunk in cut],
            )
            evidence = [chunk for chunk in ranked if scores[chunk.chunk_id] >= floor]
        diagnostics["below_keep_ratio"] = len(cut)

    # Q4: Auto-Merge, then one-hop reference expansion.
    chunks_by_id = _ancestors(
        index, evidence, owner_user_id=owner_user_id, project_id=project_id
    )
    merged = auto_merge(evidence, chunks_by_id, token_budget=active.token_budget)
    diagnostics["auto_merged"] = merged.merge_count

    selected: list[Chunk] = list(merged.chunks)
    selected_ids = {chunk.chunk_id for chunk in selected}

    # How strongly each referenced block was pointed at. An expanded chunk has no
    # score of its own, it was never in the pool the reranker saw, so scoring it
    # by `scores.get(chunk.chunk_id)` returned 0.0 for every candidate: the sort
    # below was a no-op and `reference_cap` cut the list in whatever order Qdrant
    # happened to scroll it back, which is by point id.
    #
    # Weighed from the ranked leaves first, because they are the only chunks
    # holding a real score. Auto-Merge may have replaced several of them with one
    # ancestor, and an ancestor carries the union of its children's refs, so
    # reading the weights off `selected` alone would give every ref under it the
    # same number and put the ordering straight back to a tie.
    reference_weight: dict[str, float] = {}
    for chunk in ranked:
        weight = scores.get(chunk.chunk_id, 0.0)
        for ref in chunk.refs:
            reference_weight[ref] = max(reference_weight.get(ref, 0.0), weight)
    for chunk in selected:
        # Refs an ancestor carries from a child the reranker never ranked. Filled
        # rather than maxed, so a per-leaf weight already recorded above wins.
        fallback = _selected_score(chunk, scores, chunks_by_id)
        for ref in chunk.refs:
            reference_weight.setdefault(ref, fallback)
    # Truncated here, not just sorted: `fetch_by_block_ids` is one MatchAny
    # scroll, and Qdrant neither honours the order of a match list nor chooses
    # which survive its limit -- that cut falls by point id. Sending fewer ids
    # than the limit can hold is what moves the choice onto the weights. Two
    # per cap slot is enough: a block belongs to exactly one leaf, so K ids
    # return at most K chunks, under the fetch limit of cap * 4 below.
    referenced_block_ids = sorted(
        reference_weight, key=lambda block_id: reference_weight[block_id], reverse=True
    )[: active.reference_cap * 2]
    expansion = index.fetch_by_block_ids(
        referenced_block_ids,
        owner_user_id=owner_user_id,
        project_id=project_id,
        limit=active.reference_cap * 4,
        exclude_doc_ids=exclude_doc_ids,
    )
    expanded = [chunk for chunk in expansion if chunk.chunk_id not in selected_ids]
    expanded.sort(
        key=lambda chunk: max(
            (reference_weight.get(block_id, 0.0) for block_id in chunk.source_block_ids),
            default=0.0,
        ),
        reverse=True,
    )
    for chunk in expanded[: active.reference_cap]:
        selected.append(chunk)
        selected_ids.add(chunk.chunk_id)
    diagnostics["reference_expanded"] = min(len(expanded), active.reference_cap)

    # Context order: a merged parent stands at its best recalled leaf's rank
    # (max-signal, not mean -- one bullseye leaf must not be diluted by dull
    # siblings), leaves keep their own rank, expansion chunks follow behind.
    rank_position = {chunk.chunk_id: position for position, chunk in enumerate(ranked)}

    def _best_rank(chunk: Chunk) -> int:
        own = rank_position.get(chunk.chunk_id)
        if own is not None:
            return own
        return min(
            (
                rank_position[leaf_id]
                for leaf_id in descendant_leaf_ids(chunk, chunks_by_id)
                if leaf_id in rank_position
            ),
            default=len(rank_position),
        )

    selected.sort(key=_best_rank)

    # Q6: resolve everything back to citable leaves.
    evidence_ids: list[str] = []
    for chunk in selected:
        evidence_ids.extend(descendant_leaf_ids(chunk, chunks_by_id))
    evidence_ids = list(dict.fromkeys(evidence_ids))

    known_leaves: dict[str, Chunk] = {
        chunk.chunk_id: chunk
        for chunk in [*chunks_by_id.values(), *selected]
        if chunk.level is ChunkLevel.LEAF
    }
    missing = [chunk_id for chunk_id in evidence_ids if chunk_id not in known_leaves]
    if missing:
        known_leaves.update(
            index.fetch_chunks(
                missing, owner_user_id=owner_user_id, project_id=project_id
            )
        )

    evidence_chunks = [
        known_leaves[chunk_id]
        for chunk_id in evidence_ids
        if chunk_id in known_leaves and known_leaves[chunk_id].citable
    ]
    # Preserve the reranker's ordering; leaves pulled in by promotion or
    # reference expansion keep their traversal order behind it.
    evidence_chunks.sort(
        key=lambda chunk: rank_position.get(chunk.chunk_id, len(rank_position))
    )
    diagnostics["evidence_leaves"] = len(evidence_chunks)
    # One line carrying every stage count. This is the line to read first when an
    # answer cites the wrong thing, or nothing.
    if diagnostics["rewrite_used"]:
        _log.info(
            "Retrieval for %r (rewritten to %r): %s", query, rewritten_query, diagnostics
        )
    else:
        _log.info("Retrieval for %r: %s", query, diagnostics)

    return RetrievalOutcome(
        chunks=evidence_chunks,
        context_chunks=selected,
        evidence_leaf_ids=[chunk.chunk_id for chunk in evidence_chunks],
        scores=scores,
        routed_doc_ids=routed_doc_ids,
        diagnostics=diagnostics,
    )
