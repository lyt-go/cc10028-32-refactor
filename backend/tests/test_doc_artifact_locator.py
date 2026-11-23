"""共享文档产物定位器及其在文档版本/抽取血缘两侧消费的专项测试。

覆盖：
- 正常路径（现行命名）的整组定位与页/分块/关系范围说明；
- 存量兼容（缺一个产物的旧文档，走无 `_parsed` 中缀的历史命名）；
- 结果完整的新文档，快照计数与血缘完整性原口径不变；
- 快照与血缘并发/先后发起时各自只采信有确认依据的同一来源组；
- 同一份文档两个版本产物并存、定位中途出现新产物的输出；
- 找不到产物时保留既有语义（空统计 / 存在断链）。
"""
import os
import sys
import threading
import uuid as _uuid

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.services.base import DATA_ROOT
from app.services.doc_artifacts import (
    ARTIFACT_KINDS,
    DocArtifactGroup,
    DocArtifactLocator,
    locate_doc_artifacts,
)
from app.services.doc_version import DocVersionService
from app.services.extract_lineage import ExtractLineageService


def _uid(prefix="DOC"):
    return f"{prefix}-{_uuid.uuid4().hex[:8].upper()}"


def _write_artifact(subdir: str, filename: str, records: list[dict]) -> str:
    path = DATA_ROOT / subdir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    DocVersionService()._write_jsonl(path, records)
    return str(path)


def _current_name(source: str, kind: str) -> str:
    suffix = {
        "parsed": "_parsed",
        "chunks": "_parsed_chunks",
        "entities": "_parsed_entities",
        "relations": "_parsed_relations",
        "graph": "_parsed_graph",
        "vectors": "_parsed_vectors",
    }[kind]
    return f"{source}{suffix}.jsonl"


def _legacy_name(source: str, kind: str) -> str:
    suffix = {
        "parsed": "",
        "chunks": "_chunks",
        "entities": "_entities",
        "relations": "_relations",
        "graph": "_graph",
        "vectors": "_vectors",
    }[kind]
    return f"{source}{suffix}.jsonl"


def _seed_current_group(source: str, *, kinds=ARTIFACT_KINDS) -> None:
    """按现行命名写全一组产物。"""
    for kind in kinds:
        if kind == "parsed":
            recs = [{"doc_id": source, "page_num": i} for i in range(1, 4)]
        elif kind == "chunks":
            recs = [{"chunk_id": f"CHUNK-{source}-{i:04d}", "page": i} for i in range(1, 6)]
        elif kind == "entities":
            recs = [{"entity_id": f"ENT-{i:06d}", "entity_text": f"E{i}"} for i in range(3)]
        elif kind == "relations":
            recs = [{"relation_id": f"REL-{i:06d}", "head_entity": "A", "tail_entity": "B"} for i in range(4)]
        elif kind == "graph":
            recs = [{"triple_id": f"TRIPLE-{i}", "head": "A", "tail": "B"} for i in range(2)]
        else:
            recs = [{"chunk_id": f"CHUNK-{source}-{i}", "vector_dim": 8} for i in range(5)]
        _write_artifact(kind, _current_name(source, kind), recs)


