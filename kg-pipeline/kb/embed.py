"""
bge-m3 本地编码（ONNX，零调用费）。

模型来源：ModelScope `Xenova/bge-m3` 的 `onnx/model_int8.onnx`（约 568MB）。
为什么不用 fastembed：它的内置模型表里**没有 bge-m3**（只有 bge-small-zh），
而它的自定义模型机制还要额外写 ModelSource；直接调 onnxruntime 更短、更可控。

池化方式：BGE 用 **CLS**（取 last_hidden_state[:, 0]）再做 L2 归一化。
⚠️ bge-m3 **不需要**查询指令前缀（与 bge v1.5 不同）。
"""
import os
from typing import Iterable, List, Sequence

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

DEFAULT_MODEL_DIR = os.path.expanduser('~/.cache/ai-kg/bge-m3')
ONNX_FILE = os.path.join(DEFAULT_MODEL_DIR, 'onnx', 'model_int8.onnx')
TOKENIZER_FILE = os.path.join(DEFAULT_MODEL_DIR, 'tokenizer.json')

MAX_TOKENS = 8192          # bge-m3 的上限；实际由调用方按字符预算控制

# 写进 slide_parts.model 的版本标记。换模型/换量化版本时必须改这个值，
# 建库时的向量搬运和 build_vectors.py 的增量判断都依赖它。
MODEL_TAG = 'bge-m3-int8-v1'


class Embedder:
    def __init__(self, model_dir: str = DEFAULT_MODEL_DIR,
                 threads: int = 0, max_tokens: int = MAX_TOKENS):
        onnx_path = os.path.join(model_dir, 'onnx', 'model_int8.onnx')
        tok_path = os.path.join(model_dir, 'tokenizer.json')
        if not os.path.exists(onnx_path):
            raise FileNotFoundError(
                '找不到模型：%s\n'
                '下载（ModelScope）：\n'
                '  curl -L -o %s "https://www.modelscope.cn/api/v1/models/'
                'Xenova/bge-m3/repo?Revision=master&FilePath=onnx/model_int8.onnx"'
                % (onnx_path, onnx_path))

        self.tok = Tokenizer.from_file(tok_path)
        self.tok.enable_truncation(max_length=max_tokens)
        self.tok.enable_padding(pad_id=1, pad_token='<pad>')

        so = ort.SessionOptions()
        if threads:
            so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(onnx_path, so,
                                         providers=['CPUExecutionProvider'])
        self.input_names = {i.name for i in self.sess.get_inputs()}
        self.outputs = [o.name for o in self.sess.get_outputs()]
        # 有的导出会直接给 sentence_embedding；没有就自己池化
        self.emb_out = next((o for o in self.outputs if 'sentence' in o.lower()), None)

    @property
    def dim(self) -> int:
        return 1024

    def encode(self, texts: Sequence[str], batch_size: int = 4) -> np.ndarray:
        """返回 (N, 1024) 的 L2 归一化向量。"""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        out: List[np.ndarray] = []
        for i in range(0, len(texts), batch_size):
            batch = [t if isinstance(t, str) and t.strip() else ' ' for t in texts[i:i + batch_size]]
            encs = self.tok.encode_batch(batch)
            feeds = {
                'input_ids': np.array([e.ids for e in encs], dtype=np.int64),
                'attention_mask': np.array([e.attention_mask for e in encs], dtype=np.int64),
            }
            if 'token_type_ids' in self.input_names:
                feeds['token_type_ids'] = np.zeros_like(feeds['input_ids'])

            if self.emb_out:
                vec = self.sess.run([self.emb_out], feeds)[0]
            else:
                hidden = self.sess.run(['last_hidden_state'], feeds)[0]
                vec = hidden[:, 0]                      # CLS pooling
            vec = np.asarray(vec, dtype=np.float32)
            # L2 归一化
            norm = np.linalg.norm(vec, axis=1, keepdims=True)
            norm[norm == 0] = 1.0
            out.append(vec / norm)

        return np.vstack(out)
