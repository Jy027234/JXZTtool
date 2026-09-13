from __future__ import annotations

from parsecore.catalog_extractors import (
    CatalogPage,
    CatalogExtractorRegistry,
    CaacApprovedCatalogV1Extractor,
    CaacApprovedCatalogV2Extractor,
    anchor_row_numbers,
    build_candidate_record,
    default_catalog_extractor_registry,
    detect_catalog_sections,
    extract_candidate_rows,
    map_words_to_columns,
)


def _word(text: str, x0: float, x1: float, top: float, bottom: float) -> dict[str, object]:
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom}


def test_default_registry_is_explicit_and_versioned() -> None:
    registry = default_catalog_extractor_registry()

    extractor = registry.get("caac-approved-catalog.v1")
    assert isinstance(extractor, CaacApprovedCatalogV1Extractor)
    assert isinstance(
        registry.get("caac-approved-catalog.v2"),
        CaacApprovedCatalogV2Extractor,
    )
    assert registry.descriptors() == (
        {
            "schemaVersion": "caac-approved-catalog.v1",
            "extractor": "caac-approved-catalog",
            "extractorVersion": "1.0.0",
        },
        {
            "schemaVersion": "caac-approved-catalog.v2",
            "extractor": "caac-approved-catalog",
            "extractorVersion": "2.0.0",
        },
    )
    assert registry.maybe_get(None) is None


def test_registry_rejects_duplicate_schema_versions() -> None:
    registry = CatalogExtractorRegistry((CaacApprovedCatalogV1Extractor(),))

    try:
        registry.register(CaacApprovedCatalogV1Extractor())
    except ValueError as error:
        assert "already registered" in str(error)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("duplicate extractor registration unexpectedly succeeded")


def test_section_detector_keeps_pma_and_ctsoa_physical_pages() -> None:
    pages = (
        CatalogPage(162, "七、已获批准的PMA产品目录清单"),
        CatalogPage(163, "七、已获批准的PMA产品目录清单\n序号 PMA编号 持证人 产品"),
        CatalogPage(8, "二、获得国外认可的民用航空产品\n序号 持有人 名称"),
        CatalogPage(16786, "CTSOA 持证人及产品"),
    )

    sections = detect_catalog_sections(pages)

    assert [(item.section_key, item.pdf_page_start) for item in sections] == [
        ("pma_item", 162),
        ("pma_item", 163),
        ("foreign_product_recognition", 8),
        ("ctsoa", 16786),
    ]


def test_row_anchor_uses_first_column_coordinates_and_reports_ambiguous_rows() -> None:
    page = CatalogPage(
        164,
        words=(
            _word("35", 40, 50, 100, 110),
            _word("PMA0005-ZN-001", 70, 150, 100, 110),
            _word("过滤器", 170, 210, 100, 110),
            _word("36", 40, 50, 120, 130),
            _word("37", 55, 65, 120, 130),
        ),
    )

    anchors, signals = anchor_row_numbers(page, first_column_right=72)

    assert [anchor.row_number for anchor in anchors] == [35]
    assert any(signal["code"] == "row_anchor_out_of_column" for signal in signals)


def test_column_mapper_uses_header_centres_instead_of_text_regex() -> None:
    headers = (
        _word("approval", 60, 100, 80, 90),
        _word("part", 220, 250, 80, 90),
    )
    words = (
        _word("PMA0005", 65, 115, 100, 110),
        _word("412A1600-41PY", 215, 290, 100, 110),
    )

    mapped, signals = map_words_to_columns(words, headers)

    assert signals == ()
    assert [word["text"] for word in mapped["approval"]] == ["PMA0005"]
    assert [word["text"] for word in mapped["part"]] == ["412A1600-41PY"]


def test_candidate_row_projection_keeps_cells_and_field_evidence_separate() -> None:
    words = (
        _word("序号", 40, 55, 80, 90),
        _word("证件编号", 80, 130, 80, 90),
        _word("持证人", 180, 220, 80, 90),
        _word("型别", 300, 330, 80, 90),
        _word("最新批准日期", 450, 520, 80, 90),
        _word("1", 40, 45, 100, 110),
        _word("TC001A", 80, 120, 100, 110),
        _word("示例持证人", 180, 230, 100, 110),
        _word("Y11B", 300, 330, 100, 110),
        _word("2024-12-31", 450, 510, 100, 110),
    )

    rows, signals = extract_candidate_rows(CatalogPage(3, words=words), "tc")

    assert len(rows) == 1
    assert signals == ()
    assert rows[0]["rowNumber"] == 1
    assert rows[0]["cells"]["approval.numberOriginal"] == "TC001A"
    assert rows[0]["cells"]["holder.nameOriginal"] == "示例持证人"
    assert rows[0]["fieldEvidence"][0]["pdfPage"] == 3


