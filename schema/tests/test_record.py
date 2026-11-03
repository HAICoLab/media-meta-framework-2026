from mmf_schema import MetadataRecord, Provenance, SourceType


def test_make_id_is_deterministic():
    a = MetadataRecord.make_id(SourceType.WEB, "https://example.com")
    b = MetadataRecord.make_id(SourceType.WEB, "https://example.com")
    assert a == b
    assert a.startswith("web:")


def test_record_roundtrip_json():
    rec = MetadataRecord(
        id="web:abc",
        source_type=SourceType.WEB,
        title="제목",
        provenance=Provenance(
            source_uri="https://example.com", extractor="t", extractor_version="0"
        ),
    )
    again = MetadataRecord.model_validate_json(rec.model_dump_json())
    assert again == rec