class TestSharedLocator:
    """定位器自身行为：整组识别、确认依据、历史兼容、来源组隔离。"""

    def test_locate_identifies_whole_source_group(self):
        source = _uid("GRP")
        _seed_current_group(source)

        group = locate_doc_artifacts(source)
        assert isinstance(group, DocArtifactGroup)
        assert group.source_key == source
        assert set(group.coverage().keys()) == set(ARTIFACT_KINDS)
        assert all(group.exists(kind) for kind in ARTIFACT_KINDS)
        # 确认依据：每个绑定只指向实际存在的同组文件，且现行命名优先、非历史
        for kind in ARTIFACT_KINDS:
            binding = group.binding(kind)
            assert binding.path is not None
            assert binding.path.name == _current_name(source, kind)
            assert binding.legacy is False
            assert any(str(p).endswith(_current_name(source, kind)) for p in binding.candidates)

    def test_scope_describes_pages_chunks_relations(self):
        source = _uid("SCP")
        _seed_current_group(source)

        group = locate_doc_artifacts(source)
        scope = group.describe_scope(DocVersionService()._read_jsonl)
        assert scope["pages"] == {"count": 3, "page_nums": [1, 2, 3]}
        assert scope["chunks"]["count"] == 5
        assert scope["chunks"]["chunk_ids"][0] == f"CHUNK-{source}-0001"
        assert scope["relations"] == {"count": 4, "relation_ids": [f"REL-{i:06d}" for i in range(4)]}

    def test_legacy_paths_fallback_for_old_documents(self):
        """存量兼容：旧文档只有无 `_parsed` 中缀的历史命名时仍可定位。"""
        source = _uid("OLD")
        legacy_paths = {}
        for kind in ARTIFACT_KINDS:
            if kind == "parsed":
                recs = [{"doc_id": source, "page_num": 1}]
            elif kind == "relations":
                recs = [{"relation_id": "REL-000001"}]
            else:
                recs = [{"id": f"{kind}-1"}]
            legacy_paths[kind] = _write_artifact(kind, _legacy_name(source, kind), recs)

        group = locate_doc_artifacts(source)
        for kind in ARTIFACT_KINDS:
            assert group.exists(kind), f"历史命名产物 {kind} 应被兼容定位"
            assert group.binding(kind).legacy is True
            assert str(group.path(kind)) == legacy_paths[kind]
            # 现行候选路径仍在确认依据中，证明先查现行再回退历史
            assert any(p.name == _current_name(source, kind) for p in group.binding(kind).candidates)

    def test_current_path_takes_precedence_over_legacy(self):
        """现行与历史命名同时存在时，只确认现行路径，不把两份计入同一结果。"""
        source = _uid("MIX")
        _write_artifact("parsed", _current_name(source, "parsed"),
                        [{"doc_id": source, "page_num": 1}, {"doc_id": source, "page_num": 2}])
        _write_artifact("parsed", _legacy_name(source, "parsed"), [{"doc_id": source, "page_num": 9}])

        group = locate_doc_artifacts(source)
        binding = group.binding("parsed")
        assert binding.path.name == _current_name(source, "parsed")
        assert binding.legacy is False
        pages = group.read("parsed", DocVersionService()._read_jsonl)
        assert len(pages) == 2  # 只读取确认的现行文件，历史文件不被计入

    def test_missing_artifacts_keep_existing_semantics(self):
        """找不到产物：绑定缺失、read 返回空列表，不抛异常。"""
        source = _uid("MISS")
        group = locate_doc_artifacts(source)
        assert not any(group.coverage().values())
        for kind in ARTIFACT_KINDS:
            assert group.binding(kind).path is None
            assert group.binding(kind).legacy is False
        assert group.read("parsed", DocVersionService()._read_jsonl) == []

    def test_separate_source_groups_never_mixed(self):
        """一次定位只识别同一来源组，两组产物不会计入同一个结果。"""
        source_a = _uid("GA")
        source_b = _uid("GB")
        _seed_current_group(source_a)
        _seed_current_group(source_b)

        group_a = locate_doc_artifacts(source_a)
        group_b = locate_doc_artifacts(source_b)
        assert group_a.group_token != group_b.group_token
        for kind in ARTIFACT_KINDS:
            assert source_a in str(group_a.path(kind))
            assert source_b in str(group_b.path(kind))
            assert source_b not in str(group_a.path(kind))

    def test_snapshot_of_artifact_appearing_midway(self):
        """定位中途出现新产物：两次定位各自只统计当时已确认的产物。"""
        source = _uid("MID")
        # 先只有解析与分块（旧文档缺一个产物的形态）
        _seed_current_group(source, kinds=("parsed", "chunks"))
        locator = DocArtifactLocator()

        first = locator.locate(source)
        assert first.exists("parsed") and first.exists("chunks")
        assert not first.exists("entities")
        token_before = first.group_token

        # 中途补齐实体与关系产物
        _write_artifact("entities", _current_name(source, "entities"),
                        [{"entity_id": f"ENT-{i:06d}"} for i in range(3)])
        _write_artifact("relations", _current_name(source, "relations"),
                        [{"relation_id": f"REL-{i:06d}"} for i in range(2)])

        second = locator.locate(source)
        assert first.exists("entities") is False  # 旧结果保持不可变
        assert second.exists("entities") and second.exists("relations")
        assert second.group_token != token_before   # 来源组内容变化产生新身份


