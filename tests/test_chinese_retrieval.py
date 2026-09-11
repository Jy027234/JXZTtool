from parsecore.models import Chunk
from parsecore.runtime import _keyword_match_score, _keyword_relevance_score, _tokenize


def test_chinese_phrase_inside_sentence_is_retrievable():
    chunk = Chunk(chunk_id="c1", doc_id="d1", block_ids=("b1",),
                  text="本机型的检查周期为750小时，适用于测试构型。")
    assert _keyword_relevance_score(query="检查周期", chunk=chunk) > 0.5
    assert _keyword_match_score(query="检查周期", text=chunk.text) > 0.5
    assert _keyword_relevance_score(query="氧气瓶更换", chunk=chunk) == 0


def test_mixed_chinese_numbers_and_identifiers_remain_separate():
    assert _tokenize("检查周期750小时 PN-ABC_01") == (
        "检查", "查周", "周期", "750", "小时", "pn", "abc_01",
    )
    assert _tokenize("AD 2026-01 FAA") == ("ad", "2026", "01", "faa")
    assert _tokenize("") == ()
    assert _tokenize("中") == ("中",)


def test_last_page_fact_is_not_drowned_by_unrelated_chinese_sentences():
    texts = ["旅客服务流程和地面运输安排。", "厨房设备检查及饮用水加注。", "检查周期为750小时。"]
    scores = [_keyword_match_score(query="检查周期", text=text) for text in texts]
    assert scores.index(max(scores)) == 2
