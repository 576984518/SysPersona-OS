# -*- coding: utf-8 -*-
"""Embedding Provider：hash（零依赖基线）/ ollama / fastembed。

选择顺序（auto）：
  1. 配置里显式指定 → 用它；
  2. ollama 在跑 → 用它（需要用户 pull 过 embedding 模型）；
  3. fastembed 已安装 → 用它（首次会下载模型）；
  4. 都没有 → hash 基线（明确标注：不是语义检索）。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from array import array
from typing import Protocol

HASH_DIM = 256
DEFAULT_MODELS = {
    "ollama": "bge-m3",
    "fastembed": "BAAI/bge-small-zh-v1.5",
    "openai": "bge-m3",
    "hash": "hash-v1",
}
_CJK = re.compile(r"[\u4e00-\u9fa5]+")
_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
_BATCH = 16


class EmbeddingProvider(Protocol):
    id: str
    model: str
    dim: int
    is_semantic: bool

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vec))
    if norm == 0:
        return vec
    return [value / norm for value in vec]


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for run in _CJK.findall(text):
        out.extend(run)
        out.extend(run[i : i + 2] for i in range(len(run) - 1))
    out.extend(word.lower() for word in _WORD.findall(text))
    return out


class HashProvider:
    """字号 n-gram 的带符号哈希投影。

    它能抓住「部分字面重叠」，但**不理解同义/改写**。只用于测试与兜底，
    不得对外宣称是语义检索。
    """

    id = "hash"
    model = DEFAULT_MODELS["hash"]
    dim = HASH_DIM
    is_semantic = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in _tokens(text):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                value = int.from_bytes(digest, "big")
                index = value % self.dim
                sign = 1.0 if (value >> 63) & 1 else -1.0
                vec[index] += sign
            vectors.append(_unit(vec))
        return vectors


class OllamaProvider:
    """本地 ollama 的 embedding 接口；不需要新增 Python 依赖。"""

    id = "ollama"
    is_semantic = True

    def __init__(self, model: str | None = None, host: str = "http://127.0.0.1:11434"):
        self.model = model or DEFAULT_MODELS["ollama"]
        self.host = host.rstrip("/")
        self.dim = 0

    @classmethod
    def available(cls, host: str = "http://127.0.0.1:11434", timeout: float = 0.8) -> bool:
        try:
            with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            batch = texts[start : start + _BATCH]
            try:
                vectors.extend(self._embed_batch(batch))
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                raise RuntimeError(f"ollama embedding 不可用：{exc}")
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.host}/api/embed", data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            embeddings = data.get("embeddings") or []
            if len(embeddings) == len(texts):
                self.dim = len(embeddings[0])
                return [[float(x) for x in vec] for vec in embeddings]
        except urllib.error.HTTPError:
            pass  # 老版本 ollama 没有 /api/embed，退回 /api/embeddings
        return [self._embed_legacy(text) for text in texts]

    def _embed_legacy(self, text: str) -> list[float]:
        payload = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.host}/api/embeddings", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vector = [float(x) for x in data.get("embedding", [])]
        if vector:
            self.dim = len(vector)
        return vector


class FastEmbedProvider:
    """fastembed（ONNX，离线），首次使用会下载模型。"""

    id = "fastembed"
    is_semantic = True

    def __init__(self, model: str | None = None):
        self.model = model or DEFAULT_MODELS["fastembed"]
        self.dim = 0
        self._model = None

    def _ensure(self):
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding  # type: ignore
        except ImportError as exc:
            raise RuntimeError(f"未安装 fastembed：{exc}")
        self._model = TextEmbedding(model_name=self.model)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure()
        vectors = [list(map(float, vec)) for vec in model.embed(texts)]
        if vectors:
            self.dim = len(vectors[0])
        return vectors


class OpenAICompatProvider:
    """OpenAI 兼容的 /v1/embeddings（Herdsman / LM Studio / vLLM 等）。"""

    id = "openai"
    is_semantic = True

    def __init__(self, base_url: str, model: str | None = None, api_key: str = ""):
        self.base_url = (base_url or "http://127.0.0.1:8080/v1").rstrip("/")
        self.model = model or DEFAULT_MODELS["openai"]
        self.api_key = api_key
        self.dim = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            vectors.extend(self._embed_batch(texts[start : start + _BATCH]))
        if vectors:
            self.dim = len(vectors[0])
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(f"{self.base_url}/embeddings", data=payload, headers=headers)
        with urllib.request.urlopen(request, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        vectors = [[float(x) for x in item.get("embedding") or []] for item in items]
        if len(vectors) != len(texts) or not all(vectors):
            raise RuntimeError("embedding 返回数量/维度不符合预期")
        return vectors


def detect(config: dict | None = None) -> EmbeddingProvider:
    """按配置与可用性选 Provider；永远返回一个可用实例。"""
    config = config or {}
    requested = (config.get("provider") or "auto").lower()
    host = config.get("host") or "http://127.0.0.1:11434"
    base_url = config.get("base_url")

    if requested == "hash":
        return HashProvider()
    if requested in ("openai", "openai-compatible"):
        return OpenAICompatProvider(base_url or "http://127.0.0.1:8080/v1", config.get("model") or None,
                                    config.get("api_key") or "")
    if requested == "ollama":
        return OllamaProvider(config.get("model") or None, host)
    if requested == "fastembed":
        return FastEmbedProvider(config.get("model") or None)

    if base_url:  # 显式给了 OpenAI 兼容地址，优先用它
        return OpenAICompatProvider(base_url, config.get("model") or None, config.get("api_key") or "")
    if OllamaProvider.available(host):
        return OllamaProvider(config.get("model") or None, host)
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return HashProvider()
    return FastEmbedProvider(config.get("model") or None)


def vector_bytes(vec: list[float]) -> bytes:
    return array("f", _unit(vec)).tobytes()
