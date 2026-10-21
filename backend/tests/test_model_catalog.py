import pytest
from pydantic import ValidationError

from rag_ingestion.model_catalog import (
    SAFETY_FLOOR_TOKENS,
    context_percent,
    load_catalog,
    usable_tokens,
)
from rag_ingestion.schemas.model_catalog import ModelCatalog, ModelSpec


class TestBundledCatalog:
    def test_the_bundled_file_loads_and_has_a_default(self) -> None:
        catalog = load_catalog()

        assert catalog.models
        assert catalog.resolve(None).id == catalog.default

    def test_every_bundled_model_reserves_room_for_output(self) -> None:
        """`usable_tokens` is the gauge's denominator; a spec whose output
        cap swallows its context would make it zero or negative."""
        for spec in load_catalog().models:
            assert usable_tokens(spec) > 0, spec.id

    def test_a_known_id_resolves_to_its_own_spec(self) -> None:
        catalog = load_catalog()
        spec = catalog.models[-1]

        assert catalog.resolve(spec.id) is spec

    def test_an_unknown_id_falls_back_to_the_default(self) -> None:
        """A thread that recorded a model later removed from the catalog must
        keep working on the default, never crash the turn."""
        catalog = load_catalog()

        assert catalog.resolve("vendor/model-that-was-removed").id == catalog.default

    def test_the_bundled_default_is_offered_by_the_picker(self) -> None:
        """Disabling the default would leave the picker with no way back to
        the model every unknown id resolves to."""
        catalog = load_catalog()

        assert catalog.resolve(catalog.default).enabled

    def test_a_disabled_model_still_resolves_to_its_own_spec(self) -> None:
        """`enabled` hides a model from the picker, it does not remove it: a
        thread that already recorded the id keeps that model's budget."""
        catalog = load_catalog()
        disabled = [spec for spec in catalog.models if not spec.enabled]

        for spec in disabled:
            assert catalog.resolve(spec.id) is spec


class TestCatalogValidation:
    def _spec(self, id: str = "test/model") -> dict:
        return {
            "id": id,
            "label": "Test",
            "context_tokens": 100_000,
            "max_output_tokens": 8_000,
            "keep_recent_max_messages": 20,
            "effort": None,
        }

    def test_duplicate_ids_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelCatalog(
                version=1,
                default="test/model",
                models=[self._spec(), self._spec()],
            )

    def test_a_default_not_in_the_list_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelCatalog(version=1, default="test/other", models=[self._spec()])

    def test_output_cap_must_leave_input_room(self) -> None:
        bad = self._spec()
        bad["max_output_tokens"] = bad["context_tokens"]
        with pytest.raises(ValidationError):
            ModelSpec(**bad)

    def test_a_spec_without_enabled_is_offered(self) -> None:
        """Existing entries carry no `enabled` key, so the default has to be
        the visible one or adding the field would empty the picker."""
        assert ModelSpec(**self._spec()).enabled is True

    def test_a_spec_without_router_lives_on_openrouter(self) -> None:
        """Entries that predate multi-router carry no `router` key and have
        always been served by OpenRouter; the default keeps that true."""
        assert ModelSpec(**self._spec()).router == "openrouter"


class TestGaugeMath:
    SPEC = ModelSpec(
        id="test/model",
        label="Test",
        context_tokens=100_000,
        max_output_tokens=8_000,
        keep_recent_max_messages=20,
        effort=None,
    )

    def test_usable_subtracts_output_and_safety_floor(self) -> None:
        assert usable_tokens(self.SPEC) == 100_000 - 8_000 - SAFETY_FLOOR_TOKENS

    def test_percent_is_prompt_over_usable(self) -> None:
        usable = usable_tokens(self.SPEC)

        assert context_percent(usable // 2, self.SPEC) == 50
        assert context_percent(0, self.SPEC) == 0

    def test_percent_clamps_at_100(self) -> None:
        """After a switch from a long-context model, the recorded prompt can
        exceed the new model's whole budget; the gauge caps, never overflows."""
        assert context_percent(10_000_000, self.SPEC) == 100

    def test_no_measurement_yet_means_no_percent(self) -> None:
        assert context_percent(None, self.SPEC) is None