def test_candidate_row_projection_partitions_centered_multiline_model_cells() -> None:
    # Mirrors the TC page layout where a model cell can span several visual
    # lines above and below the serial-number baseline.  Midpoint row bands
    # would leak Y8F into row 2 and Y8F-400 into row 4.
    words = (
        _word("序号", 40, 55, 80, 90),
        _word("证件编号", 80, 130, 80, 90),
        _word("持证人", 180, 220, 80, 90),
        _word("型别", 300, 330, 80, 90),
        _word("最新批准日期", 450, 520, 80, 90),
        _word("1", 40, 45, 96, 106),
        _word("TC001A", 80, 120, 96, 106),
        _word("持证人一", 180, 230, 96, 106),
        _word("Y11B", 300, 330, 96, 106),
        _word("1992-12-28", 450, 510, 96, 106),
        _word("2", 40, 45, 110, 120),
        _word("TC002P", 80, 120, 110, 120),
        _word("持证人二", 180, 230, 110, 120),
        _word("J17-G13", 300, 350, 110, 120),
        _word("1993-07-06", 450, 510, 110, 120),
        _word("Y8F", 300, 330, 124, 134),
        _word("Y8F-100", 300, 350, 137, 147),
        _word("Y8F-200", 300, 350, 150, 160),
        _word("3", 40, 45, 143, 153),
        _word("TC003A", 80, 120, 143, 153),
        _word("持证人三", 180, 230, 143, 153),
        _word("2002-08-26", 450, 510, 143, 153),
        _word("Y8F-400", 300, 350, 163, 173),
        _word("4", 40, 45, 176, 186),
        _word("TDA-LSA-0003A", 80, 160, 176, 186),
        _word("持证人四", 180, 230, 176, 186),
        _word("Ikarus C42E", 300, 380, 176, 186),
        _word("2012-04-28", 450, 510, 176, 186),
        _word("TC-1", 300, 330, 560, 570),
    )

    rows, signals = extract_candidate_rows(
        CatalogPage(3, words=words, metadata={"pageHeight": 595.0}),
        "tc",
    )

    assert signals == ()
    assert [row["cells"]["item.modelOriginal"] for row in rows] == [
        "Y11B",
        "J17-G13",
        "Y8F Y8F-100 Y8F-200 Y8F-400",
        "Ikarus C42E",
    ]


def test_stc_multiline_original_model_keeps_numeric_aircraft_model() -> None:
    # STC page 54 has a long original-model cell whose first/last visual
    # lines sit well above/below the serial baseline.  A model such as
    # ``737-800`` is source data, never generic numeric page furniture.
    words = (
        _word("序号", 40, 55, 80, 90),
        _word("证件编号", 80, 130, 80, 90),
        _word("持证人", 150, 200, 80, 90),
        _word("原产品型号", 280, 340, 80, 90),
        _word("合格证编号", 459, 520, 80, 90),
        _word("现产品型号", 530, 590, 80, 90),
        _word("最新批准日期", 680, 750, 80, 90),
        _word("A320-231", 280, 330, 108, 118),
        _word("A320-232", 280, 330, 121, 131),
        _word("1", 40, 45, 136, 146),
        _word("STC0411-ZN", 80, 130, 136, 146),
        _word("持证人一", 150, 210, 136, 146),
        _word("A320-233", 280, 330, 134, 144),
        _word("A320-C1", 425, 455, 134, 144),
        _word("VTC0099A", 459, 510, 136, 146),
        _word("改装一", 530, 580, 136, 146),
        _word("2024-12-26", 680, 740, 136, 146),
        _word("A320-234", 280, 330, 147, 157),
        _word("A320-235", 280, 330, 160, 170),
        _word("A320-236", 280, 330, 175, 185),
        _word("2", 40, 45, 220, 230),
        _word("STC0412-HB", 80, 130, 220, 230),
        _word("持证人二", 150, 210, 220, 230),
        _word("737-800", 280, 330, 220, 230),
        _word("VTC0167A", 459, 510, 220, 230),
        _word("不适用", 530, 580, 220, 230),
        _word("2024-12-27", 680, 740, 220, 230),
        _word("STC-1", 280, 320, 280, 290),
    )

    rows, _ = extract_candidate_rows(
        CatalogPage(54, words=words, metadata={"pageHeight": 300.0}),
        "stc",
    )

    assert [row["cells"]["attributes.originalModel"] for row in rows] == [
        "A320-231 A320-232 A320-233 A320-C1 A320-234 A320-235 A320-236",
        "737-800",
    ]
    record, signals = build_candidate_record(
        row=rows[1],
        section_key="stc",
        source_snapshot_sha256="a" * 64,
        parser_version="0.1.0",
    )
    assert signals == ()
    assert record is not None
    assert record["attributes"] == {
        "originalApprovalNumber": "VTC0167A",
        "originalModel": "737-800",
    }