class TestDocVersionConsumesLocator:
    """文档版本侧：共享定位结果，快照计数与持久化接口原口径不变。"""

    def test_snapshot_counts_complete_new_document(self):
        source = _uid("NEW")
        _seed_current_group(source)
        svc = DocVersionService()

        result = svc.create_snapshot(source, "v1.0")
        # 原返回字段与计数口径
        assert result["doc_id"] == source
        assert result["pages"] == 3
        assert result["chunks"] == 5
        assert result["entities"] == 3
        assert result["triples"] == 2
        assert result["snapshot"]["分块数量"] == 5
        assert result["snapshot"]["实体数量"] == 3
        # 新增：来源组身份与核验范围
        assert result["artifact_group"]["source_key"] == source
        assert result["scope"]["chunks"]["count"] == 5
        # 原持久化接口不变
        snap_file = DATA_ROOT / "versions" / f"{source}_vv1.0.jsonl"
        assert snap_file.exists()
        cp = svc._read_checkpoint(f"version_{source}_v1.0")
        assert cp["chunks"] == 5 and cp["entities"] == 3
        assert cp["artifact_group"]["group_token"] == result["artifact_group"]["group_token"]

    def test_snapshot_on_legacy_old_document_missing_one_artifact(self):
        """缺少一个产物的旧文档：历史路径兼容，缺失产物按空统计（原语义）。"""
        source = _uid("LEG")
        # 旧命名：有 parsed/chunks/entities，缺 relations 与 graph
        _write_artifact("parsed", _legacy_name(source, "parsed"),
                        [{"doc_id": source, "page_num": 1}, {"doc_id": source, "page_num": 2}])
        _write_artifact("chunks", _legacy_name(source, "chunks"),
                        [{"chunk_id": f"CHUNK-{source}-{i:04d}"} for i in range(1, 4)])
        _write_artifact("entities", _legacy_name(source, "entities"),
                        [{"entity_id": f"ENT-{i:06d}"} for i in range(2)])

        result = DocVersionService().create_snapshot(source, "v1.0")
        assert result["pages"] == 2
        assert result["chunks"] == 3
        assert result["entities"] == 2
        assert result["triples"] == 0  # graph 缺失，计数为 0 而非报错
        assert result["artifact_group"]["artifacts"]["graph"]["exists"] is False
        assert result["artifact_group"]["artifacts"]["chunks"]["legacy"] is True

    def test_two_versions_of_same_document_coexist(self):
        """同一文档两个版本的产物/快照并存，计数各自基于各自定位结果。"""
        source = _uid("VER")
        _seed_current_group(source)
        svc = DocVersionService()

        first = svc.create_snapshot(source, "v1.0")
        # 第二版：实体产物追加新记录（_write_jsonl 按既有口径为附件模式）
        _write_artifact("entities", _current_name(source, "entities"),
                        [{"entity_id": f"ENT-{i:06d}"} for i in range(6)])
        second = svc.create_snapshot(source, "v2.0")

        assert first["snapshot"]["快照编号"] == f"SNAP-{source}-v1.0"
        assert second["snapshot"]["快照编号"] == f"SNAP-{source}-v2.0"
        assert first["entities"] == 3 and second["entities"] == 9
        assert (DATA_ROOT / "versions" / f"{source}_vv1.0.jsonl").exists()
        assert (DATA_ROOT / "versions" / f"{source}_vv2.0.jsonl").exists()
        # 变更摘要仍按原口径生成
        assert "实体数" in second["snapshot"]["变更摘要"]

    def test_snapshot_for_nonexistent_document_returns_zero_counts(self):
        source = _uid("NONE")
        result = DocVersionService().create_snapshot(source, "v1.0")
        assert result["pages"] == 0
        assert result["chunks"] == 0
        assert result["entities"] == 0
        assert result["triples"] == 0
        assert result["snapshot"]["变更摘要"] == "初始版本快照"


