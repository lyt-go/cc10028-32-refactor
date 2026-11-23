"""共享文档产物定位器：收拢文档版本与抽取血缘两侧各自拼路径的产物读取。

一次定位识别同一来源组（source_key，即文档编号/解析任务键）下的全部抽取
产物，返回不可变的定位结果：

- 每个产物种类只记录经 ``exists()`` 确认过的路径（确认依据含全部候选路径），
  消费方只能读取确认路径，缺失产物维持既有语义（读空 / 存在性为 False）；
- 定位结果带来源组身份标识 ``group_token``，快照与血缘即使并发发起，也各自
  持有同一 source_key 的独立定位结果，不会把两组产物计入同一个结果；
- 兼容历史路径：现行命名（带 ``_parsed`` 中缀）优先，缺失时回退到无中缀的
  历史命名，并在绑定结果中标注 ``legacy``。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from app.services.base import DATA_ROOT

# (产物种类, 所在子目录, 现行文件名模板, 历史文件名候选模板)
# 历史命名与 chunk_split 中既有的无 `_parsed` 中缀备选路径口径保持一致。
ARTIFACT_SPECS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("parsed", "parsed", "{source}_parsed.jsonl", ("{source}.jsonl",)),
    ("chunks", "chunks", "{source}_parsed_chunks.jsonl", ("{source}_chunks.jsonl",)),
    ("entities", "entities", "{source}_parsed_entities.jsonl", ("{source}_entities.jsonl",)),
    ("relations", "relations", "{source}_parsed_relations.jsonl", ("{source}_relations.jsonl",)),
    ("graph", "graph", "{source}_parsed_graph.jsonl", ("{source}_graph.jsonl",)),
    ("vectors", "vectors", "{source}_parsed_vectors.jsonl", ("{source}_vectors.jsonl",)),
)

ARTIFACT_KINDS: tuple[str, ...] = tuple(spec[0] for spec in ARTIFACT_SPECS)


@dataclass(frozen=True)
class ArtifactBinding:
    """单个产物种类的定位绑定：候选路径与确认结果在定位时一次性固定。"""

    kind: str
    candidates: tuple[Path, ...]
    path: Path | None
    legacy: bool

    @property
    def exists(self) -> bool:
        return self.path is not None


@dataclass(frozen=True)
class DocArtifactGroup:
    """同一来源组全部产物的不可变定位结果，供版本快照与抽取血缘共享消费。"""

    source_key: str
    bindings: Mapping[str, ArtifactBinding]
    group_token: str
    located_at: float
    data_root: Path

    def binding(self, kind: str) -> ArtifactBinding:
        return self.bindings[kind]

    def exists(self, kind: str) -> bool:
        return self.bindings[kind].exists

    def path(self, kind: str) -> Path | None:
        return self.bindings[kind].path

    def read(self, kind: str, reader: Callable[[Path], list[dict]]) -> list[dict]:
        """只读取定位时已确认存在的同组产物；缺失按既有语义返回空列表。"""
        binding = self.bindings[kind]
        if binding.path is None:
            return []
        return reader(binding.path)

    def coverage(self) -> dict[str, bool]:
        """返回各类产物的存在性覆盖表，口径与血缘校验原 coverage 一致。"""
        return {kind: self.bindings[kind].exists for kind in ARTIFACT_KINDS}

    def describe_scope(self, reader: Callable[[Path], list[dict]]) -> dict[str, dict]:
        """说明本次定位核验的是文档的哪组页、分块和关系。"""
        pages = self.read("parsed", reader)
        chunks = self.read("chunks", reader)
        relations = self.read("relations", reader)
        return {
            "pages": {
                "count": len(pages),
                "page_nums": [p.get("page_num") for p in pages if p.get("page_num") is not None],
            },
            "chunks": {
                "count": len(chunks),
                "chunk_ids": [c.get("chunk_id") for c in chunks if c.get("chunk_id")],
            },
            "relations": {
                "count": len(relations),
                "relation_ids": [r.get("relation_id") for r in relations if r.get("relation_id")],
            },
        }

    def as_dict(self) -> dict:
        """序列化定位依据，便于写入检查点与接口返回，说明数据读取自哪一组。"""
        return {
            "source_key": self.source_key,
            "group_token": self.group_token,
            "located_at": self.located_at,
            "data_root": str(self.data_root),
            "artifacts": {
                kind: {
                    "exists": binding.exists,
                    "path": str(binding.path) if binding.path else None,
                    "legacy": binding.legacy,
                    "candidates": [str(p) for p in binding.candidates],
                }
                for kind, binding in self.bindings.items()
            },
        }


class DocArtifactLocator:
    """文档产物定位器：每次 locate 独立扫描文件系统并返回新的不可变来源组。"""

    def __init__(self, data_root: Path | str = DATA_ROOT):
        self.data_root = Path(data_root)

    def locate(self, source_key: str) -> DocArtifactGroup:
        bindings: dict[str, ArtifactBinding] = {}
        confirmed: list[str] = []
        for kind, subdir, current_tpl, legacy_tpls in ARTIFACT_SPECS:
            directory = self.data_root / subdir
            candidates = [directory / current_tpl.format(source=source_key)]
            candidates.extend(directory / tpl.format(source=source_key) for tpl in legacy_tpls)

            # 现行路径优先；全部候选都不存在时维持“找不到产物”的既有语义。
            path: Path | None = None
            legacy = False
            for idx, candidate in enumerate(candidates):
                if candidate.exists():
                    path = candidate
                    legacy = idx > 0
                    break

            bindings[kind] = ArtifactBinding(
                kind=kind,
                candidates=tuple(candidates),
                path=path,
                legacy=legacy,
            )
            if path is not None:
                confirmed.append(f"{kind}={path.name}")

        token_source = f"{source_key}|{'|'.join(confirmed)}"
        group_token = hashlib.sha256(token_source.encode("utf-8")).hexdigest()[:16]
        return DocArtifactGroup(
            source_key=source_key,
            bindings=MappingProxyType(bindings),
            group_token=group_token,
            located_at=time.time(),
            data_root=self.data_root,
        )


_DEFAULT_LOCATOR = DocArtifactLocator()


def locate_doc_artifacts(source_key: str) -> DocArtifactGroup:
    """使用默认数据根目录定位一个文档来源组的全部产物。"""
    return _DEFAULT_LOCATOR.locate(source_key)