def test_mda_reassigns_product_type_and_description_overflow() -> None:
    # On the MDA table, 737 is visually left of the description header but
    # beyond the midpoint between header centres; description text can likewise
    # cross into the date interval. Preserve the physical columns.
    words = (
        _word("序号", 32, 54, 80, 90),
        _word("证件编号", 58, 103, 80, 90),
        _word("持证人", 126, 160, 80, 90),
        _word("型号证书编号", 221, 288, 80, 90),
        _word("产品型别", 293, 338, 80, 90),
        _word("设计更改/修理设计描述", 448, 563, 80, 90),
        _word("最新批准日期", 741, 809, 80, 90),
        _word("1", 40, 45, 110, 120),
        _word("MDA317-XN", 58, 114, 110, 120),
        _word("示例持证人", 126, 216, 110, 120),
        _word("VTC0167A", 221, 268, 110, 120),
        _word("737", 430, 445, 110, 120),
        _word("说明尾段", 650, 700, 110, 120),
        _word("2024-12-31", 753, 800, 110, 120),
        _word("MDA-14", 430, 470, 260, 270),
    )

    rows, signals = extract_candidate_rows(
        CatalogPage(77, words=words, metadata={"pageHeight": 300.0}),
        "mda",
    )

    assert signals == ()
    assert len(rows) == 1
    assert rows[0]["cells"]["item.productTypeOriginal"] == "737"
    assert rows[0]["cells"]["item.descriptionOriginal"] == "说明尾段"
    assert rows[0]["cells"]["approval.latestApprovedOn"] == "2024-12-31"
    record, record_signals = build_candidate_record(
        row=rows[0],
        section_key="mda",
        source_snapshot_sha256="a" * 64,
        parser_version="0.1.0",
    )
    assert record_signals == ()
    assert record is not None
    assert record["attributes"] == {"modelCertificateNumber": "VTC0167A"}
    assert record["item"]["productTypeOriginal"] == "737"
    assert record["item"]["descriptionOriginal"] == "说明尾段"


def test_mda_partitions_wrapped_product_type_blocks() -> None:
    # PDF page 154 gives the last MDA rows a vertically centred serial anchor
    # while their product-type cells wrap over many short lines. Partition by
    # visual cell block rather than the midpoint between serial anchors.
    words = (
        _word("序号", 32, 54, 80, 90),
        _word("证件编号", 58, 103, 80, 90),
        _word("持证人", 126, 160, 80, 90),
        _word("型号证书编号", 221, 288, 80, 90),
        _word("产品型别", 293, 338, 80, 90),
        _word("设计更改/修理设计描述", 448, 563, 80, 90),
        _word("最新批准日期", 741, 809, 80, 90),
        _word("A320-214", 293, 340, 110, 120),
        _word("A321-271NX", 293, 350, 123, 133),
        _word("1", 40, 45, 117, 127),
        _word("MDA0693-XN", 58, 114, 117, 127),
        _word("持证人一", 126, 216, 117, 127),
        _word("VTC0099A", 221, 268, 117, 127),
        _word("设计描述一", 448, 520, 117, 127),
        _word("2024-12-30", 753, 800, 117, 127),
        _word("A320-231", 293, 340, 160, 170),
        _word("A321-111", 293, 340, 173, 183),
        _word("A319-112", 293, 340, 186, 196),
        _word("A320-216", 293, 340, 199, 209),
        _word("A321-212", 293, 340, 212, 222),
        _word("A321-232", 293, 340, 225, 235),
        _word("A319-151N", 293, 345, 238, 248),
        _word("A320-271N", 293, 345, 251, 261),
        _word("A321-272N", 293, 345, 264, 274),
        _word("A321-253N", 293, 345, 277, 287),
        _word("A321-251NX", 293, 350, 290, 300),
        _word("A321-271NX", 293, 350, 303, 313),
        _word("A319-171N", 293, 345, 316, 326),
        _word("2", 40, 45, 242, 252),
        _word("MDA0694-HB", 58, 114, 242, 252),
        _word("持证人二", 126, 216, 242, 252),
        _word("VTC0099A", 221, 268, 242, 252),
        _word("设计描述二", 448, 520, 242, 252),
        _word("2024-12-31", 753, 800, 242, 252),
    )

    rows, signals = extract_candidate_rows(
        CatalogPage(154, words=words, metadata={"pageHeight": 400.0}),
        "mda",
    )

    assert [signal["code"] for signal in signals] == ["continuation_pending"]
    assert [row["cells"]["item.productTypeOriginal"] for row in rows] == [
        "A320-214 A321-271NX",
        (
            "A320-231 A321-111 A319-112 A320-216 A321-212 A321-232 "
            "A319-151N A320-271N A321-272N A321-253N A321-251NX "
            "A321-271NX A319-171N"
        ),
    ]


def test_candidate_row_projection_flags_prefix_continuation_before_first_anchor() -> None:
    words = (
        _word("序号", 40, 55, 80, 90),
        _word("证件编号", 80, 130, 80, 90),
        _word("持证人", 180, 220, 80, 90),
        _word("型别", 300, 330, 80, 90),
        _word("最新批准日期", 450, 520, 80, 90),
        _word("续行型号", 300, 340, 98, 108),
        _word("1", 40, 45, 115, 125),
        _word("TC001A", 80, 120, 115, 125),
        _word("示例持证人", 180, 230, 115, 125),
        _word("Y11B", 300, 330, 115, 125),
        _word("2024-12-31", 450, 510, 115, 125),
    )

    rows, signals = extract_candidate_rows(CatalogPage(4, words=words), "tc")

    assert len(rows) == 1
    assert any(signal["code"] == "continuation_pending" for signal in signals)


