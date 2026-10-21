from dataclasses import dataclass
from enum import StrEnum

from rag_ingestion.indexing.qdrant_index import Embedder, EmbeddingProfile
from rag_ingestion.retrieval.reranking import Reranker


class Provider(StrEnum):
    COHERE = "cohere"
    VOYAGE = "voyage"
    JINA = "jina"


class ProviderError(RuntimeError):
    pass


class ProviderUnavailableError(ProviderError):
    pass


class ProviderRequestError(ProviderError):
    pass


class ProviderResponseError(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    provider: Provider
    profile: EmbeddingProfile
    collection_name: str
    embedder: Embedder
    reranker: Reranker


class ProviderRegistry:
    def __init__(self, bundles: list[ProviderBundle] | None = None) -> None:
        self._bundles = {
            bundle.provider: bundle for bundle in bundles or []
        }

    def register(self, bundle: ProviderBundle) -> None:
        if bundle.profile.provider != bundle.provider.value:
            raise ValueError("Provider bundle profile does not match its provider")
        self._bundles[bundle.provider] = bundle

    @property
    def providers(self) -> list[Provider]:
        return sorted(self._bundles, key=lambda provider: provider.value)

    def get(self, provider: Provider | str) -> ProviderBundle:
        selected = Provider(provider)
        try:
            return self._bundles[selected]
        except KeyError as error:
            raise ProviderUnavailableError(
                f"Provider {selected.value!r} is not configured"
            ) from error