class TestLineageConsumesLocator:
    """抽取血缘侧：共享定位结果，完整性原口径与断链语义不变。"""

    def test_lineage_record_and_integrity_on_complete_document(self):
        source = _uid("LNEW")
        _seed_current_group(source)
        svc = ExtractLineageService()

        record = svc.record_from_parse(source)
        assert record["nodes"] >= 6
        assert "知识图谱系统" in record["downstream"]
        assert "向量检索引擎" in record["downstream"]
        assert record["artifact_group"]["source_key"] == source
        assert record["scope"]["relations"]["count"] == 4

        integrity = svc.validate_lineage_integrity(source)
        # 原口径：四项全在为“完整”
        assert integrity["checks"] == {"parsed": True, "chunks": True,
                                       "entities": True, "relations": True}
        assert integrity["integrity"] == "完整"
        assert integrity["missing_nodes"] == []
        # 原持久化接口不变
        lineage_file = DATA_ROOT / "lineage" / f"{source}_lineage.jsonl"
        assert lineage_file.exists()
        cp = svc._read_checkpoint(f"lineage_{source}")
        assert cp["coverage"]["parsed"] is True

    def test_integrity_reports_broken_chain_for_old_incomplete_document(self):
        """缺一个产物的旧文档：历史命名能识别的识别，缺的仍按断链（原语义）。"""
        source = _uid("LLEG")
        _write_artifact("parsed", _legacy_name(source, "parsed"), [{"doc_id": source}])
        _write_artifact("chunks", _legacy_name(source, "chunks"), [{"chunk_id": "C1"}])
        # entities / relations 缺失
        svc = ExtractLineageService()

        integrity = svc.validate_lineage_integrity(source)
        assert integrity["checks"]["parsed"] is True
        assert integrity["checks"]["chunks"] is True
        assert integrity["checks"]["entities"] is False
        assert integrity["checks"]["relations"] is False
        assert integrity["integrity"] == "存在断链"
        assert set(integrity["missing_nodes"]) == {"entities", "relations"}

        record = svc.record_from_parse(source)
        cp = svc._read_checkpoint(f"lineage_{source}")
        assert cp["coverage"]["parsed"] is True
        assert cp["coverage"]["relations"] is False
        assert record["artifact_group"]["artifacts"]["parsed"]["legacy"] is True

    def test_integrity_for_nonexistent_document_still_broken(self):
        source = _uid("LNONE")
        integrity = ExtractLineageService().validate_lineage_integrity(source)
        assert integrity["integrity"] == "存在断链"
        assert set(integrity["missing_nodes"]) == {"parsed", "chunks", "entities", "relations"}
        assert all(v is False for v in integrity["checks"].values())


class TestConcurrentSnapshotAndLineage:
    """快照与血缘并发：各自独立定位，只读取有确认依据的同一来源组。"""

    def test_concurrent_requests_get_independent_confirmed_groups(self):
        source = _uid("CCN")
        _seed_current_group(source)
        version_svc = DocVersionService()
        lineage_svc = ExtractLineageService()
        outcomes: dict[str, dict] = {}
        barrier = threading.Barrier(2)

        def run_snapshot():
            barrier.wait()
            outcomes["snapshot"] = version_svc.create_snapshot(source, "v1.0")

        def run_lineage():
            barrier.wait()
            outcomes["lineage"] = lineage_svc.validate_lineage_integrity(source)

        t1 = threading.Thread(target=run_snapshot)
        t2 = threading.Thread(target=run_lineage)
        t1.start(); t2.start()
        t1.join(); t2.join()

        snap_group = outcomes["snapshot"]["artifact_group"]
        lin_group = outcomes["lineage"]["artifact_group"]
        # 同一来源组身份一致，且两边各自持有完整的确认依据
        assert snap_group["source_key"] == lin_group["source_key"] == source
        assert snap_group["group_token"] == lin_group["group_token"]
        for kind in ARTIFACT_KINDS:
            assert snap_group["artifacts"][kind]["exists"] is True
        assert outcomes["lineage"]["checks"] == {"parsed": True, "chunks": True,
                                                 "entities": True, "relations": True}
        # 计数与完整性原口径
        assert outcomes["snapshot"]["chunks"] == 5
        assert outcomes["snapshot"]["triples"] == 2

    def test_concurrent_requests_do_not_cross_source_groups(self):
        source_a = _uid("CA")
        source_b = _uid("CB")
        _seed_current_group(source_a, kinds=("parsed", "chunks"))
        _seed_current_group(source_b)
        outcomes = {}
        barrier = threading.Barrier(2)

        def run_a():
            barrier.wait()
            outcomes["a"] = ExtractLineageService().record_from_parse(source_a)

        def run_b():
            barrier.wait()
            outcomes["b"] = DocVersionService().create_snapshot(source_b, "v1.0")

        t1 = threading.Thread(target=run_a)
        t2 = threading.Thread(target=run_b)
        t1.start(); t2.start()
        t1.join(); t2.join()

        group_a = outcomes["a"]["artifact_group"]
        group_b = outcomes["b"]["artifact_group"]
        assert group_a["source_key"] == source_a
        assert group_b["source_key"] == source_b
        assert group_a["group_token"] != group_b["group_token"]
        # A 组没有的产物不能因为 B 组存在而被计入 A
        assert group_a["artifacts"]["entities"]["exists"] is False
        assert group_b["artifacts"]["entities"]["exists"] is True
        for kind, info in group_a["artifacts"].items():
            if info["path"]:
                assert source_a in info["path"]
                assert source_b not in info["path"]