def test_candidate_row_projection_does_not_append_wrapped_text_to_approval_id() -> None:
    words = (
        _word("序号", 40, 55, 80, 90),
        _word("证件编号", 80, 130, 80, 90),
        _word("持证人", 180, 220, 80, 90),
        _word("型号", 300, 330, 80, 90),
        _word("最新批准日期", 450, 520, 80, 90),
        _word("1", 40, 45, 100, 110),
        _word("VDA0001", 80, 125, 100, 110),
        _word("DIVIS", 80, 110, 112, 122),
        _word("示例持证人", 180, 230, 100, 110),
        _word("Y11B", 300, 330, 100, 110),
        _word("2024-12-31", 450, 510, 100, 110),
    )

    rows, _ = extract_candidate_rows(CatalogPage(17051, words=words), "tc")

    assert rows[0]["cells"]["approval.numberOriginal"] == "VDA0001"
    assert rows[0]["cells"]["holder.nameOriginal"] == "示例持证人 DIVIS"


def test_candidate_row_projection_removes_repeated_pma_footer_from_item_name() -> None:
    words = (
        _word("序号", 40, 55, 80, 90),
        _word("项目单编号", 80, 130, 80, 90),
        _word("持证人", 180, 220, 80, 90),
        _word("项目名称", 300, 350, 80, 90),
        _word("件号", 400, 430, 80, 90),
        _word("有效期至", 450, 520, 80, 90),
        _word("1", 40, 45, 100, 110),
        _word("PMA0001", 80, 125, 100, 110),
        _word("示例持证人", 180, 230, 100, 110),
        _word("过滤器", 300, 330, 100, 110),
        _word("活门零件项目清单", 300, 390, 112, 122),
        _word("P-001", 400, 430, 100, 110),
        _word("2026-07-01", 450, 510, 100, 110),
    )

    rows, _ = extract_candidate_rows(CatalogPage(162, words=words), "pma_item")

    assert rows[0]["cells"]["item.nameOriginal"] == "过滤器"


def test_candidate_row_projection_keeps_vda_name_separate_from_model_part_lines() -> None:
    words = (
        _word("序号", 40, 55, 80, 90),
        _word("证件编号", 80, 130, 80, 90),
        _word("持证人", 180, 220, 80, 90),
        _word("产品类别", 260, 300, 80, 90),
        _word("产品名称", 310, 350, 80, 90),
        _word("型（件）号", 400, 450, 80, 90),
        _word("CTSO", 500, 530, 80, 90),
        _word("所属当局", 550, 590, 80, 90),
        _word("最新批准日期", 650, 720, 80, 90),
        _word("1", 40, 45, 100, 110),
        _word("VDA0001", 80, 125, 100, 110),
        _word("示例持证人", 180, 230, 100, 110),
        _word("航空器", 260, 300, 100, 110),
        _word("6项防撞设备", 310, 350, 100, 110),
        _word("P/N", 360, 390, 100, 110),
        _word("066-50002-8102", 400, 470, 100, 110),
        _word("TSO-C112", 500, 550, 100, 110),
        _word("FAA", 550, 580, 100, 110),
        _word("1996-11-11", 650, 710, 100, 110),
    )

    rows, _ = extract_candidate_rows(CatalogPage(17051, words=words), "vda")

    assert rows[0]["cells"]["item.nameOriginal"] == "6项防撞设备"


def test_candidate_row_projection_restores_vda_physical_column_starts() -> None:
    """VDA uses data-cell starts that do not align with centred headers."""

    words = (
        _word("序号", 32.64, 54.83, 80, 90),
        _word("证件编号", 60.46, 104.95, 80, 90),
        _word("持证人", 152.20, 185.54, 80, 90),
        _word("产品类别", 230.52, 275.01, 80, 90),
        _word("产品名称", 294.72, 339.21, 80, 90),
        _word("型（件）号", 403.24, 458.88, 80, 90),
        _word("CTSO", 516.36, 546.27, 80, 90),
        _word("所属当局", 560.40, 604.89, 80, 90),
        _word("国外审定证件号", 657.55, 735.38, 80, 90),
        _word("最新批准日期", 740.12, 806.80, 80, 90),
        _word("8", 41.28, 46.26, 100, 110),
        _word("VDA0022", 62.00, 103.65, 100, 110),
        _word("GARMIN", 109.23, 149.50, 100, 110),
        _word("International", 152.09, 202.94, 100, 110),
        _word("Inc.", 205.44, 220.67, 100, 110),
        _word("航空器", 231.36, 261.24, 100, 110),
        _word("GPS", 276.96, 296.90, 100, 110),
        _word("010-00182-()", 359.76, 443.10, 100, 110),
        _word("TSO-", 471.75, 493.97, 100, 110),
        _word("TSO-C112", 505.09, 548.40, 100, 110),
        _word("FAA", 560.28, 580.00, 100, 110),
        _word("1999-08-13", 752.16, 798.90, 100, 110),
    )

    rows, _ = extract_candidate_rows(CatalogPage(17052, words=words), "vda")

    assert rows[0]["cells"]["holder.nameOriginal"] == "GARMIN International Inc."
    assert rows[0]["cells"]["item.productTypeOriginal"] == "航空器"
    assert rows[0]["cells"]["item.nameOriginal"] == "GPS"
    assert rows[0]["cells"]["item.modelOriginal"] == "010-00182-() TSO-"
    assert rows[0]["cells"]["item.ctsoCodesOriginal"] == "TSO-C112"


def test_candidate_row_projection_restores_vtc_holder_suffix_from_product_type() -> None:
    """VTC product-type headers are centred right of their data-cell start."""

    words = (
        _word("序号", 32, 54, 80, 90),
        _word("证件编号", 60, 105, 80, 90),
        _word("持证人", 150, 185, 80, 90),
        _word("产品类型", 310, 355, 80, 90),
        _word("型别", 360, 385, 80, 90),
        _word("所属当局", 530, 575, 80, 90),
        _word("国外审定证件号", 635, 710, 80, 90),
        _word("最新批准日期", 750, 810, 80, 90),
        _word("394", 38, 55, 100, 110),
        _word("VTC0381P", 61, 108, 100, 110),
        _word("MT-Propeller", 115, 170, 100, 110),
        _word("Entwicklung", 172, 224, 100, 110),
        _word("GmbH", 225, 251, 100, 110),
        _word("螺旋桨", 310, 340, 100, 110),
        _word("MTV-27-1", 360, 405, 100, 110),
        _word("欧盟航空安全局/EASA", 530, 626, 100, 110),
        _word("EASA.P.104", 635, 687, 100, 110),
        _word("2024-09-19", 750, 798, 100, 110),
    )

    rows, _ = extract_candidate_rows(CatalogPage(16826, words=words), "vtc")

    assert rows[0]["cells"]["holder.nameOriginal"] == "MT-Propeller Entwicklung GmbH"
    assert rows[0]["cells"]["item.productTypeOriginal"] == "螺旋桨"


def test_candidate_row_projection_restores_tda_holder_suffix_before_model_cell() -> None:
    words = (
        _word("序号", 93.24, 115.43, 80, 90),
        _word("证件编号", 130.33, 174.82, 80, 90),
        _word("持有人", 255.85, 289.19, 80, 90),
        _word("型号", 474.10, 496.29, 80, 90),
        _word("最新批准日期", 679.44, 746.23, 80, 90),
        _word("15", 99.36, 109.42, 100, 110),
        _word("TDA-LSA-0006A", 130.23, 203.15, 100, 110),
        _word("EVEKTOR-AEROTECHNIK", 255.72, 376.59, 100, 110),
        _word("a.s", 379.08, 389.93, 100, 110),
        _word("SPORTSTAR", 474.00, 530.65, 100, 110),
        _word("SL", 533.18, 544.73, 100, 110),
        _word("2012-08-10", 696.72, 743.46, 100, 110),
    )

    rows, _ = extract_candidate_rows(CatalogPage(10, words=words), "tda")

    assert rows[0]["cells"]["holder.nameOriginal"] == "EVEKTOR-AEROTECHNIK a.s"
    assert rows[0]["cells"]["item.modelOriginal"] == "SPORTSTAR SL"


def test_candidate_row_projection_isolates_vda_identifier_and_reassigns_holder_overlap() -> None:
    words = (
        _word("序号", 40, 55, 80, 90),
        _word("证件编号", 80, 130, 80, 90),
        _word("持证人", 180, 220, 80, 90),
        _word("产品类别", 260, 300, 80, 90),
        _word("产品名称", 310, 350, 80, 90),
        _word("型（件）号", 400, 450, 80, 90),
        _word("CTSO", 500, 530, 80, 90),
        _word("所属当局", 550, 590, 80, 90),
        _word("最新批准日期", 650, 720, 80, 90),
        _word("1", 40, 45, 100, 110),
        _word("VDA0004", 80, 125, 100, 110),
        _word("Garmin", 126, 160, 100, 110),
        _word("International", 180, 230, 100, 110),
        _word("航空器", 260, 300, 100, 110),
        _word("GPS", 310, 340, 100, 110),
        _word("066-001", 400, 450, 100, 110),
        _word("TSO-C112", 500, 550, 100, 110),
        _word("FAA", 550, 580, 100, 110),
        _word("1998-03-26", 650, 710, 100, 110),
    )

    rows, _ = extract_candidate_rows(CatalogPage(17051, words=words), "vda")

    assert rows[0]["cells"]["approval.numberOriginal"] == "VDA0004"
    assert rows[0]["cells"]["holder.nameOriginal"] == "Garmin International"
    assert "approval_identifier_extraneous_text" in rows[0]["qualitySignalCodes"]


def test_candidate_row_projection_isolates_foreign_recognition_identifier_and_keeps_model_prefix() -> None:
    words = (
        _word("序号", 40, 55, 80, 90),
        _word("持有人", 80, 120, 80, 90),
        _word("项目名称", 180, 230, 80, 90),
        _word("认可型号", 300, 350, 80, 90),
        _word("对应TSOA证件号", 410, 470, 80, 90),
        _word("对应TSO编号", 520, 570, 80, 90),
        _word("认可编号", 620, 660, 80, 90),
        _word("认可日期", 700, 750, 80, 90),
        _word("1", 40, 45, 100, 110),
        _word("示例持证人", 80, 150, 100, 110),
        _word("厨房手推车", 180, 240, 100, 110),
        _word("SBA-C(", 300, 350, 100, 110),
        _word("),", 410, 425, 100, 110),
        _word("SBA-D(", 426, 465, 100, 110),
        _word("TSOA0045-HD-003", 440, 490, 100, 110),
        _word("CTSO-C175", 520, 580, 100, 110),
        _word("FAA", 620, 640, 100, 110),
        _word("2020-02-06", 700, 750, 100, 110),
    )

    rows, _ = extract_candidate_rows(CatalogPage(16801, words=words), "foreign_tso_recognition")

    assert rows[0]["cells"]["approval.numberOriginal"] == "TSOA0045-HD-003"
    assert rows[0]["cells"]["item.modelOriginal"] == "SBA-C( ), SBA-D("
    assert "approval_identifier_extraneous_text" in rows[0]["qualitySignalCodes"]


def test_extractor_keeps_empty_text_only_page_fail_closed() -> None:
    extractor = CaacApprovedCatalogV1Extractor()
    result = extractor.extract(
        pages=(CatalogPage(162, "七、已获批准的PMA产品目录清单"),),
        source_snapshot_sha256="a" * 64,
        parser_version="0.1.0",
    )

    assert result.records == ()
    assert result.continuation_state["lastSectionKey"] == "pma_item"
    assert result.candidate_rows == ()


def test_extractor_uses_context_for_pma_merged_cells_but_emits_only_owned_page() -> None:
    header_words = (
        _word("序号", 40, 55, 80, 90),
        _word("项目单编号", 80, 130, 80, 90),
        _word("持证人", 180, 220, 80, 90),
        _word("项目名称", 300, 350, 80, 90),
        _word("件号", 400, 430, 80, 90),
        _word("有效期至", 500, 550, 80, 90),
    )
    context_page = CatalogPage(
        162,
        "七、已获批准的PMA产品目录清单",
        words=(
            *header_words,
            _word("1", 40, 45, 100, 110),
            _word("PMA0001", 80, 125, 100, 110),
            _word("示例持证人", 180, 230, 100, 110),
            _word("过滤器", 300, 330, 100, 110),
            _word("P-001", 400, 430, 100, 110),
            _word("2026-07-01", 500, 550, 100, 110),
        ),
    )
    owned_page = CatalogPage(
        163,
        "七、已获批准的PMA产品目录清单",
        words=(
            *header_words,
            _word("2", 40, 45, 100, 110),
            _word("P-002", 400, 430, 100, 110),
        ),
    )

    result = CaacApprovedCatalogV1Extractor().extract(
        pages=(context_page, owned_page),
        source_snapshot_sha256="a" * 64,
        parser_version="0.1.0",
        continuation_state={
            "ownedPageStart": 163,
            "ownedPageEnd": 163,
            "sectionKey": "pma_item",
        },
    )

    assert len(result.records) == 1
    record = result.records[0]
    assert record["source"]["pdfPageStart"] == 162
    assert record["source"]["pdfPageEnd"] == 163
    assert record["approval"]["numberOriginal"] == "PMA0001"
    assert record["holder"]["nameOriginal"] == "示例持证人"
    assert record["item"]["partNumberOriginal"] == "P-002"
    assert [row["pdfPage"] for row in result.candidate_rows] == [163]
    assert {signal["code"] for signal in result.quality_signals} >= {
        "cross_part_context_used",
        "inherited_merged_cell",
    }


def test_candidate_record_identity_includes_item_context_not_just_approval_number() -> None:
    row = {
        "rowNumber": 1,
        "pdfPage": 162,
        "cells": {
            "approval.numberOriginal": "PMA0001-001-ZN",
            "holder.nameOriginal": "示例持证人",
            "item.nameOriginal": "过滤器",
            "item.partNumberOriginal": "P-001",
            "item.replacedManufacturerOriginal": "OEM A",
            "item.replacedPartNumberOriginal": "OEM-P-001",
            "approval.expiresOn": "2026-07-01",
        },
        "fieldEvidence": [
            {
                "fieldPath": "approval.numberOriginal",
                "textOriginal": "PMA0001-001-ZN",
                "pdfPage": 162,
                "bbox": [1, 2, 3, 4],
                "sourceWordIds": ["p00162w00001"],
            }
        ],
    }

    first, first_signals = build_candidate_record(
        row=row,
        section_key="pma_item",
        source_snapshot_sha256="a" * 64,
        parser_version="0.1.0",
    )
    changed = dict(row)
    changed["cells"] = {**row["cells"], "item.partNumberOriginal": "P-002"}
    second, second_signals = build_candidate_record(
        row=changed,
        section_key="pma_item",
        source_snapshot_sha256="a" * 64,
        parser_version="0.1.0",
    )

    assert first_signals == second_signals == ()
    assert first is not None and second is not None
    assert first["approvalType"] == second["approvalType"] == "pma"
    assert first["approval"]["numberNormalized"] == second["approval"]["numberNormalized"]
    assert first["recordId"] != second["recordId"]
    assert first["quality"]["status"] == "candidate"


def test_candidate_record_preserves_catalog_attributes_with_their_evidence() -> None:
    record, signals = build_candidate_record(
        row={
            "rowNumber": 191,
            "pdfPage": 161,
            "cells": {
                "approval.numberOriginal": "PMA0277-XN",
                "holder.nameOriginal": "四川川航航空发动机维修工程有限责任公司",
                "item.descriptionOriginal": "质量管理手册 R0",
                "attributes.qualityManualNumber": "SAECO-QMM-001",
                "attributes.qualityManualRevision": "R0",
                "approval.latestApprovedOn": "2024-12-11",
            },
            "fieldEvidence": [
                {
                    "fieldPath": "attributes.qualityManualNumber",
                    "textOriginal": "SAECO-QMM-001",
                    "pdfPage": 161,
                    "bbox": [1, 2, 3, 4],
                    "sourceWordIds": ["p00161w00153"],
                }
            ],
        },
        section_key="pma_holder",
        source_snapshot_sha256="a" * 64,
        parser_version="0.1.0",
    )

    assert signals == ()
    assert record is not None
    assert record["attributes"] == {
        "qualityManualNumber": "SAECO-QMM-001",
        "qualityManualRevision": "R0",
    }
    assert record["fieldEvidence"][0]["fieldPath"] == "attributes.qualityManualNumber"


def test_candidate_record_normalizes_date_token_in_noisy_cell_and_keeps_signal() -> None:
    record, signals = build_candidate_record(
        row={
            "rowNumber": 1,
            "pdfPage": 55,
            "cells": {
                "approval.numberOriginal": "MDA001",
                "holder.nameOriginal": "示例持证人",
                "approval.latestApprovedOn": "2003-12-30 设计描述残留",
            },
            "fieldEvidence": [],
        },
        section_key="mda",
        source_snapshot_sha256="a" * 64,
        parser_version="0.1.0",
    )

    assert record is not None
    assert record["approval"]["latestApprovedOn"] == "2003-12-30"
    assert any(signal["code"] == "date_text_contains_extra" for signal in signals)


def test_vstc_outer_table_rules_do_not_merge_adjacent_rows() -> None:
    words = (
        _word("序号", 20, 40, 80, 90),
        _word("证件编号", 45, 95, 80, 90),
        _word("持证人", 100, 165, 80, 90),
        _word("产品类型", 170, 205, 80, 90),
        _word("适用机型", 210, 280, 80, 90),
        _word("叙述", 285, 375, 80, 90),
        _word("所属当局", 380, 450, 80, 90),
        _word("国外审定证件号", 455, 510, 80, 90),
        _word("最新批准日期", 515, 580, 80, 90),
        _word("977", 20, 40, 112, 122),
        _word("VSTC1098", 45, 95, 112, 122),
        _word("The NORDAM", 100, 165, 105, 115),
        _word("Group LLC", 100, 150, 119, 129),
        _word("飞机", 170, 205, 112, 122),
        _word("B737", 210, 260, 112, 122),
        _word("改装项目一", 285, 355, 112, 122),
        _word("FAA", 380, 420, 112, 122),
        _word("STC-ONE", 455, 505, 112, 122),
        _word("2024-10-31", 515, 580, 112, 122),
        _word("978", 20, 40, 162, 172),
        _word("VSTC1099", 45, 95, 162, 172),
        _word("Garmin", 100, 145, 155, 165),
        _word("International,Inc.", 100, 165, 169, 179),
        _word("飞机", 170, 205, 162, 172),
        _word("B777", 210, 260, 162, 172),
        _word("改装项目二", 285, 355, 162, 172),
        _word("FAA", 380, 420, 162, 172),
        _word("STC-TWO", 455, 505, 162, 172),
        _word("2024-11-28", 515, 580, 162, 172),
    )
    page = CatalogPage(
        17050,
        words=words,
        metadata={
            "pageHeight": 220.0,
            "rects": (
                {"top": 94.0, "bottom": 95.0, "width": 560.0},
                {"top": 190.0, "bottom": 191.0, "width": 560.0},
            ),
        },
    )

    rows, signals = extract_candidate_rows(page, "vstc")

    assert not any(signal["code"] == "header_not_recognized" for signal in signals)
    assert [row["rowNumber"] for row in rows] == [977, 978]
    assert [row["cells"]["holder.nameOriginal"] for row in rows] == [
        "The NORDAM Group LLC",
        "Garmin International,Inc.",
    ]
    assert [row["cells"]["approval.latestApprovedOn"] for row in rows] == [
        "2024-10-31",
        "2024-11-28",
    ]


def test_vstc_reassigns_wrapped_model_and_description_words_by_physical_grid() -> None:
    words = (
        _word("序号", 33, 56, 83, 94),
        _word("证件编号", 66, 110, 83, 94),
        _word("持证人", 145, 178, 83, 94),
        _word("产品类型", 209, 253, 83, 94),
        _word("适用机型", 288, 333, 83, 94),
        _word("叙述", 448, 470, 83, 94),
        _word("所属当局", 556, 600, 83, 94),
        _word("国外审定证件号", 657, 735, 83, 94),
        _word("最新批准日期", 739, 806, 83, 94),
        _word("977", 42, 47, 116, 127),
        _word("VSTC1098", 60, 106, 116, 127),
        _word("NORDAM", 119, 160, 116, 127),
        _word("航空器", 208, 238, 117, 127),
        _word("737-900", 258, 310, 110, 120),
        _word("System", 366, 396, 110, 120),
        _word("sample", 397, 430, 110, 120),
        _word("737-8", 258, 290, 123, 133),
        _word("FAA", 556, 600, 116, 127),
        _word("ST09928AC", 657, 708, 116, 127),
        _word("2024-10-31", 751, 798, 116, 127),
        _word("978", 42, 47, 163, 173),
        _word("VSTC1099", 60, 106, 163, 173),
        _word("Garmin", 119, 160, 163, 173),
        _word("航空器", 208, 238, 163, 173),
        _word("Bell", 258, 275, 163, 173),
        _word("505", 277, 292, 163, 173),
        _word("Installation", 366, 411, 150, 160),
        _word("Control", 366, 397, 163, 173),
        _word("Canada", 509, 539, 163, 173),
        _word("Limited", 366, 398, 176, 186),
        _word("FAA", 556, 600, 163, 173),
        _word("SR01961WI", 657, 708, 163, 173),
        _word("2024-11-28", 751, 798, 163, 173),
    )
    page = CatalogPage(
        17050,
        words=words,
        metadata={"pageHeight": 595.0, "rects": ()},
    )

    rows, _ = extract_candidate_rows(page, "vstc")

    assert [row["cells"]["item.productTypeOriginal"] for row in rows] == [
        "航空器",
        "航空器",
    ]
    assert [row["cells"]["item.modelOriginal"] for row in rows] == [
        "737-900 737-8",
        "Bell 505",
    ]
    assert [row["cells"]["item.descriptionOriginal"] for row in rows] == [
        "System sample",
        "Installation Control Canada Limited",
    ]
    assert [row["cells"]["attributes.authority"] for row in rows] == ["FAA", "FAA"]


def test_candidate_record_removes_only_cjk_layout_spaces_from_holder_name() -> None:
    cases = (
        ("成都富凯飞机工程服 务有限公司", "成都富凯飞机工程服务有限公司"),
        ("湖北航宇嘉泰飞机设 备有限公司", "湖北航宇嘉泰飞机设备有限公司"),
        ("The NORDAM Group LLC", "The NORDAM Group LLC"),
        (
            "AlliedSignal Commercial Avionics Systems",
            "AlliedSignal Commercial Avionics Systems",
        ),
    )
    for holder_input, expected in cases:
        record, signals = build_candidate_record(
            row={
                "rowNumber": 1,
                "pdfPage": 55,
                "cells": {
                    "approval.numberOriginal": "MDA001",
                    "holder.nameOriginal": holder_input,
                },
                "fieldEvidence": [],
            },
            section_key="mda",
            source_snapshot_sha256="a" * 64,
            parser_version="0.1.0",
        )

        assert signals == ()
        assert record is not None
        assert record["holder"]["nameOriginal"] == expected


def test_v2_candidate_record_allows_only_an_explicitly_reviewed_empty_holder_cell() -> None:
    row = {
        "rowNumber": 875,
        "pdfPage": 17023,
        "cells": {
            "approval.numberOriginal": "VSTC0996",
            "holder.nameOriginal": "",
            "item.productTypeOriginal": "航空器",
            "item.modelOriginal": "BD-700-1A10/BD-700-1A11",
            "approval.latestApprovedOn": "2021-05-31",
        },
        "fieldEvidence": [
            {
                "fieldPath": "approval.numberOriginal",
                "textOriginal": "VSTC0996",
                "pdfPage": 17023,
                "bbox": [65.0, 231.0, 110.0, 244.0],
                "sourceWordIds": ["p17023w001"],
            }
        ],
    }
    review = {
        "reviewId": "vstc-2025-p17023-r875-holder-empty",
        "sectionKey": "vstc",
        "rowNumber": 875,
        "pdfPage": 17023,
        "fieldPath": "holder.nameOriginal",
        "reasonCode": "empty_source_cell",
        "bbox": [111.0, 231.19471025, 203.0, 409.31127575],
        "reviewedOn": "2026-08-17",
    }

    rejected, rejected_signals = build_candidate_record(
        row=row,
        section_key="vstc",
        source_snapshot_sha256="a" * 64,
        parser_version="0.1.0",
        schema_version="caac-approved-catalog.v2",
    )
    assert rejected is None
    assert [signal["code"] for signal in rejected_signals] == ["required_field_missing"]

    record, signals = build_candidate_record(
        row=row,
        section_key="vstc",
        source_snapshot_sha256="a" * 64,
        parser_version="0.1.0",
        extractor_version="2.0.0",
        schema_version="caac-approved-catalog.v2",
        source_omission=review,
    )

    assert record is not None
    assert record["schemaVersion"] == "caac-approved-catalog.v2"
    assert record["holder"] == {
        "status": "source_missing",
        "nameOriginal": None,
        "nameNormalized": None,
        "sourceOmission": {
            "reviewId": review["reviewId"],
            "fieldPath": review["fieldPath"],
            "reasonCode": review["reasonCode"],
            "pdfPage": review["pdfPage"],
            "bbox": review["bbox"],
            "sourceSnapshotSha256": "a" * 64,
            "reviewedOn": review["reviewedOn"],
        },
    }
    assert record["source"]["cells"][-1] == {
        "columnKey": "holder.nameOriginal",
        "textOriginal": "",
        "pdfPage": 17023,
        "bbox": review["bbox"],
        "sourceWordIds": [],
    }
    assert record["fieldEvidence"][-1]["sourceCellId"] == (
        "source-omission:vstc-2025-p17023-r875-holder-empty"
    )
    assert [signal["code"] for signal in signals] == ["source_omission_reviewed"]
